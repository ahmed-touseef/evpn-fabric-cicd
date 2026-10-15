"""Live tests: boots nothing, checks the fabric that lab/deploy.sh booted on this machine.

Every expectation comes from intent/fabric.yml, never from the running network: the network
is what is being checked, the intent is what it must obey. Nothing here sleeps for a fixed
time. Each check is repeated until it holds or its deadline passes, so a slow machine only
makes the run longer, never wrong.
"""
import ipaddress
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from fabctl import intent as intent_file, model

INTENT = intent_file.load()
LAB = INTENT["fabric"]["name"]
DEVICES = model.devices(INTENT)
HOSTS = sorted(n for n, f in DEVICES.items() if f["role"] == "host")
SWITCHES = [n for n, f in DEVICES.items() if f["role"] != "host"]
LEAVES = [n for n in SWITCHES if DEVICES[n]["role"] == "leaf"]
BORDERS = [n for n in SWITCHES if DEVICES[n]["role"] == "border"]
INTERNET = INTENT["isp"]["internet_host"]
BOOT_S = 120              # all BGP sessions came up in about 4 s on GitHub's machine; this is the ceiling
SETTLE_S = 20             # after that, how long any single check may take to come true


# ---- Talking to the lab --------------------------------------------------------------------------

def run(node, *command, timeout=30):
    try:
        return subprocess.run(["sudo", "docker", "exec", f"clab-{LAB}-{node}", *command],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, "", "timed out")


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


def address(name):
    return name if name not in DEVICES else DEVICES[name]["address"].split("/")[0]


def tenant_of(host):
    return INTENT["tenants"][DEVICES[host]["tenant"]]


def in_parallel(function, jobs):
    with ThreadPoolExecutor(16) as pool:
        return dict(zip(jobs, pool.map(lambda job: function(*job), jobs)))


# ---- Control plane: BGP sessions and EVPN --------------------------------------------------------

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


@pytest.fixture(scope="session", autouse=True)
def booted():
    """Waits once for the whole fabric to bring its BGP sessions up, so that each test below only
    has to allow a few seconds of settling. Fails nothing itself: the tests say what is missing."""
    total = sum(len(expected_sessions(n)) for n in SWITCHES)
    end = time.time() + BOOT_S
    while time.time() < end:
        up = in_parallel(established, [(n,) for n in SWITCHES])
        if sum(len(sessions) for sessions in up.values()) >= total:
            return
        time.sleep(1)


@pytest.mark.parametrize("node", SWITCHES)
def test_every_bgp_session_is_up_to_the_right_neighbour(node):
    """Also proves the cabling: the session on eth11 of spine1 must lead to leaf1, and so on."""
    want = expected_sessions(node)

    def problems():
        have = established(node)
        return ([f"{vrf} {peer}: not established (should reach {who})" for (vrf, peer), who in want.items()
                 if (vrf, peer) not in have] +
                [f"{vrf} {peer}: reaches {have[(vrf, peer)]}, the cabling says {who}" for (vrf, peer), who in want.items()
                 if (vrf, peer) in have and have[(vrf, peer)] != who] +
                [f"{vrf} {peer}: established, but the intent has no such session" for (vrf, peer) in have
                 if (vrf, peer) not in want])

    found = eventually(problems)
    assert not found, report(f"{node} BGP sessions", found)


@pytest.mark.parametrize("leaf", LEAVES)
def test_every_vni_knows_every_other_leaf_that_carries_it(leaf):
    """An L2 VNI must list as remote VTEPs exactly the other leaves with servers in that VLAN."""
    facts = DEVICES[leaf]
    carriers = {}
    for other in LEAVES:
        for t in DEVICES[other]["tenants"]:
            for vlan in t["vlans"]:
                carriers.setdefault(vlan["vni"], set()).add(DEVICES[other]["loopback"])
    want = {vlan["vni"]: (t["name"], carriers[vlan["vni"]] - {facts["loopback"]})
            for t in facts["tenants"] for vlan in t["vlans"]}
    l3 = {t["l3vni"]: t["name"] for t in facts["tenants"]}

    def problems():
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

    found = eventually(problems)
    assert not found, report(f"{leaf} EVPN", found)


# ---- Routing: what comes in from outside --------------------------------------------------------

