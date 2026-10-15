"""Static analysis with Batfish: proves properties of the generated configs before anything boots.

Every expectation comes from intent/fabric.yml, never from the configs: the configs are what
is being checked, the intent is what they must obey.
Needs a Batfish server on localhost:9996 and a snapshot built by: python3 -m fabctl snapshot
"""
import re
import sys
import time

import pytest
from pybatfish.client.session import Session
from pybatfish.datamodel import BgpRoute, BgpRouteConstraints

from fabctl import intent as intent_file

# What Batfish does not model in this network, and which layer covers it instead.
KNOWN_PARSE_GAPS = {
    "Route maps in 'advertise ipv4 unicast' are not supported":
        "DEFAULT-ONLY's content is proven below; its effect on the leaves is checked by the live tests",
}
KNOWN_MODEL_GAPS = [
    (r"^vni \d+ for vrf \S+ does not exist$",
     "VXLAN tunnels are built by Linux (interfaces.sh), outside Batfish's view; the live tests cover them"),
]


@pytest.fixture(scope="session")
def intent():
    return intent_file.load()


@pytest.fixture(scope="session")
def q():
    """A Batfish session on the snapshot. Batfish needs a minute to start next to the job."""
    deadline = time.time() + 300
    while True:
        try:
            bf = Session(host="localhost")
            break
        except Exception as e:
            if time.time() > deadline:
                sys.exit(f"Batfish did not answer within 300 s: {e}")
            time.sleep(5)
    bf.set_network("fabric")
    bf.init_snapshot("build/batfish", name="candidate", overwrite=True)
    return bf.q


def nodes(names):
    return ",".join(sorted(names))


def show(frame, columns):
    return "\n" + frame[columns].to_string(index=False)


# ---- Batfish must understand the configs, or nothing below means anything --------------------

def test_every_config_is_understood(q):
    status = q.fileParseStatus().answer().frame()
    bad = status[status["Status"] != "PASSED"]
    assert bad.empty, "Batfish could not fully read:" + show(bad, ["File_Name", "Status"])


def test_only_known_lines_are_unsupported(q):
    warnings = q.parseWarning().answer().frame()
    unknown = warnings[~warnings["Comment"].isin(KNOWN_PARSE_GAPS)]
    assert unknown.empty, "Lines Batfish does not support:" + show(unknown, ["Filename", "Line", "Text", "Comment"])


def test_only_known_model_gaps(q):
    issues = q.initIssues().answer().frame()

    def known(row):
        if row["Type"] == "Parse warning":
            return row["Details"] in KNOWN_PARSE_GAPS
        return any(re.match(pattern, str(row["Details"])) for pattern, _ in KNOWN_MODEL_GAPS)

    unknown = issues[~issues.apply(known, axis=1)] if len(issues) else issues
    assert unknown.empty, "Problems while building the model:" + show(unknown, ["Nodes", "Type", "Details"])


def test_no_name_is_used_without_being_defined(q):
    """Catches a typo such as route-map TO-ISPP out: FRR accepts it silently and filters nothing."""
    refs = q.undefinedReferences().answer().frame()
    assert refs.empty, "Used but never defined:" + refs.to_string(index=False)


def test_nothing_is_defined_without_being_used(q):
    """A filter that is defined but not applied to any session protects nothing."""
    unused = q.unusedStructures().answer().frame()
    assert unused.empty, "Defined but never used:" + unused.to_string(index=False)


# ---- Sessions ----------------------------------------------------------------------------------

def test_every_bgp_session_comes_up(q, intent):
    sessions = q.bgpSessionCompatibility().answer().frame()
    spines, below = intent["spines"]["devices"], len(intent["leaves"]) + len(intent["borders"])
    expected = 2 * len(spines) * below + 2 * len(intent["borders"])     # both ends of every session
    broken = sessions[sessions["Configured_Status"] != "UNIQUE_MATCH"]
    assert broken.empty, "Sessions that would not come up:" + show(
        broken, ["Node", "VRF", "Remote_Node", "Remote_IP", "Configured_Status"])
    assert len(sessions) == expected, f"{len(sessions)} session ends found, the intent implies {expected}"


# ---- Routing policy: proven for every possible route, not just examples -------------------------

def policies(intent):
    """(policy, nodes it must be on, the only prefixes it may ever permit, why)."""
    fabric = list(intent["leaves"]) + list(intent["borders"]) + list(intent["spines"]["devices"])
    borders = list(intent["borders"])
    summaries = sorted(intent["isp"]["announce"].values())
    loopbacks = intent["fabric"]["loopbacks"]
    return [
        ("TO-ISP", borders, summaries, "only the summaries in isp.announce may be sent to the ISP"),
        ("FROM-ISP", borders, ["0.0.0.0/0"], "only a default route may be accepted from the ISP"),
        ("DEFAULT-ONLY", borders, ["0.0.0.0/0"], "only the default route may be passed into the fabric"),
        ("LOOPBACKS-ONLY", fabric, [f"{loopbacks}:32-32"], "only switch addresses may enter the underlay"),
    ]


POLICY_NAMES = ["TO-ISP", "FROM-ISP", "DEFAULT-ONLY", "LOOPBACKS-ONLY"]


@pytest.mark.parametrize("name", POLICY_NAMES)
def test_policy_exists_where_it_must(q, intent, name):
    """Without this, a deleted or renamed filter would make the proof below pass on nothing."""
    _, where, _, _ = next(p for p in policies(intent) if p[0] == name)
    found = q.namedStructures(nodes=nodes(where), structureTypes="Routing_Policy", structureNames=name)
    have = set(found.answer().frame()["Node"])
    assert have == set(where), f"{name} is missing on: {sorted(set(where) - have)}"


@pytest.mark.parametrize("name", POLICY_NAMES)
def test_policy_permits_nothing_else(q, intent, name):
    _, where, allowed, why = next(p for p in policies(intent) if p[0] == name)
    leaks = q.searchRoutePolicies(
        nodes=nodes(where), policies=name, action="permit",
        inputConstraints=BgpRouteConstraints(prefix=allowed, complementPrefix=True)).answer().frame()
    examples = "" if leaks.empty else "\n" + "\n".join(
        f"  {row['Node']}: {row['Input_Route'].network} would be permitted" for _, row in leaks.iterrows())
    assert leaks.empty, f"{name}: {why}, but Batfish found routes it lets through:{examples}"


def test_isp_gets_exactly_the_summaries(q, intent):
    """The other side of the proof: the summaries themselves must not be blocked."""
    summaries = sorted(intent["isp"]["announce"].values())
    routes = [BgpRoute(network=p, originatorIp="0.0.0.0", originType="igp", protocol="bgp") for p in summaries]
    result = q.testRoutePolicies(nodes=nodes(intent["borders"]), policies="TO-ISP", direction="out",
                                 inputRoutes=routes).answer().frame()
    denied = result[result["Action"] != "PERMIT"]
    assert len(result) == len(routes) * len(intent["borders"]) and denied.empty, \
        "TO-ISP blocks a summary it must announce:" + show(denied, ["Node", "Action"])
