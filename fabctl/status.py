"""The status collector: everything the page at https://cicd.touseefahmed.com shows, measured on the
running lab and written to one JSON file.

fabric-status.timer runs it every 30 s from the production checkout, so it describes the lab with
the code and the intent of the commit that runs. It only reads: show commands and pings inside the
lab, Git, and the files that the deploy agent and the game day keep. It never changes a router, so
a bug here can make the page wrong but never the network.

One check is left to the agent. Comparing a router's running config with Git means staging a file
on the router and running frr-reload there; the agent already does that for every router every
minute, and repairs any difference, so the page shows when it last did instead of doing it twice.
"""
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import agent, lab

OUT = Path(os.environ.get("FABRIC_STATUS", "/var/lib/fabric-status")) / "status.json"
EVENTS = 30               # newest agent events on the page
GAMEDAY_MAX_S = 45 * 60   # a game day still "running" after this long died without saying so


def git(*args):
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def commit(ref):
    out = git("log", "-1", "--format=%H%x1f%s%x1f%cI", ref)
    if not out:
        return None
    sha, subject, date = out.split("\x1f")
    return {"sha": sha, "subject": subject, "date": date}


def repository():
    found = re.search(r"github\.com[:/](.+?)(?:\.git)?/?$", git("remote", "get-url", "origin"))
    return f"https://github.com/{found[1]}" if found else None


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def history():
    """The agent's newest events first."""
    try:
        lines = (agent.STATE / "history.jsonl").read_text().splitlines()[-EVENTS:]
    except OSError:
        return []
    events = []
    for line in reversed(lines):
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    return events


def gameday():
    result = read_json(agent.STATE / "gameday.json")
    if result and result.get("running"):
        started = datetime.fromisoformat(result["started"]).timestamp()
        result["running"] = time.time() - started < GAMEDAY_MAX_S
    return result


# ---- Routers and cables --------------------------------------------------------------------------

def links(node):
    """{port: link up}, or None if the router does not answer at all (stopped, or frozen)."""
    result = lab.run(node, "sh", "-c", "grep -H . /sys/class/net/eth*/operstate", timeout=10)
    if result.returncode != 0:
        return None
    return {port: state == "up" for port, state in re.findall(r"/sys/class/net/([^/]+)/operstate:(\S+)", result.stdout)}


def router(node):
    facts, ports = lab.DEVICES[node], links(node)
    entry = {"name": node, "role": facts["role"], "asn": facts["asn"], "loopback": facts.get("loopback"),
             "answers": ports is not None, "links": ports or {}, "established": []}
    if ports is None:
        entry["problems"] = ["does not answer: the container is stopped or frozen"]
        return entry
    have = lab.established(node)
    want = lab.expected_sessions(node)
    entry["established"] = sorted(have)
    entry["sessions"] = {"up": len(set(want) & set(have)), "want": len(want)}
    problems = lab.session_problems(node, have)
    if facts["role"] != "isp":
        bfd = lab.bfd_problems(node)
        entry["bfd"] = {"up": len(facts["ports"]) - len(bfd), "want": len(facts["ports"])}
        problems += bfd
    if facts["role"] in ("leaf", "border"):
        problems += lab.ecmp_problems(node)
    if facts["role"] == "leaf":
        problems += lab.vni_problems(node) + lab.leaf_route_problems(node)
    if facts["role"] == "isp":
        problems += lab.isp_problems()
    entry["problems"] = problems
    return entry


def cable(kind, a, a_port, b, b_port, routers, sessions=()):
    """One cable: 'up', 'down' (no link), 'no session' (link up, BGP down) or 'unknown' (no end answers).
    sessions: (router, (vrf, peer)) pairs, any of which shows the BGP session over this cable."""
    ends = [routers[n]["links"].get(port, False) for n, port in ((a, a_port), (b, b_port))
            if n in routers and routers[n]["answers"]]
    seen = [tuple(key) in {tuple(k) for k in routers[n]["established"]} for n, key in sessions if routers[n]["answers"]]
    if not ends:
        state = "unknown"
    elif not all(ends):
        state = "down"
    elif sessions and not any(seen):
        state = "no session" if seen else "unknown"
    else:
        state = "up"
    return {"kind": kind, "a": a, "a_port": a_port, "b": b, "b_port": b_port, "state": state}


def cables(routers):
    found, devices = [], lab.DEVICES
    for node in lab.LEAVES + lab.BORDERS:
        for port in devices[node]["ports"]:
            spine = port["peer"]
            back = next(p["name"] for p in devices[spine]["ports"] if p["peer"] == node)
            found.append(cable("fabric", node, port["name"], spine, back, routers,
                               [(node, ("default", port["name"])), (spine, ("default", back))]))
    for border in lab.BORDERS:
        facts = devices[border]
        isp = next(p for p in devices["isp"]["ports"] if p["peer"] == border)
        found.append(cable("isp", border, facts["isp"]["port"], "isp", isp["name"], routers,
                           [(border, (facts["tenant"]["name"], facts["isp"]["peer"])), ("isp", ("default", isp["peer_address"]))]))
    for leaf in lab.LEAVES:
        for server in devices[leaf]["servers"]:
            found.append(cable("server", leaf, server["port"], server["name"], "eth1", routers))
    return found


