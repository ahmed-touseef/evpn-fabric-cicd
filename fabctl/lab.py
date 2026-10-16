"""Probes for the running lab, shared by the live tests and the deploy agent: how to reach the lab,
how to wait for it, and what a healthy fabric looks like according to intent/fabric.yml.

Every expectation is computed from the intent, never read from the running network: the network
is what is being checked, the intent is what it must obey. Nothing here sleeps for a fixed time.
A check is repeated until it holds or its deadline passes, so a slow machine only makes the run
longer, never wrong.
"""
import ipaddress
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import frr, intent as intent_file, model

INTENT = intent_file.load()
LAB = INTENT["fabric"]["name"]
DEVICES = model.devices(INTENT)
HOSTS = sorted(n for n, f in DEVICES.items() if f["role"] == "host")
SWITCHES = [n for n, f in DEVICES.items() if f["role"] != "host"]
SPINES = [n for n in SWITCHES if DEVICES[n]["role"] == "spine"]
LEAVES = [n for n in SWITCHES if DEVICES[n]["role"] == "leaf"]
BORDERS = [n for n in SWITCHES if DEVICES[n]["role"] == "border"]
INTERNET = INTENT["isp"]["internet_host"]
BOOT_S = 120              # all BGP sessions came up in about 4 s on GitHub's machine; this is the ceiling
SETTLE_S = 20             # after that, how long any single check may take to come true
SUDO = [] if os.geteuid() == 0 else ["sudo"]      # the agent runs as root already; people and the pipeline do not


# What the failure tests measured during this run: printed at the end of every run, and saved
# as JSON when FABRIC_REPORT names a file (the nightly game day does, for the status page).
MEASURED = {}


def measure(name, value):
    MEASURED[name] = value


# ---- Reaching the lab ----------------------------------------------------------------------------

def run(node, *command, timeout=30, stdin=None):
    """Runs a command inside one container of the lab, optionally feeding it text on stdin."""
    feed = ["-i"] if stdin is not None else []
    try:
        return subprocess.run([*SUDO, "docker", "exec", *feed, f"clab-{LAB}-{node}", *command],
                              input=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, "", "timed out")


def docker(action, node):
    """docker pause, unpause and so on, for one container of the lab."""
    subprocess.run([*SUDO, "docker", action, f"clab-{LAB}-{node}"], check=True, capture_output=True)


def vtysh_json(node, command):
    try:
        return json.loads(run(node, "vtysh", "-c", command).stdout or "{}")
    except ValueError:
        return {}


def eventually(problems, deadline_s=SETTLE_S):
    """Re-runs a check until it reports no problems, or the deadline passes. Returns the last report."""
    end = time.time() + deadline_s
    while True:
        found = problems()
        if not found or time.time() > end:
            return found
        time.sleep(1)


def report(title, found):
    return f"{title}:\n" + "\n".join(f"  {line}" for line in found)


def in_parallel(function, jobs):
    with ThreadPoolExecutor(16) as pool:
        return dict(zip(jobs, pool.map(lambda job: function(*job), jobs)))


def address(name):
    return name if name not in DEVICES else DEVICES[name]["address"].split("/")[0]


# ---- BGP sessions --------------------------------------------------------------------------------

def expected_sessions(node):
    """{(vrf, peer): hostname of the device that must be on the other end}, from the cabling."""
    facts = DEVICES[node]
    if facts["role"] == "isp":
        return {("default", p["peer_address"]): p["peer"] for p in facts["ports"]}
    sessions = {("default", p["name"]): p["peer"] for p in facts["ports"]}
    if facts["role"] == "border" and facts["tenant"]:
        sessions[(facts["tenant"]["name"], facts["isp"]["peer"])] = "isp"
    return sessions


def established(node):
    found = {}
    for vrf, families in vtysh_json(node, "show bgp vrf all summary json").items():
        for family in (families.values() if isinstance(families, dict) else []):
            for peer, state in (family.get("peers", {}).items() if isinstance(family, dict) else []):
                if state.get("state") == "Established":
                    found[(vrf, peer)] = state.get("hostname")
    return found


