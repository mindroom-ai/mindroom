"""Canaries for every normal-output sink used by the Docker release smoke."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from mindroom.constants import RuntimePaths
from mindroom.logging_config import get_logger, setup_logging
from tests.manual import minimal_agent_worker_smoke as smoke


@pytest.mark.parametrize(
    "sink",
    ["worker", "primary", "primary-console", "presentation", "trace", "child-presentation", "child-trace"],
)
def test_smoke_capture_detects_canary_in_previously_missed_sink(tmp_path: Path, sink: str) -> None:
    """A leak in any sink fails even when other outputs contain no secrets."""
    audit = smoke.CaptureAudit(tmp_path, ["synthetic-secret-canary"])
    if sink == "worker":
        removed = []

        def logs(**kwargs: object) -> bytes:
            assert kwargs == {"stdout": True, "stderr": True}
            assert not removed
            return b"synthetic-secret-canary\n"

        container = SimpleNamespace(name="own-worker", id="worker-id", logs=logs)
        smoke.capture_worker_before_removal(audit, container, lambda: removed.append(True))
        assert removed == [True]
    elif sink == "primary":
        paths = RuntimePaths(
            config_path=tmp_path / "config.yaml",
            config_dir=tmp_path,
            env_path=tmp_path / ".env",
            storage_root=tmp_path / "state",
        )
        setup_logging(runtime_paths=paths)
        get_logger("mindroom.ai").info("runtime output synthetic-secret-canary")
        audit.capture_primary(paths.storage_root / "logs")
    elif sink == "primary-console":
        audit.capture("primary-console", "primary-process-stdout-stderr", "synthetic-secret-canary")
    else:
        audit.capture_response(
            "minimal",
            "synthetic-secret-canary" if sink.endswith("presentation") else "safe response",
            [{"rendered_trace": "synthetic-secret-canary"}] if sink.endswith("trace") else [],
            entity_name="code" if sink.startswith("child") else "helper",
        )
        audit.capture_response("standard", "safe later response", [])
    with pytest.raises(AssertionError, match="Secret detected"):
        audit.verify()


def test_worker_capture_failure_still_performs_original_removal(tmp_path: Path) -> None:
    """A failed log read cannot skip the original worker retirement."""
    audit = smoke.CaptureAudit(tmp_path, [])
    removed = []

    def fail(**_kwargs: object) -> None:
        message = "log read failed"
        raise RuntimeError(message)

    container = SimpleNamespace(name="own-worker", id="worker-id", logs=fail)
    smoke.capture_worker_before_removal(audit, container, lambda: removed.append(True))
    assert removed == [True]
    with pytest.raises(ExceptionGroup, match="capture"):
        audit.verify()


def test_smoke_capture_requires_observed_execution_sinks(tmp_path: Path) -> None:
    """Empty captures never satisfy the release evidence requirement."""
    audit = smoke.CaptureAudit(tmp_path, [])
    with pytest.raises(AssertionError, match="Missing"):
        audit.verify()


@pytest.mark.parametrize("failure", ["logs", "remove", "both"])
def test_cleanup_continues_after_one_container_failure(tmp_path: Path, failure: str) -> None:
    """Owned peers retire after a failure; unrelated containers remain untouched."""
    audit = smoke.CaptureAudit(tmp_path, [])
    live = []
    attempted = []

    def broken() -> None:
        message = "synthetic cleanup failure"
        raise RuntimeError(message)

    for name in ("own-first", "other-container", "own-second"):
        container = SimpleNamespace(name=name, id=name)

        def logs(*, container: SimpleNamespace = container) -> bytes:
            if container.name == "own-first" and failure in {"logs", "both"}:
                broken()
            return b"safe logs"

        def remove(*, force: bool, container: SimpleNamespace = container) -> None:
            assert force
            attempted.append(container.name)
            if container.name == "own-first" and failure in {"remove", "both"}:
                broken()
            live.remove(container)

        container.logs, container.remove = logs, remove
        live.append(container)
    client = SimpleNamespace(containers=SimpleNamespace(list=lambda **_kwargs: list(live)))
    assert smoke.cleanup_containers(client, "own-", audit) is (failure == "logs")
    assert attempted == ["own-first", "own-second"]
    assert any(container.name == "other-container" for container in live)
    assert len(audit.errors) == (2 if failure == "both" else 1)
