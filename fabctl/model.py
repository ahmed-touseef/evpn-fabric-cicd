"""Turns the intent into the facts each device needs: its addresses, its ports and
who is on the other end of every cable. Templates only read these facts; all the
calculating happens here, once."""
import ipaddress


def loopback(intent, switch_id):
    """Every switch's own address: the loopback range plus its id (leaf1, id 11 = 10.255.0.11)."""
    return str(ipaddress.ip_network(intent["fabric"]["loopbacks"])[switch_id])


def spines(intent):
    """One entry per spine. Spine port eth<N> goes to the leaf or border with id N."""
    below = {**intent["leaves"], **intent["borders"]}
    for name, spine in intent["spines"]["devices"].items():
        yield name, {
            "role": "spine",
            "name": name,
            "asn": intent["spines"]["asn"],
            "loopback": loopback(intent, spine["id"]),
            "loopbacks": intent["fabric"]["loopbacks"],
            "bfd": intent["fabric"]["bfd"],
            "ports": [{"name": f"eth{sw['id']}", "peer": peer}
                      for peer, sw in sorted(below.items(), key=lambda kv: kv[1]["id"])],
        }


def uplinks(intent):
    """Leaf and border port eth<N> goes to the spine with id N."""
    return [{"name": f"eth{spine['id']}", "peer": name}
            for name, spine in sorted(intent["spines"]["devices"].items(), key=lambda kv: kv[1]["id"])]


def leaves(intent):
    """One entry per leaf, with only the VLANs and tenants its servers actually need."""
    vlan_tenant = {vid: t for t, tenant in intent["tenants"].items() for vid in tenant["vlans"]}
    rt = intent["fabric"]["route_target_asn"]
    for name, leaf in intent["leaves"].items():
        servers = {h: host for h, host in intent["hosts"].items() if host["leaf"] == name}
        tenants = {}
        for vid in sorted({host["vlan"] for host in servers.values()}):
            t = vlan_tenant[vid]
            vlan = intent["tenants"][t]["vlans"][vid]
            subnet = ipaddress.ip_network(vlan["subnet"])
            tenants.setdefault(t, {"name": t, "l3vni": intent["tenants"][t]["l3vni"], "vlans": []})
            tenants[t]["vlans"].append({"id": vid, "name": vlan["name"], "vni": vlan["vni"],
                                        "gateway": f"{subnet[1]}/{subnet.prefixlen}"})
        yield name, {
            "role": "leaf",
            "name": name,
            "asn": leaf["asn"],
            "loopback": loopback(intent, leaf["id"]),
            "loopbacks": intent["fabric"]["loopbacks"],
            "bfd": intent["fabric"]["bfd"],
            "rt": rt,
            "ports": uplinks(intent),
            "tenants": [tenants[t] for t in sorted(tenants)],
            "anycast_mac": intent["fabric"]["anycast_gateway_mac"],
            "servers": [{"name": h, "port": host["port"], "vlan": host["vlan"]}
                        for h, host in sorted(servers.items(), key=lambda kv: int(kv[1]["port"][3:]))],
        }


def borders(intent):
    """One entry per border. A border carries only the tenant with internet access:
    its uplink to the ISP lives inside that tenant's VRF."""
    online = [t for t, tenant in intent["tenants"].items() if tenant["internet"]]
    tenant = None
    if online:
        t = online[0]
        tenant = {"name": t, "l3vni": intent["tenants"][t]["l3vni"], "summary": intent["isp"]["announce"][t]}
    for name, border in intent["borders"].items():
        link = ipaddress.ip_network(intent["isp"]["links"][name])
        yield name, {
            "role": "border",
            "name": name,
            "asn": border["asn"],
            "loopback": loopback(intent, border["id"]),
            "loopbacks": intent["fabric"]["loopbacks"],
            "bfd": intent["fabric"]["bfd"],
            "rt": intent["fabric"]["route_target_asn"],
            "ports": uplinks(intent),
            "tenant": tenant,
            "isp": {"port": "eth3", "address": f"{link[0]}/31", "peer": str(link[1]), "asn": intent["isp"]["asn"]},
        }


def isp(intent):
    """The provider's router. ISP port eth<N> goes to the border with id N; on each /31
    the border takes the first address and the ISP the second."""
    ports = []
    for border, link in intent["isp"]["links"].items():
        net = ipaddress.ip_network(link)
        ports.append({"name": f"eth{intent['borders'][border]['id']}", "peer": border,
                      "address": f"{net[1]}/31", "peer_address": str(net[0]),
                      "peer_asn": intent["borders"][border]["asn"]})
    yield "isp", {"role": "isp", "name": "isp", "asn": intent["isp"]["asn"],
                  "internet_host": intent["isp"]["internet_host"], "ports": ports}


def hosts(intent):
    """One entry per server: its address, and the anycast gateway of its VLAN (always the .1)."""
    vlans = {vid: (t, vlan) for t, tenant in intent["tenants"].items() for vid, vlan in tenant["vlans"].items()}
    for name, host in intent["hosts"].items():
        tenant, vlan = vlans[host["vlan"]]
        subnet = ipaddress.ip_network(vlan["subnet"])
        yield name, {"role": "host", "name": name, "tenant": tenant, "vlan": host["vlan"], "leaf": host["leaf"],
                     "address": f"{host['address']}/{subnet.prefixlen}", "gateway": str(subnet[1])}


def devices(intent):
    """Everything the renderer has to produce, as {device name: facts}."""
    return {**dict(spines(intent)), **dict(leaves(intent)), **dict(borders(intent)),
            **dict(isp(intent)), **dict(hosts(intent))}