def session_problems(node, have=None):
    """Sessions missing, leading to the wrong device, or not in the intent. `have` is what
    established(node) returned, when the caller already asked."""
    want, have = expected_sessions(node), established(node) if have is None else have
    return ([f"{vrf} {peer}: not established (should reach {who})" for (vrf, peer), who in want.items()
             if (vrf, peer) not in have] +
            [f"{vrf} {peer}: reaches {have[(vrf, peer)]}, the cabling says {who}" for (vrf, peer), who in want.items()
             if (vrf, peer) in have and have[(vrf, peer)] != who] +
            [f"{vrf} {peer}: established, but the intent has no such session" for (vrf, peer) in have
             if (vrf, peer) not in want])


def bfd_problems(node):
    """BFD must be up on every fabric link of a switch, with the timers of the intent."""
    bfd = INTENT["fabric"]["bfd"]
    raw = vtysh_json(node, "show bfd peers json")
    peers = {p.get("interface"): p for p in raw} if isinstance(raw, list) else {}
    found = []
    for port in sorted(p["name"] for p in DEVICES[node]["ports"]):
        peer = peers.get(port)
        if not peer:
            found.append(f"{port}: no BFD session")
        elif peer.get("status") != "up":
            found.append(f"{port}: BFD is {peer.get('status')}")
        elif (peer.get("transmit-interval"), peer.get("detect-multiplier")) != (bfd["interval_ms"], bfd["multiplier"]):
            found.append(f"{port}: BFD every {peer.get('transmit-interval')} ms x {peer.get('detect-multiplier')}, "
                         f"the intent says {bfd['interval_ms']} ms x {bfd['multiplier']}")
    return found


# ---- The running config against Git -------------------------------------------------------------

def config_problems(node):
    """What a reload of the rendered config would still change on the running router: must be nothing."""
    wanted = (Path("rendered") / node / "frr.conf").read_text()
    result = frr.changes(lambda *command, stdin=None: run(node, *command, stdin=stdin), wanted)
    if result is None:
        return ["frr-reload could not compare the running config with the rendered one"]
    delete, add = result
    return [f"would delete  {line}" for line in delete] + [f"would add     {line}" for line in add]


# ---- EVPN ----------------------------------------------------------------------------------------

def vni_problems(leaf):
    """An L2 VNI must list as remote VTEPs exactly the other leaves with servers in that VLAN,
    and every tenant of the leaf must have its L3VNI in its own VRF."""
    facts = DEVICES[leaf]
    carriers = {}
    for other in LEAVES:
        for t in DEVICES[other]["tenants"]:
            for vlan in t["vlans"]:
                carriers.setdefault(vlan["vni"], set()).add(DEVICES[other]["loopback"])
    want = {vlan["vni"]: (t["name"], carriers[vlan["vni"]] - {facts["loopback"]})
            for t in facts["tenants"] for vlan in t["vlans"]}
    l3 = {t["l3vni"]: t["name"] for t in facts["tenants"]}
    have = vtysh_json(leaf, "show evpn vni json")
    found = []
    for vni, (tenant, vteps) in want.items():
        entry = have.get(str(vni))
        if not entry:
            found.append(f"VNI {vni}: missing")
        elif entry.get("tenantVrf") != tenant:
            found.append(f"VNI {vni}: in VRF {entry.get('tenantVrf')}, the intent says {tenant}")
        elif set(entry.get("remoteVteps") or []) != vteps:
            found.append(f"VNI {vni}: remote VTEPs {sorted(entry.get('remoteVteps') or [])}, expected {sorted(vteps)}")
    for vni in have:
        if int(vni) not in want and int(vni) not in l3:
            found.append(f"VNI {vni}: configured, but the intent has no such VNI on {leaf}")
    for vni, tenant in l3.items():
        entry = have.get(str(vni), {})
        if entry.get("type") != "L3" or entry.get("tenantVrf") != tenant:
            found.append(f"L3VNI {vni}: expected for VRF {tenant}, found {entry or 'nothing'}")
    return found


# ---- Routing -------------------------------------------------------------------------------------

def bgp_routes(node, vrf):
    """{prefix: set of next hops} for the BGP routes this switch actually uses in a VRF."""
    routes = {}
    for prefix, entries in vtysh_json(node, f"show ip route vrf {vrf} json").items():
        for entry in entries if isinstance(entries, list) else []:
            if entry.get("protocol") == "bgp" and entry.get("selected"):
                routes[prefix] = {nh.get("ip") for nh in entry.get("nexthops", []) if nh.get("ip")}
    return routes


