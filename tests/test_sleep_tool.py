"""Sleep pauses stay within their cap."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from agno.tools import sleep as agno_sleep

from mindroom.custom_tools.sleep import SleepTools

if TYPE_CHECKING:
    import pytest


def test_sleep_refuses_durations_outside_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Durations past 300 seconds or below zero return an error without holding a thread."""
    slept: list[float] = []
    monkeypatch.setattr(agno_sleep, "time", SimpleNamespace(sleep=slept.append))
    tools = SleepTools()

    refused = [tools.sleep(10**9), tools.sleep(-1)]
    allowed = tools.sleep(300)

    assert all("between 0 and 300" in result for result in refused)
    assert allowed == "Slept for 300 seconds"
    assert slept == [300]
