"""Packs the rendered configs into a Batfish snapshot.

Batfish reads a Linux switch running FRR in the "Cumulus concatenated" layout: one file
per device holding its hostname, its interfaces (ifupdown2 syntax), its ports file and
its frr.conf, in that order. The frr.conf part is the exact file the router runs, minus
the few lines listed in UNMODELLED: Batfish's FRR grammar does not model them, and none
of them changes which routes a router accepts or sends. Every other line must be
understood by Batfish, or the pipeline fails.
"""
import json
import re
from pathlib import Path

from . import model

# (pattern, why it is safe to leave out of the analysis)
UNMODELLED = [
    (r"^\s*exit$", "end of block marker written by FRR 8 and later"),
    (r"^\s+vni \d+$", "start of an explicit route target block for one VNI"),
    (r"^\s+exit-vni$", "end of that block"),
    (r"^\s+route-target (import|export) \S+$",
     "explicit route target: Batfish already matches EVPN routes on the VNI alone, which is what these values do"),
    (r"^\s*no bgp ebgp-requires-policy$", "simulated ISP only: it accepts everything on purpose"),
]
BFD_BLOCK = "BFD timers: liveness detection, not part of the routing model"


def frr_view(text):
    """Returns (the frr.conf text Batfish analyses, [(line, reason) left out])."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("frr version"))
    kept, dropped, in_bfd, in_vrf_block = [], [], False, False
    for line in lines[start:]:
        if line == "bfd":
            in_bfd = True
        if in_bfd:
            dropped.append((line, BFD_BLOCK))
            in_bfd = line != "exit"
            continue
        # "vni N" inside a "vrf" block maps the VRF to its L3VNI, which Batfish does model
        if line.startswith("vrf "):
            in_vrf_block = True
        elif line == "exit-vrf":
            in_vrf_block = False
        reason = None if in_vrf_block else next((why for pattern, why in UNMODELLED if re.match(pattern, line)), None)
        if reason:
            dropped.append((line, reason))
        else:
            kept.append(line)
    return "\n".join(kept) + "\n", dropped


def interfaces(facts):
    """The interfaces section: the loopback, every cabled port, the VRFs and the ISP link."""
    out = ["auto lo", "iface lo inet loopback",
           f"    address {facts.get('loopback') or facts['internet_host']}/32", ""]
    vrfs = [t["name"] for t in facts.get("tenants", [])]
    if facts.get("tenant"):
        vrfs.append(facts["tenant"]["name"])
    for vrf in vrfs:
        out += [f"auto {vrf}", f"iface {vrf}", "    vrf-table auto", ""]
    for port in facts["ports"]:
        out += [f"auto {port['name']}", f"iface {port['name']}"]
        if "address" in port:                       # the ISP's own ports
            out.append(f"    address {port['address']}")
        out.append("")
    if facts.get("tenant"):                         # a border's link to the ISP
        isp = facts["isp"]
        out += [f"auto {isp['port']}", f"iface {isp['port']}", f"    address {isp['address']}",
                f"    vrf {facts['tenant']['name']}", ""]
    return "\n".join(out)


def snapshot(intent, rendered="rendered", out="build/batfish"):
    """Writes the snapshot and returns {device: [(line, reason) left out]}."""
    out = Path(out)
    (out / "configs").mkdir(parents=True, exist_ok=True)
    (out / "batfish").mkdir(parents=True, exist_ok=True)
    devices = {n: f for n, f in model.devices(intent).items() if f["role"] != "host"}
    report = {}
    for name, facts in devices.items():
        frr, dropped = frr_view((Path(rendered) / name / "frr.conf").read_text())
        report[name] = dropped
        (out / "configs" / f"{name}.cfg").write_text(
            f"{name}\n# This file describes the network interfaces\n{interfaces(facts)}\n"
            f"# ports.conf --\n{frr}")
    # Physical cabling, so Batfish can pair up the unnumbered BGP sessions
    edges = []
    for c in model.cables(intent):
        if c["a"] in devices and c["b"] in devices:
            a = {"hostname": c["a"], "interfaceName": c["a_port"]}
            b = {"hostname": c["b"], "interfaceName": c["b_port"]}
            edges += [{"node1": a, "node2": b}, {"node1": b, "node2": a}]
    (out / "batfish" / "layer1_topology.json").write_text(json.dumps({"edges": edges}, indent=1) + "\n")
    return report
