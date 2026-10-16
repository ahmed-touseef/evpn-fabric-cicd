"""The deploy agent: makes the lab on this server run what main says, and keeps it that way.

Pull, never push: GitHub holds no key to this server. A systemd timer runs one cycle a minute:

  1. Fetch main. Nothing new: check every router for drift from Git, and heal it.
  2. Accept a new commit only if it descends from the commit running now, and only once GitHub
     reports every required check green for exactly that commit.
  3. Apply it the gentlest way the change allows:
       router configs only  frr-reload on one router first (the canary), then the others one by
                            one, each checked before the next; any failure rolls them all back
       anything else        rebuild the lab from the new commit, check it, and rebuild it from
                            the old commit if the check fails
  4. Check the result with the pipeline's own live tests, and record what happened in a history
     file that the status page reads.

A lab found missing or incomplete, after a reboot for example, is rebuilt from the running commit,
and a file of this checkout edited by hand is restored from Git.
"""
import fcntl
import importlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import frr

REQUIRED = ("Intent and configs", "Batfish", "Live lab")      # must match the ruleset protecting main
BRANCH = "main"
STATE = Path(os.environ.get("FABRIC_STATE", "/var/lib/fabric-agent"))
SETTLE_S = 60             # how long a router may take to settle after a reload
BAKE_S = 20               # how long the canary must stay healthy before the others follow
ORDER = {"leaf": 0, "border": 1, "spine": 2, "isp": 3}       # the canary is the first changed router here


# ---- Decisions: pure functions, tested in the pipeline ------------------------------------------

def verdict(runs, required=REQUIRED):
    """'approved', 'rejected' or 'waiting', from GitHub's check runs for one commit. Only the latest
    run of each required check counts, so a re-run that passed replaces an earlier failure."""
    latest = {}
    for run in sorted(runs, key=lambda r: r.get("id", 0)):
        latest[run.get("name")] = run
    wanted = [latest.get(name) for name in required]
    if any(r and r.get("status") == "completed" and r.get("conclusion") != "success" for r in wanted):
        return "rejected"
    if all(r and r.get("status") == "completed" for r in wanted):
        return "approved"
    return "waiting"


def plan(changed, roles):
    """How to apply a change, from the files it touches:
        ("none", [])            nothing the network runs changed: docs, tests, the pipeline
        ("reload", routers)     only router configs changed, routers in rollout order, canary first
        ("rebuild", [])         anything else under rendered/: Linux side, cabling, a new device
    """
    routers = set()
    for path in (f for f in changed if f.startswith("rendered/")):
        parts = path.split("/")
        if len(parts) != 3 or parts[2] != "frr.conf" or parts[1] not in roles:
            return "rebuild", []
        routers.add(parts[1])
    if not routers:
        return "none", []
    return "reload", sorted(routers, key=lambda name: (ORDER.get(roles[name], len(ORDER)), name))


# ---- The world around the agent -----------------------------------------------------------------

def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def log(event, **fields):
    """One line to the journal, one JSON record to the history that the status page reads."""
    detail = fields.pop("detail", [])
    record = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
    print(f"{event}: " + ", ".join(f"{k}={v}" for k, v in fields.items()), flush=True)
    for line in detail:
        print(f"    {line}", flush=True)
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "history.jsonl", "a") as history:
        history.write(json.dumps({**record, "detail": detail} if detail else record) + "\n")


def remembered():
    try:
        return json.loads((STATE / "state.json").read_text())
    except (OSError, ValueError):
        return {}


def remember(**values):
    state = {**remembered(), **values}
    (STATE / "state.json").write_text(json.dumps(state, indent=1) + "\n")


