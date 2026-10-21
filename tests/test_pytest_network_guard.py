"""Tests for the pytest guard that refuses network dials."""

import threading
from concurrent.futures import ThreadPoolExecutor

import httpcore
import pytest

from tests import conftest


def _dial_from_thread(host: str, dials: list[conftest._RefusedDial]) -> None:
    """Dial ``host`` from a background thread, as work leaked from a finished test would."""
    with ThreadPoolExecutor(thread_name_prefix="leaked") as executor:
        future = executor.submit(conftest._refuse_dial_off_the_test, host, 443, httpcore.ConnectError, dials)
    with pytest.raises(httpcore.ConnectError, match=f"network disabled in tests: {host}:443"):
        future.result()


def test_dials_are_reported_by_the_test_that_saw_them_or_at_session_end() -> None:
    """A dial outside every test's window is kept for the session report instead of being dropped."""
    dials: list[conftest._RefusedDial] = []
    first_test_start = len(dials)
    assert conftest._report_test_dials(dials, first_test_start, "MainThread") is None

    _dial_from_thread("between.example", dials)
    second_test_start = len(dials)
    _dial_from_thread("during.example", dials)
    conftest._refuse_dial_off_the_test("localhost", 8080, httpcore.ConnectError, dials)
    failure = conftest._report_test_dials(dials, second_test_start, "MainThread")
    _dial_from_thread("after.example", dials)

    assert failure == (
        "test tried to reach the network: during.example:443 (thread leaked_0); a dial from a thread other than"
        " the test's own (MainThread) may come from an earlier test's background work"
    )
    assert conftest._unreported_dials_failure(dials) == (
        "background work tried to reach the network between tests or after the last test: "
        "after.example:443 (thread leaked_0), between.example:443 (thread leaked_0)"
    )


def test_a_dial_from_the_test_thread_names_no_other_thread() -> None:
    """The background-work hint appears only when another thread dialed."""
    dials: list[conftest._RefusedDial] = []
    test_thread = threading.current_thread().name
    with pytest.raises(httpcore.ConnectError):
        conftest._refuse_dial_off_the_test("127.0.0.1", 11434, httpcore.ConnectError, dials)

    assert conftest._report_test_dials(dials, 0, test_thread) == (
        f"test tried to reach the network: 127.0.0.1:11434 (thread {test_thread})"
    )
    assert conftest._unreported_dials_failure(dials) is None
