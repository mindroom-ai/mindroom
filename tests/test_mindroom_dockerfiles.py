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
_PLATFORM_FRONTEND_DOCKERFILE = _REPO_ROOT / "saas-platform/Dockerfile.platform-frontend"
_SANDBOX_RUNNER_SCRIPT = _REPO_ROOT / "run-sandbox-runner.sh"


def _apt_install_packages(dockerfile_text: str) -> set[str]:
    match = re.search(r"apt-get install -y (?P<packages>.*?)\\\s*&&", dockerfile_text, re.DOTALL)
    assert match is not None
    return {package for package in match.group("packages").split() if not package.startswith("-") and package != "\\"}


def _assert_command_starts_with_tini(template_text: str, command: str) -> None:
    escaped_command = re.escape(command)
    block_list_pattern = rf"command:\s*\n\s*-\s*tini\s*\n\s*-\s*--\s*\n\s*-\s*{escaped_command}(?=\s)"
    inline_list_pattern = rf"command:\s*\[\s*\"tini\"\s*,\s*\"--\"\s*,\s*\"{escaped_command}\""

    assert re.search(block_list_pattern, template_text) or re.search(inline_list_pattern, template_text)


def test_apt_install_packages_ignores_flags_and_line_continuations() -> None:
    """Dockerfile package parsing should tolerate multiline apt install formatting."""
    text = """RUN apt-get update && apt-get install -y --no-install-recommends \\
    bash \\
    git \\
    && apt-get clean
"""

    assert _apt_install_packages(text) == {"bash", "git"}


def test_mindroom_runtime_images_run_under_tini() -> None:
    """MindRoom containers need an init process to reap orphaned subprocesses."""
    for dockerfile in _MINDROOM_DOCKERFILES:
        text = dockerfile.read_text(encoding="utf-8")

        assert "tini" in _apt_install_packages(text)
        assert 'ENTRYPOINT ["tini", "--"]' in text
        assert 'CMD ["/app/.venv/bin/mindroom", "run"]' in text


def test_mindroom_runtime_images_opt_into_dashboard_asset_build() -> None:
    """The runtime image should ship dashboard assets without runtime Bun."""
    for dockerfile in _MINDROOM_DOCKERFILES:
        text = dockerfile.read_text(encoding="utf-8")
        frontend_copy_index = text.index("COPY frontend /app/frontend")
        final_stage_index = text.index(" AS final")
        builder_after_frontend = text[frontend_copy_index:final_stage_index]
        frontend_build_env_index = builder_after_frontend.index("ENV MINDROOM_BUILD_FRONTEND=1")
        project_install_index = builder_after_frontend.index("uv sync --locked --no-dev")

        assert frontend_build_env_index < project_install_index
        assert "MINDROOM_BUILD_FRONTEND=1" in builder_after_frontend
        assert "PUPPETEER_SKIP_DOWNLOAD=true" in builder_after_frontend
        assert "--reinstall-package mindroom" in builder_after_frontend
        assert "rm -rf frontend/node_modules" in builder_after_frontend
        assert "bun install --frozen-lockfile" not in text


def test_full_mindroom_runtime_image_bundles_browser_runtime_packages() -> None:
    """The full runtime image should support browser and media-capable worker tools."""
    text = (_REPO_ROOT / "local/instances/deploy/Dockerfile.mindroom").read_text(encoding="utf-8")
    packages = _apt_install_packages(text)

    assert {"chromium", "ffmpeg", "fonts-liberation", "nodejs"} <= packages


def test_mindroom_runtime_images_keep_git_ssh_support_when_disabling_recommends() -> None:
    """Git-over-SSH needs an explicit SSH client when apt recommendations are disabled."""
    for dockerfile in _MINDROOM_DOCKERFILES:
        text = dockerfile.read_text(encoding="utf-8")
        install_args_match = re.search(r"apt-get install -y (?P<args>.*?)\\\s*&&", text, re.DOTALL)
        assert install_args_match is not None
        install_args = install_args_match.group("args").split()

        if "--no-install-recommends" in install_args:
            assert "openssh-client" in _apt_install_packages(text)


def test_kubernetes_command_overrides_run_under_tini() -> None:
    """Kubernetes command overrides bypass image entrypoints, so add tini explicitly."""
    runtime_template = _RUNTIME_DEPLOYMENT_TEMPLATE.read_text(encoding="utf-8")
    instance_helpers = _INSTANCE_HELPERS_TEMPLATE.read_text(encoding="utf-8")

    _assert_command_starts_with_tini(runtime_template, "/app/.venv/bin/mindroom")
    _assert_command_starts_with_tini(runtime_template, "/app/run-sandbox-runner.sh")
    _assert_command_starts_with_tini(instance_helpers, "/app/run-sandbox-runner.sh")


def test_sandbox_runner_script_imports_existing_public_runtime_serializer() -> None:
    """The sandbox sidecar startup script should import the actual constants helper."""
    text = _SANDBOX_RUNNER_SCRIPT.read_text(encoding="utf-8")

    assert "from mindroom.constants import resolve_primary_runtime_paths, write_startup_manifest" in text


def test_sandbox_runner_script_writes_startup_manifest_expected_by_app() -> None:
    """The sandbox sidecar app boots from a startup manifest path, not raw runtime JSON."""
    text = _SANDBOX_RUNNER_SCRIPT.read_text(encoding="utf-8")

    assert "MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH" in text
    assert 'startup_manifest_path="$(' in text
    assert 'export MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH="${startup_manifest_path}"' in text
    assert 'export MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH="$(' not in text
    assert "MINDROOM_RUNTIME_PATHS_JSON" not in text


def test_platform_backend_kubectl_matches_target_architecture() -> None:
    """The SaaS provisioner image must work on both amd64 and arm64 clusters."""
    text = _PLATFORM_BACKEND_DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG TARGETARCH=amd64" in text
    assert "linux/${TARGETARCH}/kubectl" in text
    assert "linux/amd64/kubectl" not in text


def test_platform_frontend_skips_puppeteer_browser_download() -> None:
    """Production frontend builds should not download the dev screenshot browser."""
    text = _PLATFORM_FRONTEND_DOCKERFILE.read_text(encoding="utf-8")

    assert "PUPPETEER_SKIP_DOWNLOAD=true" in text
