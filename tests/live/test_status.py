"""The collector behind the status page, run on the healthy fabric: the page must show a healthy
fabric as healthy, and show every cable, session and server pair the way the intent describes them.

Runs last, after the failure tests have repaired everything, through the same entry point that
fabric-status.service uses on the server.
"""
import json

from fabctl import status
from fabctl.lab import ALLOWED, BORDERS, HOSTS, LEAVES, SPINES, SWITCHES, eventually, report


def test_the_status_page_shows_the_healthy_fabric_as_healthy(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "OUT", tmp_path / "status.json")
    page = {}

    def problems():
        status.main()
        page.update(json.loads(status.OUT.read_text()))
        return page["problems"] + [f"cable {c['a']} {c['a_port']} to {c['b']} {c['b_port']}: {c['state']}"
                                   for c in page["cables"] if c["state"] != "up"]

    found = eventually(problems)
    assert not found, report("The status page would show", found)

    assert [r["name"] for r in page["routers"]] == SWITCHES
    assert all(r["answers"] and r["sessions"]["up"] == r["sessions"]["want"] for r in page["routers"])
    cables = len(LEAVES + BORDERS) * len(SPINES) + len(BORDERS) + len(HOSTS)
    assert len(page["cables"]) == cables, f"{len(page['cables'])} cables on the page, the intent has {cables}"
    assert {(p["from"], p["to"]) for p in page["reach"] if p["answers"]} == set(ALLOWED)
    assert all(d["via"] == sorted(BORDERS) for d in page["edge"]["default_routes"])
    received = {r["prefix"]: r["from"] for r in page["edge"]["received"]}
    assert received == {prefix: sorted(BORDERS) for prefix in page["edge"]["announce"].values()}
