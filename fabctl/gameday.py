"""Game day: every night the failure tests run again, on the production lab of this server.

The pipeline proves on GitHub's machine that the fabric survives a frozen spine, a spine losing
every cable and a border going down. A clean run there says the design works; it says nothing
about this server tonight, with its load, its kernel and whatever ran since the last deploy. So
fabric-gameday.timer breaks the real lab on purpose every night and keeps the numbers: how fast
BFD noticed, and the longest stretch of lost pings. The status page shows them.

It holds the deploy agent's lock for the whole run, so no deploy and no drift repair can start
while failures are being injected. The tests repair what they break. Afterwards every container
is unfrozen and every port brought up once more anyway, and if the fabric is still not whole the
lab is rebuilt from the running commit, so a failed game day never leaves a broken network.
"""
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

from . import agent

TESTS = "tests/live/test_failures.py"
WAIT_S = 15 * 60          # how long to wait for a deploy in progress before giving up for tonight
TESTS_MAX_S = 25 * 60     # the three tests take about 4 minutes; fabric-gameday.service allows 40
HEAL_S = 120
RESULT = agent.STATE / "gameday.json"
MEASURED = agent.STATE / "measured.json"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save(**values):
    """gameday.json, replaced in one step so the status collector never reads half a file."""
    new = RESULT.with_name(".gameday.json.tmp")
    new.write_text(json.dumps(values, indent=1) + "\n")
    new.replace(RESULT)


def restore(lab):
    """Unfreezes every router and brings every port up. Does nothing to a lab that is already whole."""
    for node in lab.SWITCHES:
        subprocess.run([*lab.SUDO, "docker", "unpause", f"clab-{lab.LAB}-{node}"], capture_output=True)
        facts = lab.DEVICES[node]
        ports = [p["name"] for p in facts["ports"]] + ([facts["isp"]["port"]] if facts["role"] == "border" else [])
        lab.run(node, "sh", "-c", " && ".join(f"ip link set {p} up" for p in ports))


def reasons(lines):
    """The failed tests and what each one said, from pytest's output."""
    said, inside = [], False
    for line in lines:
        if line.startswith("E   "):
            said.append(line[4:].strip())
            inside = True
        elif inside and line.startswith(" " * 8):
            said.append("  " + line.strip())
        else:
            inside = False
    return [line for line in lines if line.startswith(("FAILED", "ERROR"))] + said[:30]


def wait_for_the_agent(lock):
    end = time.time() + WAIT_S
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.time() > end:
                return False
            time.sleep(5)


def main():
    agent.STATE.mkdir(parents=True, exist_ok=True)
    with open(agent.STATE / "lock", "w") as lock:
        if not wait_for_the_agent(lock):
            agent.log("game day skipped", reason=f"the deploy agent was still busy after {WAIT_S // 60} minutes")
            return 1
        commit, started = agent.git("rev-parse", "--short", "HEAD"), time.time()
        save(running=True, started=now(), commit=commit)
        MEASURED.unlink(missing_ok=True)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))    # stopped by systemd: repair all the same
        try:
            result = subprocess.run([sys.executable, "-m", "pytest", TESTS, "-q", "--tb=line", "-p", "no:cacheprovider"],
                                    capture_output=True, text=True, timeout=TESTS_MAX_S,
                                    env={**os.environ, "FABRIC_REPORT": str(MEASURED)})
            lines, ran = result.stdout.strip().splitlines(), result.returncode == 0
        except subprocess.TimeoutExpired:
            lines, ran = [f"the tests were stopped after {TESTS_MAX_S // 60} minutes"], False
        except SystemExit:
            lines, ran = ["the game day was stopped before the tests finished"], False
        finally:
            lab = agent.probes()
            restore(lab)
        failures = reasons(lines)

        left = lab.eventually(lab.healthy_problems, HEAL_S)
        if left:
            whole = agent.rebuild()[0] and agent.live_tests()[0]
            failures += ([f"the fabric was not whole {HEAL_S} s after the repair:"] + [f"  {p}" for p in left[:10]] +
                         [f"so the lab was rebuilt from {commit}: " + ("healthy again" if whole else "STILL NOT HEALTHY")])

        try:
            measured = json.loads(MEASURED.read_text())
        except (OSError, ValueError):
            measured = {}
        passed = ran and not left
        summary = lines[-1] if lines else "pytest printed nothing"
        save(running=False, time=now(), commit=commit, passed=passed, seconds=round(time.time() - started),
             summary=summary, measured=measured, failures=failures)
        agent.log("game day", commit=commit, passed=passed, tests=summary, detail=failures)
    return 0 if passed else 1
