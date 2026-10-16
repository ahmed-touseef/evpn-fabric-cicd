"""Live tests of the healthy fabric that lab/deploy.sh booted on this machine.

Every expectation comes from intent/fabric.yml (see fabctl/lab.py): the running network is what is
being checked, the intent is what it must obey.
"""
from pathlib import Path

import pytest

from fabctl import frr
from fabctl.lab import (ALLOWED, BORDERS, DEVICES, FORBIDDEN, LEAVES, SPINES, SWITCHES, answers,
                      answers_within, bfd_problems, config_problems, ecmp_problems, eventually,
                      in_parallel, isp_problems, leaf_route_problems, report, session_problems,
                      run, vni_problems)


# ---- Every router runs exactly what Git says -----------------------------------------------------

@pytest.mark.parametrize("node", SWITCHES)
def test_every_router_runs_exactly_its_rendered_config(node):
    """frr-reload works out what a reload of the rendered config would still change on the running
    router: it must be nothing. Otherwise a deploy would send commands nobody wrote (an earlier
    template made frr-reload switch BFD off), and the server's drift check would alarm forever."""
    found = eventually(lambda: config_problems(node))
    assert not found, report(f"{node} running config differs from rendered/{node}/frr.conf", found)


@pytest.mark.parametrize("node", SWITCHES)
def test_a_reload_of_the_rendered_config_works_and_changes_nothing(node):
    """The deploy agent changes running routers with frr-reload, so reloading the config a router
    already runs must succeed and leave it as it was. The first real deploy failed right here:
    frr-reload tried to save into /etc/frr, which is read only on purpose, and reported failure."""
    wanted = (Path("rendered") / node / "frr.conf").read_text()
    applied, why = frr.reload(lambda *command, stdin=None: run(node, *command, stdin=stdin), wanted)
    assert applied, report(f"{node}: frr-reload failed", why)
    found = eventually(lambda: config_problems(node))
    assert not found, report(f"{node} differs from rendered/{node}/frr.conf after the reload", found)


# ---- Control plane -------------------------------------------------------------------------------

@pytest.mark.parametrize("node", SWITCHES)
def test_every_bgp_session_is_up_to_the_right_neighbour(node):
    """Also proves the cabling: the session on eth11 of spine1 must lead to leaf1, and so on."""
    found = eventually(lambda: session_problems(node))
    assert not found, report(f"{node} BGP sessions", found)


@pytest.mark.parametrize("node", SPINES + LEAVES + BORDERS)
def test_bfd_watches_every_fabric_link(node):
    """BFD up on every cable between spines and leaves, with the timers of the intent: it is what
    turns a dead neighbour into a rerouted packet in well under a second instead of 9 s."""
    found = eventually(lambda: bfd_problems(node))
    assert not found, report(f"{node} BFD", found)


@pytest.mark.parametrize("node", LEAVES + BORDERS)
def test_every_switch_spreads_traffic_over_every_spine(node):
    """Every leaf and border reaches every other one through all spines at once (ECMP), so losing
    a spine only removes a path, never the destination."""
    found = eventually(lambda: ecmp_problems(node))
    assert not found, report(f"{node} paths through the spines", found)


@pytest.mark.parametrize("leaf", LEAVES)
def test_every_vni_knows_every_other_leaf_that_carries_it(leaf):
    found = eventually(lambda: vni_problems(leaf))
    assert not found, report(f"{leaf} EVPN", found)


# ---- Routing -------------------------------------------------------------------------------------

@pytest.mark.parametrize("leaf", LEAVES)
def test_leaves_learn_only_their_tenant_and_the_default_route(leaf):
    found = eventually(lambda: leaf_route_problems(leaf))
    assert not found, report(f"{leaf} routing tables", found)


def test_isp_learns_exactly_the_summaries_from_every_border():
    found = eventually(isp_problems)
    assert not found, report("What the ISP learns from us", found)


# ---- Traffic: every pair of servers, both ways ---------------------------------------------------

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
