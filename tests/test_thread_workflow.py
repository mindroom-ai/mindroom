"""Tests for the thread workflow groups that matrix_room shares with MindRoom Chat."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mindroom.thread_workflow import classify_thread_workflow

_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "thread-workflow-cases.json").read_text(encoding="utf-8"),
)["cases"]


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["name"])
def test_shared_workflow_case(case: dict[str, Any]) -> None:
    """Each shared case classifies as MindRoom Chat classifies it."""
    assert classify_thread_workflow(now=case["now"], **case["status"]) == case["expected"]
