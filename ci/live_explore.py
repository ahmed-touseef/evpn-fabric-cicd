"""Looks at the fabric booted on this machine and prints what it sees.

The strict checks live in tests/live. The pipeline runs this script only when one of them
fails, so the log also shows the state behind the failure: timings, the full ping matrix
and the routing tables. It never fails anything itself, and works by hand on the lab too.
Every name and address comes from intent/fabric.yml.
"""
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))     # the repo root holds fabctl
from fabctl import intent as intent_file, model                      # noqa: E402

INTENT = intent_file.load()
LAB = INTENT["fabric"]["name"]
DEVICES = model.devices(INTENT)
SWITCHES = [name for name, facts in DEVICES.items() if facts["role"] != "host"]
HOSTS = sorted((name for name, facts in DEVICES.items() if facts["role"] == "host"),
               key=lambda name: (DEVICES[name]["tenant"], name))
INTERNET = INTENT["isp"]["internet_host"]


def run(node, *command, timeout=30):
    """Runs a command inside one container of the lab."""
    try:
        return subprocess.run(["sudo", "docker", "exec", f"clab-{LAB}-{node}", *command],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, "", "timed out")


def show(node, command):
    print(f"\n--- {node}: {command}")
    result = run(node, "vtysh", "-c", command)
    print((result.stdout or result.stderr).rstrip())


def section(title):
    print(f"\n===== {title} =====", flush=True)


def established(node):
    """The BGP sessions of one switch that are up, as {(vrf, peer)}."""
    try:
        data = json.loads(run(node, "vtysh", "-c", "show bgp vrf all summary json").stdout)
    except ValueError:
        return set()
    return {(vrf, peer) for vrf, families in data.items() if isinstance(families, dict)
            for family in families.values() if isinstance(family, dict)
            for peer, state in family.get("peers", {}).items() if state.get("state") == "Established"}


def address(host):
    return DEVICES[host]["address"].split("/")[0]


def answers(src, dst, size=None, count=3):
    """True if at least one ping from src to dst gets an answer."""
    command = ["ping", "-c", str(count), "-i", "0.2", "-W", "1", "-q", dst]
    if size:
        command[1:1] = ["-M", "do", "-s", str(size)]          # don't fragment: it must fit as it is
    return run(src, *command).returncode == 0


def first_answer(src, dst, limit=120):
    """Seconds until src first reaches dst, or None."""
    start = time.time()
    while time.time() - start < limit:
        if answers(src, dst, count=1):
            return time.time() - start
        time.sleep(0.5)
    return None


def should_reach(src, dst):
    if dst == INTERNET:
        return INTENT["tenants"][DEVICES[src]["tenant"]]["internet"]
    return DEVICES[src]["tenant"] == DEVICES[dst]["tenant"]


def parallel(function, jobs):
    with ThreadPoolExecutor(16) as pool:
        return dict(zip(jobs, pool.map(lambda job: function(*job), jobs)))


# ---- 1. Timing: how long the fabric takes to come up on this machine ---------------------------

section("How long until every BGP session is up?")
spines, below = len(INTENT["spines"]["devices"]), len(INTENT["leaves"]) + len(INTENT["borders"])
want = 2 * spines * below + 2 * len(INTENT["borders"])
start, last = time.time(), None
while True:
    with ThreadPoolExecutor(len(SWITCHES)) as pool:
        up = sum(len(sessions) for sessions in pool.map(established, SWITCHES))
    waited = time.time() - start
    if up != last:
        print(f"  {waited:5.1f} s  {up:2} of {want} session ends up", flush=True)
        last = up
    if up >= want or waited > 120:
        break
    time.sleep(1)

section("How long until traffic flows? (counted from when every session is up)")
bridged = next((a, b) for a in HOSTS for b in HOSTS
               if DEVICES[a]["vlan"] == DEVICES[b]["vlan"] and DEVICES[a]["leaf"] != DEVICES[b]["leaf"])
online = next(h for h in HOSTS if INTENT["tenants"][DEVICES[h]["tenant"]]["internet"])
pairs = [(bridged[0], address(bridged[1]), "same VLAN, across the fabric"), (online, INTERNET, "to the internet")]
for src, dst, what in pairs:
    took = first_answer(src, dst)
    print(f"  {src} -> {dst} ({what}): " + (f"first answer after {took:.1f} s" if took is not None else "no answer in 120 s"),
          flush=True)

# ---- 2. Traffic: who reaches whom ------------------------------------------------------------

section("Ping matrix: ok = answered, . = no answer, ! = differs from the intent")
targets = HOSTS + [INTERNET]
jobs = [(src, dst) for src in HOSTS for dst in targets if dst != src]
reached = parallel(lambda src, dst: answers(src, address(dst) if dst in DEVICES else dst), jobs)
names = [t if t in DEVICES else "internet" for t in targets]
print("  from \\ to     " + "".join(f"{n:>10}" for n in names))
for src in HOSTS:
    cells = []
    for dst in targets:
        if dst == src:
            cells.append(f"{'':>10}")
            continue
        mark = "ok" if reached[(src, dst)] else "."
        if reached[(src, dst)] != should_reach(src, dst):
            mark = "!" + mark
        cells.append(f"{mark:>10}")
    print(f"  {src:5} {DEVICES[src]['tenant']:8}" + "".join(cells))
wrong = [(s, d) for (s, d), ok in reached.items() if ok != should_reach(s, d)]
print(f"\n  {len(jobs) - len(wrong)} of {len(jobs)} pairs behave as the intent says")

section("Full size packets: 1500 bytes with don't fragment set, inside each tenant")
same = [(s, d) for s in HOSTS for d in HOSTS if s != d and DEVICES[s]["tenant"] == DEVICES[d]["tenant"]]
big = parallel(lambda src, dst: answers(src, address(dst), size=1472), same)     # 1472 + 28 header bytes
for (src, dst), ok in big.items():
    if not ok:
        print(f"  {src} -> {dst}: too big, dropped")
print(f"  {sum(big.values())} of {len(same)} pairs pass full size packets")

# ---- 3. Tables: what the routers believe -----------------------------------------------------

section("BGP sessions")
for node in [n for n in SWITCHES if DEVICES[n]["role"] == "spine"][:1]:
    show(node, "show bgp summary")
for role in ("leaf", "border"):
    show(next(n for n in SWITCHES if DEVICES[n]["role"] == role), "show bgp vrf all summary")
show("isp", "show bgp summary")

section("EVPN: which VNIs each leaf carries, and how many other leaves share them")
for node in [n for n in SWITCHES if DEVICES[n]["role"] == "leaf"]:
    show(node, "show evpn vni")

section("Routing tables")
leaf = next(n for n in SWITCHES if DEVICES[n]["role"] == "leaf")
border = next(n for n in SWITCHES if DEVICES[n]["role"] == "border")
for tenant in INTENT["tenants"]:
    show(leaf, f"show ip route vrf {tenant}")
show(border, f"show ip route vrf {DEVICES[border]['tenant']['name']}")
show("isp", "show ip bgp")

section("Raw JSON, to design the strict tests")
show(leaf, "show evpn vni json")
show("isp", "show ip bgp json")

print("\nExploration finished.")