# ---- Traffic and the edge ------------------------------------------------------------------------

def reachability():
    """Every server pings every other server and the internet, two pings each, one answer is enough.
    All the pings of a server start at once inside it: one docker exec per server, not per pair."""
    def from_server(src):
        targets = {lab.address(dst): dst for s, dst in lab.PAIRS if s == src}
        script = " ".join(f"(ping -c 2 -i 0.2 -W 1 -q {ip} >/dev/null 2>&1 && echo {ip} yes || echo {ip} no) &"
                          for ip in targets) + " wait"
        said = dict(line.split() for line in lab.run(src, "sh", "-c", script).stdout.splitlines() if len(line.split()) == 2)
        return {dst: said.get(ip) == "yes" for ip, dst in targets.items()}
    answered = lab.in_parallel(from_server, [(h,) for h in lab.HOSTS])
    return [{"from": s, "to": d, "allowed": lab.should_reach(s, d), "answers": answered[(s,)][d]} for s, d in lab.PAIRS]


def edge():
    """What the ISP learns from us, and how each leaf leaves the fabric for the internet."""
    from_border = {p["peer_address"]: p["peer"] for p in lab.DEVICES["isp"]["ports"]}
    routes = lab.vtysh_json("isp", "show ip bgp json").get("routes", {})
    received = []
    for prefix, paths in sorted(routes.items()):
        borders = sorted({from_border[p.get("peerId")] for p in paths if p.get("peerId") in from_border})
        if borders:
            received.append({"prefix": prefix, "from": borders})
    by_loopback = {lab.DEVICES[b]["loopback"]: b for b in lab.BORDERS}
    defaults = []
    for leaf in lab.LEAVES:
        for tenant in lab.DEVICES[leaf]["tenants"]:
            if lab.INTENT["tenants"][tenant["name"]]["internet"]:
                via = lab.bgp_routes(leaf, tenant["name"]).get("0.0.0.0/0", set())
                defaults.append({"leaf": leaf, "tenant": tenant["name"], "via": sorted(by_loopback.get(ip, ip) for ip in via)})
    return {"announce": lab.INTENT["isp"]["announce"], "received": received, "default_routes": defaults}


# ---- The whole picture ---------------------------------------------------------------------------

def collect(previous=None):
    started = time.time()
    found = lab.in_parallel(router, [(n,) for n in lab.SWITCHES])
    routers = {name: found[(name,)] for name in lab.SWITCHES}
    reach = reachability()
    problems = [f"{name} {p}" for name, r in routers.items() for p in r["problems"]]
    problems += [f"{p['from']} gets no answer from {p['to']}" for p in reach if p["allowed"] and not p["answers"]]
    problems += [f"{p['from']} gets an answer from {p['to']}, which its tenant forbids"
                 for p in reach if not p["allowed"] and p["answers"]]

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    since = now
    if previous and bool(previous.get("problems")) == bool(problems):
        since = previous.get("since", now)              # the overall state has not changed
    behind = git("rev-list", "--count", f"HEAD..origin/{agent.BRANCH}")
    return {
        "generated": round(time.time(), 1),
        "since": since,
        "fabric": {"name": lab.LAB, "bfd": lab.INTENT["fabric"]["bfd"]},
        "repository": repository(),
        "running": commit("HEAD"),
        "main": commit(f"origin/{agent.BRANCH}"),
        "behind": int(behind) if behind.isdigit() else None,
        "required_checks": list(agent.REQUIRED),
        "agent": {**(read_json(agent.STATE / "state.json") or {}), "history": history()},
        "gameday": gameday(),
        "routers": [{k: v for k, v in r.items() if k not in ("links", "established")} for r in routers.values()],
        "cables": cables(routers),
        "servers": [{"name": x["name"], "leaf": leaf, "port": x["port"], "tenant": lab.DEVICES[x["name"]]["tenant"],
                     "vlan": x["vlan"], "address": lab.address(x["name"])}
                    for leaf in lab.LEAVES for x in sorted(lab.DEVICES[leaf]["servers"], key=lambda x: x["port"])],
        "tenants": [{"name": name, "l3vni": t["l3vni"], "internet": t["internet"],
                     "vlans": [{"id": vid, **v} for vid, v in t["vlans"].items()]}
                    for name, t in lab.INTENT["tenants"].items()],
        "internet": lab.INTERNET,
        "reach": reach,
        "edge": edge(),
        "problems": problems,
        "took_s": round(time.time() - started, 1),
    }


def write(status):
    """status.json, replaced in one step: a visitor never gets half a file."""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    new = OUT.with_name(".status.json.tmp")
    new.write_text(json.dumps(status, separators=(",", ":")) + "\n")
    new.chmod(0o644)
    new.replace(OUT)


def main():
    previous = read_json(OUT)
    status = collect(previous)
    write(status)
    if previous is None or previous.get("problems") != status["problems"]:      # the journal gets changes only
        print(f"{len(status['problems'])} problem(s), collected in {status['took_s']} s", flush=True)
        for line in status["problems"]:
            print(f"    {line}", flush=True)
    return 0
