"""Tests for the pytest guard that refuses network dials."""

import asyncio
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import httpcore
import httpx
import httpx2
import pytest

from tests import conftest

# RFC 5737 TEST-NET-1 is never routed, so a dial the guard misses fails without reaching anyone.
_TEST_NET_URL = "http://192.0.2.1/"
_REFUSED_TEST_NET_DIAL = re.escape("network disabled in tests: 192.0.2.1:80")


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


@pytest.mark.parametrize(
    ("host", "port", "refused"),
    [
        ("::1", 8080, False),
        ("[::1]", 8080, False),
        ("0.0.0.0", 8080, False),  # noqa: S104
        ("foo.localhost", 8080, False),
        ("127.0.0.2", 11434, True),
        ("10.0.0.1", 8080, True),
    ],
)
def test_only_local_hosts_off_the_ollama_port_stay_reachable(host: str, port: int, *, refused: bool) -> None:
    """Loopback and unspecified IP literals and localhost names are local, and port 11434 is refused on every host."""
    assert conftest._dial_leaves_the_test(host, port) is refused


def test_a_refused_ipv6_dial_names_a_bracketed_address() -> None:
    """An IPv6 host is bracketed so that its port reads apart from the address."""
    dials: list[conftest._RefusedDial] = []
    with pytest.raises(httpcore.ConnectError, match=re.escape("network disabled in tests: [::1]:11434")):
        conftest._refuse_dial_off_the_test("::1", 11434, httpcore.ConnectError, dials)

    assert [dial.address for dial in dials] == ["[::1]:11434"]


async def _dial_test_net_asynchronously() -> None:
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(httpx.ConnectError, match=_REFUSED_TEST_NET_DIAL):
            await client.get(_TEST_NET_URL)
    async with httpx2.AsyncClient(trust_env=False) as client:
        with pytest.raises(httpx2.ConnectError, match=_REFUSED_TEST_NET_DIAL):
            await client.get(_TEST_NET_URL)


def test_every_patched_http_client_backend_is_refused(request: pytest.FixtureRequest) -> None:
    """Dials through httpx and httpx2, sync and async, reach the session guard instead of the network."""
    dials: list[conftest._RefusedDial] = request.getfixturevalue("_refused_dials")
    start = len(dials)

    with httpx.Client(trust_env=False) as client, pytest.raises(httpx.ConnectError, match=_REFUSED_TEST_NET_DIAL):
        client.get(_TEST_NET_URL)
    with httpx2.Client(trust_env=False) as client, pytest.raises(httpx2.ConnectError, match=_REFUSED_TEST_NET_DIAL):
        client.get(_TEST_NET_URL)
    asyncio.run(_dial_test_net_asynchronously())

    test_thread = threading.current_thread().name
    assert dials[start:] == [conftest._RefusedDial("192.0.2.1:80", test_thread) for _ in range(4)]
    # These refusals are the expected outcome, so keep them out of this test's network report.
    del dials[start:]