def check_runs(sha):
    """GitHub's check runs for one commit. The repository is public, so no token is needed."""
    slug = re.search(r"github\.com[:/](.+?)(?:\.git)?/?$", git("remote", "get-url", "origin")).group(1)
    request = urllib.request.Request(f"https://api.github.com/repos/{slug}/commits/{sha}/check-runs?per_page=100",
                                     headers={"Accept": "application/vnd.github+json", "User-Agent": "fabric-agent"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response).get("check_runs", [])


def probes():
    """The pipeline's own lab checks, loaded for the commit now checked out (its intent, its configs)."""
    from . import lab
    return importlib.reload(lab)


def running_containers(lab):
    out = subprocess.run(["docker", "ps", "-q", "--filter", f"label=containerlab={lab.LAB}"],
                         capture_output=True, text=True).stdout
    return len(out.split())


def rebuild():
    """Boots the lab from the commit now checked out, with the same script as the pipeline."""
    result = subprocess.run(["./lab/deploy.sh"], capture_output=True, text=True)
    return result.returncode == 0, (result.stdout + result.stderr).strip().splitlines()[-10:]


def live_tests():
    """The pipeline's live tests of a healthy fabric, run against this lab: (passed, last lines)."""
    result = subprocess.run([sys.executable, "-m", "pytest", "tests/live/test_fabric.py", "-q", "--tb=line",
                             "-p", "no:cacheprovider"], capture_output=True, text=True)
    return result.returncode == 0, result.stdout.strip().splitlines()[-12:]


def reload(lab, node):
    """Sends one router the commands that make it run rendered/<node>/frr.conf of the checked out commit."""
    config = (Path("rendered") / node / "frr.conf").read_text()
    return frr.reload(lambda *command, stdin=None: lab.run(node, *command, stdin=stdin), config)


def settled(lab, nodes):
    """Problems left once the given routers had time to settle: their configs must match Git
    exactly, and every BGP session of the fabric must be up."""
    return lab.eventually(lambda: [f"{n} {p}" for n in nodes for p in lab.config_problems(n)] +
                          [f"{n} {p}" for n in lab.SWITCHES for p in lab.session_problems(n)], SETTLE_S)


# ---- Deploying ----------------------------------------------------------------------------------

def deploy(old, new):
    """Moves the lab from commit old to commit new. Returns True if new now runs, checked."""
    changed = git("diff", "--name-only", old, new).splitlines()
    git("checkout", "-q", "--detach", new)
    lab = probes()
    kind, routers = plan(changed, {name: facts["role"] for name, facts in lab.DEVICES.items()})
    started, base = time.time(), {"commit": new[:7], "previous": old[:7], "plan": kind}

    if kind == "none":
        log("deployed", **base, reason="nothing the network runs changed")
        return True

    if kind == "reload":
        done = []
        for node in routers:
            done.append(node)
            problems = settled(lab, [node]) if reload(lab, node) else [f"{node}: frr-reload failed"]
            if not problems and node == routers[0] and len(routers) > 1:
                time.sleep(BAKE_S)                       # the canary must stay healthy, not just get there
                problems = settled(lab, [node])
            if problems:
                return roll_back(old, new, done, problems, started)
        passed, report = live_tests()
        if not passed:
            return roll_back(old, new, done, report, started)
        log("deployed", **base, routers=routers, canary=routers[0], seconds=round(time.time() - started))
        return True

    built, report = rebuild()
    passed, report = live_tests() if built else (False, report)
    if passed:
        log("deployed", **base, seconds=round(time.time() - started))
        return True
    git("checkout", "-q", "--detach", old)
    restored = rebuild()[0] and live_tests()[0]
    log("rolled back" if restored else "rollback failed", **base, seconds=round(time.time() - started),
        detail=report)
    return False


def roll_back(old, new, done, why, started):
    """Puts the routers already changed back on the old commit's configs, newest change first."""
    git("checkout", "-q", "--detach", old)
    lab = probes()
    for node in reversed(done):
        reload(lab, node)
    left = settled(lab, done)
    log("rolled back" if not left else "rollback failed", commit=new[:7], previous=old[:7], plan="reload",
        routers=done, seconds=round(time.time() - started), detail=list(why)[-12:] + left)
    return False


def heal_drift(lab, commit):
    """Every router must run exactly what the running commit says. Anything typed by hand is undone."""
    for node in lab.SWITCHES:
        drift = lab.config_problems(node)
        if drift:
            reload(lab, node)
            left = lab.eventually(lambda: lab.config_problems(node), SETTLE_S)
            log("drift healed" if not left else "drift not healed", router=node, commit=commit[:7], detail=drift + left)


# ---- One cycle ----------------------------------------------------------------------------------

def cycle():
    edited = git("status", "--porcelain", "--untracked-files=no")
    if edited:                                   # production files are only ever changed by Git
        git("checkout", "-q", "--", ".")
        log("files restored", commit=git("rev-parse", "--short", "HEAD"), detail=[l.strip() for l in edited.splitlines()])
    git("fetch", "-q", "origin", BRANCH)
    old, new = git("rev-parse", "HEAD"), git("rev-parse", f"origin/{BRANCH}")
    state = remembered()
    lab = probes()

    if running_containers(lab) < len(lab.DEVICES):
        built, report = rebuild()
        passed, report = live_tests() if built else (False, report)
        log("rebuilt" if passed else "rebuild failed", commit=old[:7], reason="the lab was missing or incomplete",
            detail=[] if passed else report)
        return

    if new != old and new != state.get("refused"):
        if subprocess.run(["git", "merge-base", "--is-ancestor", old, new]).returncode != 0:
            log("refused", commit=new[:7], reason=f"it does not descend from the running commit {old[:7]}")
            remember(refused=new)
            return
        try:
            decision = verdict(check_runs(new))
        except OSError as error:
            print(f"GitHub not reachable, trying again next minute: {error}", flush=True)
            decision = "unknown"
        if decision == "rejected":
            log("refused", commit=new[:7], reason="a required check failed on GitHub")
            remember(refused=new)
            return
        if decision == "approved":
            if not deploy(old, new):
                remember(refused=new)
            return
        if decision == "waiting" and state.get("waiting") != new:
            log("waiting", commit=new[:7], reason="its checks are still running on GitHub")
            remember(waiting=new)

    heal_drift(lab, old)


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another cycle is still running", flush=True)
            return 0
        try:
            cycle()
        except subprocess.CalledProcessError as error:
            print(f"{' '.join(error.cmd)} failed, trying again next minute: {error.stderr.strip()}", flush=True)
            return 1
        remember(checked=datetime.now(timezone.utc).isoformat(timespec="seconds"), running=git("rev-parse", "HEAD"))
    return 0
