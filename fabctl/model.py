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


def devices(intent):
    """Everything the renderer has to produce, as {device name: facts}."""
    return dict(spines(intent))
