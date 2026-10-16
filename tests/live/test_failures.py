"""Failure tests: break the running fabric on purpose, prove it keeps working within the time the
intent allows, then repair it and prove it is whole again.

Runs after test_fabric.py, on the same booted fabric. Whatever a test breaks is repaired even when
the test fails, and the next test only starts once the fabric is fully healthy again.
"""
import re
import subprocess
import time

import pytest

from fabctl.lab import (ALLOWED, BORDERS, DEVICES, INTENT, LAB, LEAVES, SPINES, SWITCHES, address,
                      answers_within, docker, eventually, expected_sessions, healthy_problems,
                      in_parallel, isp_problems, leaf_route_problems, report, run, vtysh_json)

BFD = INTENT["fabric"]["bfd"]
DETECT_S = BFD["interval_ms"] * BFD["multiplier"] / 1000
SWITCH_S = 0.7            # after detection: BGP withdraws the paths and every switch installs the rest
BUDGET_S = DETECT_S + SWITCH_S
BUDGET = f"{BUDGET_S:.1f} s (BFD {BFD['interval_ms']} ms x {BFD['multiplier']} + {SWITCH_S} s)"
RATE_S = 0.05             # one ping every 50 ms on every flow, so an outage is measured to 50 ms
HEAL_S = 90               # how long a repaired fabric may take to be whole again
SPINE, BORDER = sorted(SPINES)[0], sorted(BORDERS)[0]


@pytest.fixture
def repairs():
    """A test lists here how to undo what it breaks. Undone afterwards even if the test failed, and
    the fabric must then be whole again before anything else runs."""
    undo = []
    yield undo
    for step in reversed(undo):
        step()
    found = eventually(healthy_problems, HEAL_S)
    assert not found, report("The fabric did not heal after the repair", found)


def cables(node, state):
    """Every cable of a switch down or up at once, as if it lost power to its ports."""
    ports = [p["name"] for p in DEVICES[node]["ports"]]
    if DEVICES[node]["role"] == "border":
        ports.append(DEVICES[node]["isp"]["port"])
    run(node, "sh", "-c", " && ".join(f"ip link set {p} {state}" for p in ports))


class Traffic:
    """Continuous pings on many flows at once, started before a failure and read after it."""

    def __init__(self, pairs, seconds=8):
        self.flows = {pair: subprocess.Popen(
            ["sudo", "docker", "exec", f"clab-{LAB}-{pair[0]}",
             "ping", "-i", str(RATE_S), "-w", str(seconds), "-W", "1", address(pair[1])],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) for pair in pairs}

    def problems(self):
        """Flows whose longest run of lost packets is longer than the budget."""
        found = []
        for (src, dst), flow in self.flows.items():
            out = flow.communicate(timeout=60)[0]
            sent = int((re.search(r"(\d+) packets transmitted", out) or [0, 0])[1])
            got = {int(seq) for seq in re.findall(r"icmp_seq=(\d+)", out)}
            longest = gap = 0
            for seq in range(1, sent + 1):
                gap = 0 if seq in got else gap + 1
                longest = max(longest, gap)
            if not got:
                found.append(f"{src} -> {dst}: no answer at all ({sent} sent)")
            elif longest * RATE_S > BUDGET_S:
                found.append(f"{src} -> {dst}: no answer for {longest * RATE_S:.2f} s, the intent allows {BUDGET}")
        return found


def noticed_after(node, port, since, limit_s=15):
    """Seconds from `since` until node's BGP session on port stops being Established, or None."""
    while time.time() - since < limit_s:
        state = vtysh_json(node, "show bgp summary json").get("ipv4Unicast", {}).get("peers", {}).get(port, {}).get("state")
        if state and state != "Established":
            return time.time() - since
    return None


def test_a_frozen_spine_is_dropped_within_the_bfd_budget(repairs):
    """docker pause freezes every process of the spine while its cables stay up: the failure that is
    hardest to see. Only BFD notices it; without BFD, the BGP hold timer would take 9 s."""
    watchers = [(n, port) for n in SWITCHES for (vrf, port), peer in expected_sessions(n).items() if peer == SPINE]
    docker("pause", SPINE)
    since = time.time()
    repairs.append(lambda: docker("unpause", SPINE))
    noticed = in_parallel(lambda n, port: noticed_after(n, port, since), watchers)
    slow = [f"{n} noticed {SPINE} after {t:.2f} s, the intent allows {BUDGET}" if t is not None
            else f"{n} did not notice {SPINE} at all within 15 s" for (n, _), t in noticed.items()
            if t is None or t > BUDGET_S]
    assert not slow, report(f"{SPINE} frozen", slow)

    result = in_parallel(answers_within, ALLOWED)
    failed = [f"{s} -> {d}" for (s, d), ok in result.items() if not ok]
    assert not failed, report(f"With {SPINE} frozen, {len(failed)} allowed pairs get no answer", failed)


def test_traffic_survives_a_spine_losing_every_cable(repairs):
    traffic = Traffic(ALLOWED)
    time.sleep(2)                                   # a clean stretch of traffic before the cut
    cables(SPINE, "down")
    repairs.append(lambda: cables(SPINE, "up"))
    found = traffic.problems()
    assert not found, report(f"While {SPINE} lost every cable", found)


def test_the_internet_survives_a_border_going_down(repairs):
    traffic = Traffic(ALLOWED)
    time.sleep(2)
    cables(BORDER, "down")
    repairs.append(lambda: cables(BORDER, "up"))
    found = traffic.problems()
    assert not found, report(f"While {BORDER} went down", found)

    alive = [b for b in BORDERS if b != BORDER]
    found = eventually(lambda: isp_problems(alive) + [f"{leaf} {p}" for leaf in LEAVES
                                                      for p in leaf_route_problems(leaf, alive)])
    assert not found, report(f"With {BORDER} down, routing must use {', '.join(alive)} only", found)
