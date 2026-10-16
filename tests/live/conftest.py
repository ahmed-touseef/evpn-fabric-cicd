"""Shared by every live test: wait once for the freshly booted fabric before the first check."""
import time

import pytest

from fabctl.lab import BOOT_S, SWITCHES, established, expected_sessions, in_parallel


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
