"""Checks for MindRoom container process reaping defaults."""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MINDROOM_DOCKERFILES = (
    _REPO_ROOT / "local/instances/deploy/Dockerfile.mindroom",
    _REPO_ROOT / "local/instances/deploy/Dockerfile.mindroom-minimal",
)
_RUNTIME_DEPLOYMENT_TEMPLATE = _REPO_ROOT / "cluster/k8s/runtime/templates/deployment.yaml"
_INSTANCE_HELPERS_TEMPLATE = _REPO_ROOT / "cluster/k8s/instance/templates/_helpers.tpl"


def test_mindroom_runtime_images_run_under_tini() -> None:
    """MindRoom containers need an init process to reap orphaned subprocesses."""
    for dockerfile in _MINDROOM_DOCKERFILES:
        text = dockerfile.read_text(encoding="utf-8")
        one_line_text = " ".join(text.split())

        assert "apt-get install -y bash bzip2 curl git git-lfs jq nano procps ripgrep tini tmux unzip" in one_line_text
        assert 'ENTRYPOINT ["tini", "--"]' in text
        assert 'CMD ["/app/.venv/bin/mindroom", "run"]' in text


def test_kubernetes_command_overrides_run_under_tini() -> None:
    """Kubernetes command overrides bypass image entrypoints, so add tini explicitly."""
    runtime_template = _RUNTIME_DEPLOYMENT_TEMPLATE.read_text(encoding="utf-8")
    instance_helpers = _INSTANCE_HELPERS_TEMPLATE.read_text(encoding="utf-8")

    assert "command:\n            - tini\n            - --\n            - /app/.venv/bin/mindroom" in runtime_template
    assert (
        "command:\n            - tini\n            - --\n            - /app/run-sandbox-runner.sh" in runtime_template
    )
    assert 'command: ["tini", "--", "/app/run-sandbox-runner.sh"]' in instance_helpers
