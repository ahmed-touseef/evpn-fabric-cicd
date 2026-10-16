"""Day two operations, rehearsed on the live fabric exactly the way the deploy agent performs them.

Runs after test_fabric.py. Whatever it changes, it puts back, and checks that it did.
"""
from pathlib import Path

from fabctl import frr
from fabctl.lab import (INTENT, LEAVES, SWITCHES, config_problems, eventually, report, run, session_problems,
                        vtysh_json)


def drops(node):
    """How often each BGP session of a router has gone down since it booted."""
    peers = vtysh_json(node, "show bgp summary json").get("ipv4Unicast", {}).get("peers", {})
    return {peer: state.get("connectionsDropped", 0) for peer, state in peers.items()}


def test_a_config_change_goes_out_and_back_without_dropping_a_session():
    """A real change on a leaf, applied with frr-reload as the agent does: a description added and
    the BFD timers changed, then the rendered config restored. Both directions must apply cleanly,
    the router must run exactly what it was sent, and no BGP session may go down on the way."""
    node = LEAVES[0]
    execute = lambda *command, stdin=None: run(node, *command, stdin=stdin)
    rendered = (Path("rendered") / node / "frr.conf").read_text()
    interval = INTENT["fabric"]["bfd"]["interval_ms"]
    other = 450 if interval != 450 else 500                # FRR hides 300, its default, so never use it
    changed = rendered.replace(" neighbor FABRIC remote-as external\n",
                               " neighbor FABRIC remote-as external\n neighbor FABRIC description deploy rehearsal\n")
    for timer in ("transmit-interval", "receive-interval"):
        changed = changed.replace(f"  {timer} {interval}\n", f"  {timer} {other}\n")
    assert changed != rendered, "nothing to change in the rendered config"
    before = drops(node)

    applied, why = frr.reload(execute, changed)
    assert applied, report(f"{node}: frr-reload of the change failed", why)
    assert frr.changes(execute, changed) == ([], []), f"{node} does not run exactly the config it was sent"

    applied, why = frr.reload(execute, rendered)
    assert applied, report(f"{node}: frr-reload back to the rendered config failed", why)
    found = eventually(lambda: config_problems(node) + [f"{n} {p}" for n in SWITCHES for p in session_problems(n)])
    assert not found, report(f"{node} after the change was undone", found)

    after = drops(node)
    dropped = [f"{peer}: went down {after[peer] - count} time(s)" for peer, count in before.items()
               if after.get(peer, count) != count]
    assert not dropped, report(f"{node} lost BGP sessions during the change", dropped)
