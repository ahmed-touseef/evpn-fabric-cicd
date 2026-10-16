"""The deploy agent's decisions, tested in the pipeline before the agent ever runs on the server:
which commits it accepts, and how it applies a change."""
from fabctl.agent import REQUIRED, plan, verdict


def run(name, conclusion="success", status="completed", id=1):
    return {"name": name, "status": status, "conclusion": conclusion, "id": id}


def test_every_required_check_green_is_approved():
    assert verdict([run(name) for name in REQUIRED]) == "approved"


def test_one_red_check_is_rejected():
    assert verdict([run("Intent and configs"), run("Batfish", "failure"), run("Live lab")]) == "rejected"


def test_a_check_still_running_or_missing_means_waiting():
    assert verdict([run("Intent and configs"), run("Batfish"), run("Live lab", None, "in_progress")]) == "waiting"
    assert verdict([run("Intent and configs"), run("Batfish")]) == "waiting"


def test_a_rerun_that_passed_replaces_the_failure():
    runs = [run("Live lab", "failure", id=1), run("Live lab", id=2), run("Batfish"), run("Intent and configs")]
    assert verdict(runs) == "approved"


def test_a_cancelled_or_skipped_check_is_not_a_pass():
    assert verdict([run("Intent and configs"), run("Batfish"), run("Live lab", "cancelled")]) == "rejected"
    assert verdict([run("Intent and configs"), run("Batfish"), run("Live lab", "skipped")]) == "rejected"


ROLES = {"leaf1": "leaf", "leaf2": "leaf", "border1": "border", "spine1": "spine", "isp": "isp", "web1": "host"}


def test_config_only_changes_are_reloaded_leaves_first():
    changed = ["rendered/spine1/frr.conf", "rendered/leaf2/frr.conf", "rendered/border1/frr.conf",
               "rendered/leaf1/frr.conf", "fabctl/templates/leaf.conf.j2"]
    assert plan(changed, ROLES) == ("reload", ["leaf1", "leaf2", "border1", "spine1"])


def test_anything_else_in_the_network_rebuilds_the_lab():
    assert plan(["rendered/leaf1/frr.conf", "rendered/leaf1/interfaces.sh"], ROLES) == ("rebuild", [])
    assert plan(["rendered/topology.clab.yml"], ROLES) == ("rebuild", [])
    assert plan(["rendered/web4/interfaces.sh"], ROLES) == ("rebuild", [])          # a new server


def test_changes_outside_the_network_need_no_deploy():
    assert plan(["README.md", "tests/live/test_fabric.py", ".github/workflows/pipeline.yml"], ROLES) == ("none", [])
