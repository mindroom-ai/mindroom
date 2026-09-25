"""Tests for the local instance sandbox isolation smoke script."""

from __future__ import annotations

import pytest

from scripts import smoke_local_instance_sandbox


@pytest.mark.parametrize("output", ["", "mindroom:8765 closed", "mindroom:8765 closed\nother:1 closed"])
def test_tcp_reachability_rejects_probe_output_for_other_targets(
    monkeypatch: pytest.MonkeyPatch,
    output: str,
) -> None:
    """A failed or partial probe must not pass the all-closed isolation check vacuously."""
    monkeypatch.setattr(smoke_local_instance_sandbox, "exec_python", lambda *_args, **_kwargs: output)

    with pytest.raises(RuntimeError, match="probe reported"):
        smoke_local_instance_sandbox.tcp_reachability([], "sandbox-runner", ["mindroom:8765", "postgres:5432"])


def test_tcp_reachability_reports_each_requested_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """A complete probe maps every requested target to whether it accepted the connection."""
    output = "mindroom:8765 closed\npostgres:5432 open"
    monkeypatch.setattr(smoke_local_instance_sandbox, "exec_python", lambda *_args, **_kwargs: output)

    reachability = smoke_local_instance_sandbox.tcp_reachability([], "mindroom", ["mindroom:8765", "postgres:5432"])

    assert reachability == {"mindroom:8765": False, "postgres:5432": True}