def bgp_routes(node, vrf):
    """{prefix: set of next hops} for the BGP routes this switch actually uses in a VRF."""
    routes = {}
    for prefix, entries in vtysh_json(node, f"show ip route vrf {vrf} json").items():
        for entry in entries if isinstance(entries, list) else []:
            if entry.get("protocol") == "bgp" and entry.get("selected"):
                routes[prefix] = {nh.get("ip") for nh in entry.get("nexthops", []) if nh.get("ip")}
    return routes


@pytest.mark.parametrize("leaf", LEAVES)
def test_leaves_learn_only_their_tenant_and_the_default_route(leaf):
    """Inside a tenant's VRF a leaf may only hold that tenant's own subnets, plus a default route
    through every border if the tenant has internet. Never an ISP address, never another tenant."""
    border_loopbacks = {DEVICES[b]["loopback"] for b in BORDERS}

    def problems():
        found = []
        for t in DEVICES[leaf]["tenants"]:
            subnets = [ipaddress.ip_network(v["subnet"]) for v in INTENT["tenants"][t["name"]]["vlans"].values()]
            routes = bgp_routes(leaf, t["name"])
            if INTENT["tenants"][t["name"]]["internet"]:
                if routes.get("0.0.0.0/0") != border_loopbacks:
                    found.append(f"{t['name']}: default route via {sorted(routes.get('0.0.0.0/0', []))}, "
                                 f"expected via every border {sorted(border_loopbacks)}")
            elif "0.0.0.0/0" in routes:
                found.append(f"{t['name']}: has a default route, but the tenant has no internet")
            for prefix in routes:
                net = ipaddress.ip_network(prefix)
                if prefix != "0.0.0.0/0" and not any(net.subnet_of(s) for s in subnets):
                    found.append(f"{t['name']}: holds {prefix}, which belongs to no subnet of this tenant")
        return found

    found = eventually(problems)
    assert not found, report(f"{leaf} routing tables", found)


def test_isp_learns_exactly_the_summaries_from_every_border():
    want = set(INTENT["isp"]["announce"].values())
    borders = {p["peer_address"] for p in DEVICES["isp"]["ports"]}

    def problems():
        routes = vtysh_json("isp", "show ip bgp json").get("routes", {})
        received = {prefix: {path.get("peerId") for path in paths} & borders for prefix, paths in routes.items()}
        received = {prefix: peers for prefix, peers in received.items() if peers}
        return ([f"{p}: should not be announced to the ISP" for p in received if p not in want] +
                [f"{p}: received from {sorted(received.get(p, []))}, expected from every border {sorted(borders)}"
                 for p in want if received.get(p) != borders])

    found = eventually(problems)
    assert not found, report("What the ISP learns from us", found)


# ---- Traffic: every pair of servers, both ways ---------------------------------------------------

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


def should_reach(src, dst):
    if dst == INTERNET:
        return tenant_of(src)["internet"]
    return DEVICES[src]["tenant"] == DEVICES[dst]["tenant"]


PAIRS = [(s, d) for s in HOSTS for d in HOSTS + [INTERNET] if s != d]
ALLOWED = [pair for pair in PAIRS if should_reach(*pair)]
FORBIDDEN = [pair for pair in PAIRS if not should_reach(*pair)]


def test_every_server_reaches_what_its_tenant_allows():
    """Includes first contact with quiet servers: a server that has not spoken yet must still be found."""
    result = in_parallel(answers_within, ALLOWED)
    failed = [f"{s} -> {d}" for (s, d), ok in result.items() if not ok]
    assert not failed, report(f"{len(failed)} of {len(ALLOWED)} allowed pairs get no answer", failed)


def test_nothing_reaches_what_its_tenant_forbids():
    """Runs after the test above, so every server is already known everywhere: isolation must hold
    in a fully learned fabric, not only in a cold one."""
    result = in_parallel(answers, FORBIDDEN)
    leaked = [f"{s} -> {d}" for (s, d), ok in result.items() if ok]
    assert not leaked, report(f"{len(leaked)} of {len(FORBIDDEN)} forbidden pairs get an answer", leaked)


def test_full_size_packets_cross_the_fabric():
    """1500 byte packets with don't fragment set: the tunnels add 50 bytes, the fabric must carry them."""
    pairs = [(s, d) for s, d in ALLOWED if d in DEVICES]
    result = in_parallel(lambda s, d: answers_within(s, d, deadline_s=5, size=1472), pairs)
    failed = [f"{s} -> {d}" for (s, d), ok in result.items() if not ok]
    assert not failed, report(f"{len(failed)} of {len(pairs)} pairs drop full size packets", failed)