def ecmp_problems(node):
    """A leaf or border must reach every other leaf and border through every spine at once."""
    uplinks = {p["name"] for p in DEVICES[node]["ports"]}
    routes = vtysh_json(node, "show ip route json")
    found = []
    for other in LEAVES + BORDERS:
        if other == node:
            continue
        prefix = f"{DEVICES[other]['loopback']}/32"
        via = {nh.get("interfaceName") for entry in routes.get(prefix, []) if entry.get("selected")
               for nh in entry.get("nexthops", []) if nh.get("active")} - {None}
        if via != uplinks:
            found.append(f"reaches {other} ({prefix}) via {sorted(via)}, expected via every spine {sorted(uplinks)}")
    return found


def leaf_route_problems(leaf, borders=None):
    """Inside a tenant's VRF a leaf may only hold that tenant's own subnets, plus a default route
    through every working border if the tenant has internet. Never an ISP address, never another tenant."""
    border_loopbacks = {DEVICES[b]["loopback"] for b in (borders or BORDERS)}
    found = []
    for t in DEVICES[leaf]["tenants"]:
        subnets = [ipaddress.ip_network(v["subnet"]) for v in INTENT["tenants"][t["name"]]["vlans"].values()]
        routes = bgp_routes(leaf, t["name"])
        if INTENT["tenants"][t["name"]]["internet"]:
            if routes.get("0.0.0.0/0") != border_loopbacks:
                found.append(f"{t['name']}: default route via {sorted(routes.get('0.0.0.0/0', []))}, "
                             f"expected via {sorted(border_loopbacks)}")
        elif "0.0.0.0/0" in routes:
            found.append(f"{t['name']}: has a default route, but the tenant has no internet")
        for prefix in routes:
            if prefix != "0.0.0.0/0" and not any(ipaddress.ip_network(prefix).subnet_of(s) for s in subnets):
                found.append(f"{t['name']}: holds {prefix}, which belongs to no subnet of this tenant")
    return found


def isp_problems(borders=None):
    """The ISP must learn exactly the summaries, from every working border, and nothing else."""
    want = set(INTENT["isp"]["announce"].values())
    alive = set(borders or BORDERS)
    peers = {p["peer_address"] for p in DEVICES["isp"]["ports"] if p["peer"] in alive}
    every = {p["peer_address"] for p in DEVICES["isp"]["ports"]}
    routes = vtysh_json("isp", "show ip bgp json").get("routes", {})
    received = {prefix: {path.get("peerId") for path in paths} & every for prefix, paths in routes.items()}
    received = {prefix: frm for prefix, frm in received.items() if frm}
    return ([f"{p}: should not be announced to the ISP" for p in received if p not in want] +
            [f"{p}: received from {sorted(received.get(p, []))}, expected from {sorted(peers)}"
             for p in want if received.get(p) != peers])


def healthy_problems():
    """Everything a whole fabric must show at once: sessions, BFD, configs exactly as in Git, ECMP,
    routing tables and the ISP view."""
    found = [f"{n} {p}" for n in SWITCHES for p in session_problems(n)]
    found += [f"{n} {p}" for n in SPINES + LEAVES + BORDERS for p in bfd_problems(n)]
    found += [f"{n} {p}" for n in SWITCHES for p in config_problems(n)]
    found += [f"{n} {p}" for n in LEAVES + BORDERS for p in ecmp_problems(n)]
    found += [f"{n} {p}" for n in LEAVES for p in leaf_route_problems(n)]
    found += [f"isp {p}" for p in isp_problems()]
    return found


# ---- Traffic -------------------------------------------------------------------------------------

def should_reach(src, dst):
    if dst == INTERNET:
        return INTENT["tenants"][DEVICES[src]["tenant"]]["internet"]
    return DEVICES[src]["tenant"] == DEVICES[dst]["tenant"]


PAIRS = [(s, d) for s in HOSTS for d in HOSTS + [INTERNET] if s != d]
ALLOWED = [pair for pair in PAIRS if should_reach(*pair)]
FORBIDDEN = [pair for pair in PAIRS if not should_reach(*pair)]


def answers(src, dst, size=None, count=3):
    command = ["ping", "-c", str(count), "-i", "0.2", "-W", "1", "-q", address(dst)]
    if size:
        command[1:1] = ["-M", "do", "-s", str(size)]          # don't fragment: it must fit as it is
    return run(src, *command).returncode == 0


def answers_within(src, dst, deadline_s=SETTLE_S, size=None):
    end = time.time() + deadline_s
    while not answers(src, dst, size=size, count=1):
        if time.time() > end:
            return False
    return True
