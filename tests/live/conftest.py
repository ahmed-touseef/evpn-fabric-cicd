"""Shared by every live test: wait once for the freshly booted fabric before the first check,
and report what the failure tests measured once the run is over."""
import json
import os
import time
from pathlib import Path

import pytest

from fabctl.lab import BOOT_S, MEASURED, SWITCHES, established, expected_sessions, in_parallel


@pytest.fixture(scope="session", autouse=True)
def booted():
    """Waits until every BGP session of the fabric is up, so that each check only has to allow a
    few seconds of settling. Fails nothing itself: the checks say what is missing."""
    total = sum(len(expected_sessions(n)) for n in SWITCHES)
    end = time.time() + BOOT_S
    while time.time() < end:
        up = in_parallel(established, [(n,) for n in SWITCHES])
        if sum(len(sessions) for sessions in up.values()) >= total:
            return
        time.sleep(1)


def pytest_terminal_summary(terminalreporter):
    if MEASURED:
        terminalreporter.section("measured on this fabric")
        for name, value in MEASURED.items():
            terminalreporter.write_line(f"{name}: {value}")


def pytest_sessionfinish(session, exitstatus):
    if MEASURED and os.environ.get("FABRIC_REPORT"):
        Path(os.environ["FABRIC_REPORT"]).write_text(json.dumps(MEASURED, indent=1) + "\n")
