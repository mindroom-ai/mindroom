"""Checks for MindRoom container process reaping defaults."""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MINDROOM_DOCKERFILES = (
    _REPO_ROOT / "local/instances/deploy/Dockerfile.mindroom",
    _REPO_ROOT / "local/instances/deploy/Dockerfile.mindroom-minimal",
)
_RUNTIME_DEPLOYMENT_TEMPLATE = _REPO_ROOT / "cluster/k8s/runtime/templates/deployment.yaml"
_INSTANCE_HELPERS_TEMPLATE = _REPO_ROOT / "cluster/k8s/instance/templates/_helpers.tpl"
_PLATFORM_BACKEND_DOCKERFILE = _REPO_ROOT / "saas-platform/Dockerfile.platform-backend"


def _apt_install_packages(dockerfile_text: str) -> set[str]:
    match = re.search(r"apt-get install -y (?P<packages>.*?)\\\s*&&", dockerfile_text, re.DOTALL)
    assert match is not None
    return set(match.group("packages").split())


def _assert_command_starts_with_tini(template_text: str, command: str) -> None:
    escaped_command = re.escape(command)
    block_list_pattern = rf"command:\s*\n\s*-\s*tini\s*\n\s*-\s*--\s*\n\s*-\s*{escaped_command}(?=\s)"
    inline_list_pattern = rf"command:\s*\[\s*\"tini\"\s*,\s*\"--\"\s*,\s*\"{escaped_command}\""

    assert re.search(block_list_pattern, template_text) or re.search(inline_list_pattern, template_text)


def test_mindroom_runtime_images_run_under_tini() -> None:
    """MindRoom containers need an init process to reap orphaned subprocesses."""
    for dockerfile in _MINDROOM_DOCKERFILES:
        text = dockerfile.read_text(encoding="utf-8")

        assert "tini" in _apt_install_packages(text)
        assert 'ENTRYPOINT ["tini", "--"]' in text
        assert 'CMD ["/app/.venv/bin/mindroom", "run"]' in text


def test_kubernetes_command_overrides_run_under_tini() -> None:
    """Kubernetes command overrides bypass image entrypoints, so add tini explicitly."""
    runtime_template = _RUNTIME_DEPLOYMENT_TEMPLATE.read_text(encoding="utf-8")
    instance_helpers = _INSTANCE_HELPERS_TEMPLATE.read_text(encoding="utf-8")

    _assert_command_starts_with_tini(runtime_template, "/app/.venv/bin/mindroom")
    _assert_command_starts_with_tini(runtime_template, "/app/run-sandbox-runner.sh")
    _assert_command_starts_with_tini(instance_helpers, "/app/run-sandbox-runner.sh")


def test_platform_backend_kubectl_matches_target_architecture() -> None:
    """The SaaS provisioner image must work on both amd64 and arm64 clusters."""
    text = _PLATFORM_BACKEND_DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG TARGETARCH" in text
    assert "linux/${TARGETARCH}/kubectl" in text
    assert "linux/amd64/kubectl" not in text
