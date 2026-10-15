"""Reads intent/fabric.yml and checks it for mistakes, before anything is generated.

Every check below catches a mistake that would otherwise show up much later,
as a broken tunnel or a missing route in the running network.
"""
import ipaddress
import re

import yaml

UPLINKS = {"eth1", "eth2"}               # leaf and border ports cabled to spine1 and spine2
PRIVATE_ASN = range(64512, 65535)         # 64512 to 65534 (RFC 6996)
MAX_VNI = 2**24 - 1                       # VXLAN carries a 24 bit network identifier
SECTIONS = ("fabric", "spines", "leaves", "borders", "tenants", "hosts", "isp")


class StrictLoader(yaml.SafeLoader):
    """Plain YAML silently keeps the last of two identical keys, so a copy pasted
    host line would replace the first one without a word. This loader refuses it."""

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.MarkedYAMLError(problem=f"'{key}' is written twice",
                                           problem_mark=key_node.start_mark)
            seen.add(key)
        return super().construct_mapping(node, deep)


def load(path="intent/fabric.yml"):
    with open(path) as f:
        return yaml.load(f, Loader=StrictLoader)


def check(intent):
    """Returns a list of problems. An empty list means the intent is consistent."""
    missing = [s for s in SECTIONS if s not in (intent or {})]
    if missing:
        return [f"missing section(s): {', '.join(missing)}"]

    errors = []
    err = errors.append
    fabric, isp, tenants, hosts = intent["fabric"], intent["isp"], intent["tenants"], intent["hosts"]
    spines, leaves, borders = intent["spines"]["devices"], intent["leaves"], intent["borders"]

    # Switches: unique names and IDs, and every ID must fit in the loopback range
    names = [*spines, *leaves, *borders, *hosts]
    for name in sorted({n for n in names if names.count(n) > 1}):
        err(f"name '{name}' is used twice")
    switches = {**spines, **leaves, **borders}
    loopbacks = ipaddress.ip_network(fabric["loopbacks"])
    by_id = {}
    for name, sw in switches.items():
        if sw["id"] in by_id:
            err(f"{name}: id {sw['id']} is already used by {by_id[sw['id']]}")
        by_id[sw["id"]] = name
        if not 0 < sw["id"] < loopbacks.num_addresses - 1:
            err(f"{name}: id {sw['id']} does not fit in the loopback range {loopbacks}")

    # BGP numbers: spines share one, every leaf and border has its own
    spine_asn = intent["spines"]["asn"]
    by_asn = {}
    for name, sw in {**leaves, **borders}.items():
        if sw["asn"] in by_asn:
            err(f"{name}: asn {sw['asn']} is already used by {by_asn[sw['asn']]}")
        by_asn[sw["asn"]] = name
        if sw["asn"] == spine_asn:
            err(f"{name}: asn {sw['asn']} is the spines' asn")
    for name, asn in [("spines", spine_asn)] + [(n, s["asn"]) for n, s in {**leaves, **borders}.items()]:
        if asn not in PRIVATE_ASN:
            err(f"{name}: asn {asn} is not a private asn (64512 to 65534)")
    if isp["asn"] in PRIVATE_ASN or isp["asn"] == spine_asn:
        err(f"isp: asn {isp['asn']} must not be one of ours")

    # Tenants: VLAN and VNI numbers unique everywhere, subnets never overlapping
    vlans, vnis, subnets = {}, {}, {}
    for t, tenant in tenants.items():
        vnis.setdefault(tenant["l3vni"], []).append(f"tenant {t} l3vni")
        for vid, vlan in tenant["vlans"].items():
            if vid in vlans:
                err(f"VLAN {vid} is in both {vlans[vid][0]} and {t}")
            if not 1 < vid < 4095:
                err(f"VLAN {vid}: must be between 2 and 4094")
            vlans[vid] = (t, vlan)
            vnis.setdefault(vlan["vni"], []).append(f"VLAN {vid}")
            subnets[f"VLAN {vid}"] = ipaddress.ip_network(vlan["subnet"])
    for vni, users in vnis.items():
        if len(users) > 1:
            err(f"VNI {vni} is used by {' and '.join(users)}")
        if not 0 < vni <= MAX_VNI:
            err(f"VNI {vni} ({users[0]}): must be between 1 and {MAX_VNI}")
    subnets["loopbacks"] = loopbacks
    for border, link in isp["links"].items():
        subnets[f"isp link {border}"] = ipaddress.ip_network(link)
    items = list(subnets.items())
    for i, (a, net_a) in enumerate(items):
        for b, net_b in items[i + 1:]:
            if net_a.overlaps(net_b):
                err(f"{a} ({net_a}) overlaps {b} ({net_b})")

    # Servers: on a real leaf, on a free port, inside their VLAN's subnet
    ports, addresses = {}, {}
    for name, host in hosts.items():
        if host["leaf"] not in leaves:
            err(f"host {name}: leaf '{host['leaf']}' does not exist")
        if not re.fullmatch(r"eth\d+", host["port"]) or host["port"] in UPLINKS:
            err(f"host {name}: port {host['port']} is not a free server port (eth3 and up)")
        port = (host["leaf"], host["port"])
        if port in ports:
            err(f"host {name}: {host['leaf']} {host['port']} is already used by {ports[port]}")
        ports[port] = name
        if host["vlan"] not in vlans:
            err(f"host {name}: VLAN {host['vlan']} does not exist")
            continue
        addr = ipaddress.ip_address(host["address"])
        net = subnets[f"VLAN {host['vlan']}"]
        if addr not in net:
            err(f"host {name}: {addr} is not in VLAN {host['vlan']} ({net})")
        elif addr in (net.network_address, net.broadcast_address, net.network_address + 1):
            err(f"host {name}: {addr} is reserved in {net} (network, broadcast or gateway .1)")
        if addr in addresses:
            err(f"host {name}: {addr} is already used by {addresses[addr]}")
        addresses[addr] = name

    # Internet edge: one /31 per border, and only internet tenants are announced
    if set(isp["links"]) != set(borders):
        err(f"isp links {sorted(isp['links'])} do not match the borders {sorted(borders)}")
    for border, link in isp["links"].items():
        if ipaddress.ip_network(link).prefixlen != 31:
            err(f"isp link {border}: {link} must be a /31")
    for t, tenant in tenants.items():
        announced = isp["announce"].get(t)
        if tenant["internet"] and announced is None:
            err(f"tenant {t} has internet but nothing to announce in isp.announce")
        if not tenant["internet"] and announced is not None:
            err(f"tenant {t} has no internet but is announced to the isp")
        if announced is not None:
            summary = ipaddress.ip_network(announced)
            for vid, vlan in tenant["vlans"].items():
                if not ipaddress.ip_network(vlan["subnet"]).subnet_of(summary):
                    err(f"tenant {t}: VLAN {vid} ({vlan['subnet']}) is outside its announced {summary}")
    for t in isp["announce"]:
        if t not in tenants:
            err(f"isp.announce: tenant '{t}' does not exist")

    if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", str(fabric["anycast_gateway_mac"])):
        err(f"anycast_gateway_mac {fabric['anycast_gateway_mac']} is not a MAC address")
    return errors
