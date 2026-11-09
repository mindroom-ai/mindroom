"""Test tool dependency resolution, auto-install logic, and pyproject sync."""

from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from importlib import metadata as importlib_metadata
from importlib import util as importlib_util
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system import dependencies
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolFileAccess, ToolMetadata, ToolStatus
from mindroom.tool_system.dependencies import (
    _PIP_TO_IMPORT,
    _auto_install_optional_extra,
    _install_optional_extras,
    _install_via_uv_sync,
    _install_via_uv_tool,
    _pip_name_to_import,
    auto_install_enabled,
    auto_install_optional_extra_for_import_retry,
    check_deps_installed,
    ensure_optional_deps,
    install_command_for_current_python,
    optional_dependency_environment,
)
from mindroom.tool_system.google_workspaces import GoogleWorkspaceConfig, register_google_workspace_tools
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.registry import builtin_tool_registry, compose_tool_registry, plugin_module_registrations
from tests.conftest import register_test_tool

_BUILTINS = builtin_tool_registry()

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.constants import RuntimePaths

REPO_ROOT = Path(__file__).parent.parent
HOOK_SCRIPT = REPO_ROOT / ".github" / "scripts" / "check_tool_extras_sync.py"


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Return a runtime whose config and storage live under the test's own directory, never the checkout."""
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "mindroom_data")


def _base_dependency_names() -> set[str]:
    """Return normalized base dependency names declared in pyproject.toml."""
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    return {canonicalize_name(Requirement(dependency).name) for dependency in pyproject["project"]["dependencies"]}


_OPENBB_FIRST_CALL = textwrap.dedent(
    """
    from concurrent.futures import ThreadPoolExecutor

    from openbb_core.app.model.obbject import OBBject
    from openbb_core.provider.abstract.data import Data

    from mindroom.tools.openbb import openbb_tools

    # Agents are built off the event loop, in a worker thread.
    with ThreadPoolExecutor(1) as pool:
        toolkit = pool.submit(lambda: openbb_tools()()).result()
    assert hasattr(toolkit.obb, "equity"), "OpenBB command package was not built"
    # The toolkit converts every result to polars, which needs pyarrow for text columns.
    quotes = OBBject(results=[Data(symbol="AAPL", name="Apple"), Data(symbol="MSFT", name="Microsoft")])
    print(quotes.to_polars().shape)
    """,
)


def test_openbb_tool_builds_on_first_use_in_concurrent_processes(tmp_path: Path) -> None:
    """Processes that load the OpenBB tool together from worker threads on a fresh install all reach its commands."""
    installed = importlib_util.find_spec("openbb")
    if installed is None or installed.origin is None:
        pytest.skip("openbb extra is not installed")
    # A copy of only the package's entry module is an OpenBB install that has never been built.
    fresh_package = tmp_path / "openbb"
    fresh_package.mkdir()
    shutil.copy(installed.origin, fresh_package / "__init__.py")
    env = {key: value for key, value in os.environ.items() if key != "OPENBB_AUTO_BUILD"}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(tmp_path), env.get("PYTHONPATH")]))
    # Processes sharing one install can have different temporary directories, as worker tool subprocesses do.
    processes: list[subprocess.Popen[str]] = []
    try:
        for index in range(2):
            temp_dir = tmp_path / f"tmp-{index}"
            temp_dir.mkdir()
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", _OPENBB_FIRST_CALL],
                    env={**env, "TMPDIR": str(temp_dir)},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                ),
            )
        outputs = [process.communicate(timeout=300)[0] for process in processes]
    finally:
        for process in processes:
            process.kill()
            process.wait()

    assert [process.returncode for process in processes] == [0, 0], outputs
    assert all(output.rstrip().endswith("(2, 2)") for output in outputs), outputs


def test_tool_extras_in_sync_with_pyproject() -> None:
    """Run the pre-commit hook script to verify tool registrations match pyproject.toml.

    This reuses the single source of truth (.github/scripts/check_tool_extras_sync.py)
    rather than reimplementing the check, ensuring CI catches sync issues even though
    pre-commit hooks don't run in CI.
    """
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        pytest.fail(f"Tool extras out of sync with pyproject.toml:\n{output}")


def test_core_runtime_imports_are_declared_as_base_dependencies() -> None:
    """Core runtime modules should not rely on transitive dependencies."""
    base_dependencies = _base_dependency_names()
    required_runtime_dependencies = {
        "src/mindroom/constants.py": "pydantic-settings",
        "src/mindroom/matrix/client_session.py": "aiohttp",
        "src/mindroom/mcp/transports.py": "anyio",
        # Agno is installed without its provider extras, so each provider SDK is a base dependency.
        "src/mindroom/providers/anthropic/messages.py": "anthropic",
        "src/mindroom/providers/gemini/adapter.py": "google-genai",
        "src/mindroom/providers/ollama/adapter.py": "ollama",
        "src/mindroom/providers/openai/build.py": "openai",
    }

    for module_path, dependency_name in required_runtime_dependencies.items():
        assert (REPO_ROOT / module_path).is_file(), f"{module_path} does not exist"
        assert dependency_name in base_dependencies, (
            f"{module_path} imports {dependency_name!r} directly and it should be declared in project.dependencies"
        )


def test_website_tool_registry_import_does_not_require_website_extra() -> None:
    """Registry imports should not import optional website implementation dependencies."""
    script = """
    import importlib.abc
    import sys


    class BlockWebsiteImplementation(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "mindroom.custom_tools.website" or fullname.startswith("bs4"):
                raise ImportError(f"blocked optional website dependency: {fullname}")
            return None


    sys.meta_path.insert(0, BlockWebsiteImplementation())
    import mindroom.tools
    import mindroom.tools.website

    assert "mindroom.custom_tools.website" not in sys.modules
    print("ok")
    """

    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr


def test_vertexai_claude_google_auth_is_declared_as_base_dependency() -> None:
    """Vertex Claude model loading should not rely on Google tool extras."""
    base_dependencies = _base_dependency_names()

    assert "google-auth" in base_dependencies, (
        "vertexai_claude model loading imports google.auth.load_credentials_from_file directly "
        "and must declare google-auth in project.dependencies"
    )


def test_tools_requiring_config_metadata() -> None:
    """Test that tools marked REQUIRES_CONFIG have config_fields or auth_provider."""
    inconsistent = []

    for tool_name, metadata in _BUILTINS.entries.items():
        if (
            metadata.status == ToolStatus.REQUIRES_CONFIG
            and not metadata.config_fields
            and metadata.auth_provider is None
        ):
            inconsistent.append(tool_name)

    if inconsistent:
        pytest.fail(
            "Tools with REQUIRES_CONFIG but no config_fields or auth_provider:\n"
            + "\n".join(f"  - {name}" for name in sorted(inconsistent)),
        )


def test_get_tool_by_name_retries_after_auto_install(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """Tool loading should retry once after auto-install succeeds."""
    tool_name = "test_auto_install_tool"
    calls = {"count": 0}

    class DummyToolkit(Toolkit):
        name = "dummy"

    def flaky_factory() -> type[DummyToolkit]:
        calls["count"] += 1
        if calls["count"] == 1:
            msg = "missing dependency"
            raise ImportError(msg)
        return DummyToolkit

    register_test_tool(
        monkeypatch,
        ToolMetadata(
            name=tool_name,
            file_access=ToolFileAccess.NONE,
            display_name="Auto Install Test Tool",
            description="Temporary test tool",
            category=ToolCategory.DEVELOPMENT,
            status=ToolStatus.AVAILABLE,
            setup_type=SetupType.NONE,
            config_fields=[],
            dependencies=[],
            factory=flaky_factory,
        ),
    )

    monkeypatch.setattr(
        "mindroom.tool_system.dependencies._auto_install_optional_extra",
        lambda name, install_paths: name == tool_name and install_paths == runtime_paths,
    )
    tool = get_tool_by_name(tool_name, runtime_paths, worker_target=None, tools=builtin_tool_registry())
    assert isinstance(tool, DummyToolkit)
    assert calls["count"] == 2


def test_workspace_tool_installs_the_extra_of_the_builtin_it_copies(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """A workspace copy of a built-in Google tool installs the built-in's extra, before and after a failed import."""
    workspace = GoogleWorkspaceConfig(
        name="secondary",
        display_name="Secondary",
        client_config_service="secondary_google_oauth_client",
        allowed_hosted_domains=("secondary.example",),
        services=("gmail",),
    )
    with plugin_module_registrations("test_workspace_plugin") as registrations:
        register_google_workspace_tools(workspace)
    metadata = registrations["secondary_gmail"]
    assert metadata.factory is not None
    workspace_factory = metadata.factory
    imports = {"count": 0}

    def flaky_factory() -> type:
        imports["count"] += 1
        if imports["count"] == 1:
            msg = "missing dependency"
            raise ImportError(msg)
        return workspace_factory()

    tools = compose_tool_registry({metadata.name: replace(metadata, factory=flaky_factory)}, {})
    requested: list[str] = []

    def install(extra_name: str, _runtime_paths: object) -> bool:
        requested.append(extra_name)
        return True

    monkeypatch.setattr(dependencies, "check_deps_installed", lambda _dependencies: False)
    monkeypatch.setattr(dependencies, "_auto_install_optional_extra", install)

    tool = get_tool_by_name("secondary_gmail", runtime_paths, worker_target=None, tools=tools)

    assert "secondary_get_latest_emails" in tool.functions
    assert requested == ["gmail", "gmail"]


def test_get_tool_by_name_raises_when_auto_install_fails(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """Tool loading should raise ImportError when auto-install cannot help."""
    tool_name = "test_auto_install_failure_tool"

    def failing_factory() -> type:
        msg = "dependency missing forever"
        raise ImportError(msg)

    register_test_tool(
        monkeypatch,
        ToolMetadata(
            name=tool_name,
            file_access=ToolFileAccess.NONE,
            display_name="Auto Install Failure Tool",
            description="Temporary failing tool",
            category=ToolCategory.DEVELOPMENT,
            status=ToolStatus.AVAILABLE,
            setup_type=SetupType.NONE,
            config_fields=[],
            dependencies=[],
            factory=failing_factory,
        ),
    )

    monkeypatch.setattr(
        "mindroom.tool_system.dependencies._auto_install_optional_extra",
        lambda _name, _runtime_paths: False,
    )
    with pytest.raises(ImportError, match="dependency missing forever"):
        get_tool_by_name(tool_name, runtime_paths, worker_target=None, tools=builtin_tool_registry())


def test_auto_install_optional_extra_for_import_retry_invalidates_caches(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """Optional import retries should refresh import caches after installing."""
    installed: list[str] = []
    invalidated: list[bool] = []

    def auto_install(extra_name: str, install_paths: object) -> bool:
        assert install_paths == runtime_paths
        installed.append(extra_name)
        return True

    monkeypatch.setattr("mindroom.tool_system.dependencies._auto_install_optional_extra", auto_install)
    monkeypatch.setattr(
        "mindroom.tool_system.dependencies.importlib.invalidate_caches",
        lambda: invalidated.append(True),
    )

    assert auto_install_optional_extra_for_import_retry("supabase", runtime_paths)

    assert installed == ["supabase"]
    assert invalidated == [True]


def test_auto_install_optional_extra_for_import_retry_skips_cache_invalidation_when_install_fails(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """Failed optional installs should not invalidate import caches before callers raise."""
    invalidated: list[bool] = []

    monkeypatch.setattr("mindroom.tool_system.dependencies._auto_install_optional_extra", lambda *_args: False)
    monkeypatch.setattr(
        "mindroom.tool_system.dependencies.importlib.invalidate_caches",
        lambda: invalidated.append(True),
    )

    assert not auto_install_optional_extra_for_import_retry("supabase", runtime_paths)
    assert invalidated == []


def test_every_builtin_tool_dependency_resolves_to_its_own_installed_module() -> None:
    """With every extra installed, each declared dependency this platform installs passes the construction pre-check, on a module of that package."""
    base_dependencies = _base_dependency_names()
    module_packages = importlib_metadata.packages_distributions()
    wrong: dict[str, list[str]] = {}
    for name, metadata in _BUILTINS.entries.items():
        installed_here = base_dependencies | {
            canonicalize_name(requirement.name) for requirement in dependencies._extra_requirements(name)
        }
        declared = [dep for dep in metadata.dependencies or [] if canonicalize_name(dep) in installed_here]
        foreign = [
            dep
            for dep in declared
            if canonicalize_name(dep)
            not in {
                canonicalize_name(package)
                for package in module_packages.get(_pip_name_to_import(dep).split(".")[0], [])
            }
        ]
        if foreign or not check_deps_installed(declared):
            wrong[name] = declared
    assert wrong == {}


def test_check_deps_installed_positive_and_negative() -> None:
    """check_deps_installed returns True for installed packages, False when any is missing."""
    assert check_deps_installed(["pytest"])
    assert not check_deps_installed(["nonexistent_package_xyz_123"])


@pytest.mark.parametrize("no_auto_install", ["0", "1"])
def test_file_generation_uses_installed_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    no_auto_install: str,
) -> None:
    """Installed file-generation extras must load without requesting an install."""
    pytest.importorskip("docx")
    pytest.importorskip("reportlab")

    def unexpected_install(_extras: list[str], **_kwargs: object) -> bool:
        pytest.fail("Installed file-generation dependencies must not be installed again")

    monkeypatch.setattr("mindroom.tool_system.dependencies._install_optional_extras", unexpected_install)
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_NO_AUTO_INSTALL_TOOLS": no_auto_install},
    )

    toolkit = get_tool_by_name(
        "file_generation",
        runtime_paths,
        tool_config_overrides={"output_directory": str(tmp_path / "output")},
        worker_target=None,
        tools=builtin_tool_registry(),
    )

    assert "generate_docx_file" in toolkit.functions


@pytest.mark.parametrize(("pip_name", "expected_import"), list(_PIP_TO_IMPORT.items()))
def test_pip_to_import_mapping(pip_name: str, expected_import: str) -> None:
    """_pip_name_to_import returns the correct import name for every entry in _PIP_TO_IMPORT."""
    assert _pip_name_to_import(pip_name) == expected_import


def test_pip_to_import_passthrough() -> None:
    """_pip_name_to_import falls back to replacing dashes with underscores."""
    assert _pip_name_to_import("some-normal-package") == "some_normal_package"


def test_pip_to_import_strips_version_specifier() -> None:
    """_pip_name_to_import strips version specifiers before lookup."""
    assert _pip_name_to_import("pyyaml>=6.0") == "yaml"
    assert _pip_name_to_import("requests>=2.0") == "requests"


def test_pip_to_import_mapping_completeness() -> None:
    """Every entry in _PIP_TO_IMPORT should have a key that differs from the naive transform."""
    for pip_name, import_name in _PIP_TO_IMPORT.items():
        naive = pip_name.replace("-", "_")
        assert naive != import_name, (
            f"Mapping entry '{pip_name}' -> '{import_name}' is redundant (naive transform already gives '{naive}')"
        )


def test_install_via_uv_sync_targets_active_virtualenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uv sync should target the active virtualenv when one is in use."""
    captured: dict[str, object] = {}

    def fake_run(
        cmd: list[str],
        *,
        check: bool,
        capture_output: bool,
        cwd: Path,
        env: dict[str, str],
    ) -> SimpleNamespace:
        captured["cmd"] = cmd
        captured["check"] = check
        captured["capture_output"] = capture_output
        captured["cwd"] = cwd
        captured["env"] = env
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("mindroom.tool_system.dependencies._in_virtualenv", lambda: True)
    monkeypatch.setattr("mindroom.tool_system.dependencies.subprocess.run", fake_run)

    assert _install_via_uv_sync(["wikipedia"])
    assert captured["cmd"] == [
        "uv",
        "sync",
        "--locked",
        "--inexact",
        "--no-dev",
        "--active",
        "--python",
        sys.executable,
        "--extra",
        "wikipedia",
        "-q",
    ]
    assert captured["check"] is False
    assert captured["capture_output"] is True
    assert isinstance(captured["cwd"], Path)
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["VIRTUAL_ENV"] == sys.prefix


def test_install_via_uv_tool_extends_running_environment_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uv tool installs must reuse the running interpreter so a failed build cannot delete the environment."""
    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], *, check: bool, **_kwargs: object) -> SimpleNamespace:
        captured["cmd"] = cmd
        captured["check"] = check
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("mindroom.tool_system.dependencies.subprocess.run", fake_run)
    monkeypatch.setattr(sys, "_base_executable", "/managed/cpython-3.13-macos-aarch64-none/bin/python3.13")

    assert _install_via_uv_tool(["browser", "duckduckgo"])
    # `--force` or a bare `--python 3.13` (which may pick another patch or arch) makes uv delete and
    # recreate the environment before building, so a build failure leaves the running install gone.
    assert captured["cmd"] == [
        "uv",
        "tool",
        "install",
        "mindroom[browser,duckduckgo]",
        "--python",
        "/managed/cpython-3.13-macos-aarch64-none/bin/python3.13",
        "-q",
    ]
    assert captured["check"] is False


def test_install_optional_extras_keeps_receipt_extras_for_uv_tool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Uv syncs the tool environment exactly to the request, so installed extras must be requested again."""
    (tmp_path / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "mindroom", extras = ["website", "Duck_Duck_Go"] }]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    requested: list[list[str]] = []

    def fake_install_via_uv_tool(extras: list[str]) -> bool:
        requested.append(extras)
        return True

    monkeypatch.setattr("mindroom.tool_system.dependencies._install_via_uv_tool", fake_install_via_uv_tool)

    assert _install_optional_extras(["browser", "duck-duck-go"])
    assert requested == [["browser", "duck-duck-go", "website"]]


def test_install_command_for_current_python_uses_uv_system_outside_virtualenv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-venv uv installs must target the system interpreter explicitly."""
    monkeypatch.setattr("mindroom.tool_system.dependencies._in_virtualenv", lambda: False)
    monkeypatch.setattr("mindroom.tool_system.dependencies._current_python_has_module", lambda _module_name: False)
    monkeypatch.setattr("mindroom.tool_system.dependencies.shutil.which", lambda _binary: "/usr/bin/uv")

    assert install_command_for_current_python() == [
        "uv",
        "pip",
        "install",
        "--python",
        sys.executable,
        "--system",
    ]


def test_install_command_for_current_python_prefers_current_python_uv_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When PATH lacks uv, the current interpreter should still be able to run `python -m uv`."""
    monkeypatch.setattr("mindroom.tool_system.dependencies._in_virtualenv", lambda: False)
    monkeypatch.setattr(
        "mindroom.tool_system.dependencies._current_python_has_module",
        lambda module_name: module_name == "uv",
    )
    monkeypatch.setattr("mindroom.tool_system.dependencies.shutil.which", lambda _binary: None)

    assert install_command_for_current_python() == [
        sys.executable,
        "-P",
        "-s",
        "-m",
        "uv",
        "pip",
        "install",
        "--python",
        sys.executable,
        "--system",
    ]


def test_install_command_for_current_python_uses_pip_user_outside_virtualenv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-venv pip installs must avoid writing to the managed interpreter directly."""
    monkeypatch.setattr("mindroom.tool_system.dependencies._in_virtualenv", lambda: False)
    monkeypatch.setattr("mindroom.tool_system.dependencies._current_python_has_module", lambda _module_name: False)
    monkeypatch.setattr("mindroom.tool_system.dependencies.shutil.which", lambda _binary: None)

    assert install_command_for_current_python() == [
        sys.executable,
        "-P",
        "-s",
        "-m",
        "pip",
        "install",
        "--user",
    ]


@pytest.mark.parametrize("available_name", ["sentence_transformers", "sentence-transformers"])
def test_auto_install_optional_extra_resolves_non_tool_groups_by_normalized_name(
    monkeypatch: pytest.MonkeyPatch,
    available_name: str,
    runtime_paths: RuntimePaths,
) -> None:
    """A non-tool extra installs through the runtime path, also when installed metadata spells it with hyphens."""
    monkeypatch.setattr("mindroom.tool_system.dependencies.auto_install_enabled", lambda _runtime_paths: True)
    monkeypatch.setattr(
        "mindroom.tool_system.dependencies._available_optional_extras",
        lambda: {available_name},
    )
    monkeypatch.setattr(
        "mindroom.tool_system.dependencies._install_optional_extras",
        lambda extras: extras == [available_name],
    )

    assert _auto_install_optional_extra("sentence_transformers", runtime_paths)


@pytest.fixture
def old_mem0_distribution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Shadow the installed Mem0's metadata with 2.0.1, as an auto-install by an older MindRoom leaves behind."""
    dist_info = tmp_path / "old-site" / "mem0ai-2.0.1.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text("Metadata-Version: 2.1\nName: mem0ai\nVersion: 2.0.1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(dist_info.parent))


@pytest.fixture
def fresh_extra_requirements() -> Iterator[None]:
    """Read the extras and their requirements again in this test and after it."""
    caches = (
        dependencies._pyproject_optional_dependencies,
        dependencies._available_optional_extras,
        dependencies._extra_requirements,
    )
    for cached in caches:
        cached.cache_clear()
    yield
    for cached in caches:
        cached.cache_clear()


@pytest.fixture(params=["source checkout", "installed package"])
def extra_requirements_source(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh_extra_requirements: None,
) -> None:
    """Read the extras' requirements from pyproject.toml, or from the installed metadata as a wheel install does."""
    del fresh_extra_requirements
    if request.param == "installed package":
        monkeypatch.setattr("mindroom.tool_system.dependencies._PROJECT_ROOT", tmp_path / "no-checkout")


@pytest.mark.usefixtures("old_mem0_distribution", "extra_requirements_source")
def test_an_installed_version_the_extra_does_not_allow_installs_the_extra(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An importable Mem0 older than the extra's floor counts as missing, so the extra installs."""
    installs: list[list[str]] = []

    def install(extras: list[str]) -> bool:
        installs.append(extras)
        return True

    monkeypatch.setattr("mindroom.tool_system.dependencies._install_optional_extras", install)
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})

    assert ensure_optional_deps(["mem0ai"], "mem0", runtime_paths) is True
    assert installs == [["mem0"]]


@pytest.mark.usefixtures("old_mem0_distribution", "extra_requirements_source")
def test_an_installed_version_the_extra_does_not_allow_names_the_version_when_install_is_off(tmp_path: Path) -> None:
    """With auto-install off, the error names the installed version, the extra's requirement, and the install."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env={"MINDROOM_NO_AUTO_INSTALL_TOOLS": "1"},
    )

    message = (
        "Unsupported dependency versions: mem0ai 2.0.1 (needs >=2.2.1). Install with: pip install 'mindroom[mem0]'"
    )
    with pytest.raises(ImportError, match=re.escape(message)):
        ensure_optional_deps(["mem0ai"], "mem0", runtime_paths, missing_message="Missing dependencies: mem0ai")


@pytest.mark.usefixtures("fresh_extra_requirements")
def test_requirements_absent_or_for_another_platform_start_no_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only installed requirements that apply on this platform are compared with the versions the extra allows."""
    monkeypatch.setattr(
        "mindroom.tool_system.dependencies._pyproject_optional_dependencies",
        lambda: {
            "demo": [
                "pytest>=1",
                "nonexistent-package-xyz-123>=1",
                "pytest>=999; sys_platform == 'no-such-platform'",
            ],
        },
    )

    def unexpected_install(_extras: list[str], **_kwargs: object) -> bool:
        pytest.fail("A requirement that is absent or for another platform must not start an install")

    monkeypatch.setattr("mindroom.tool_system.dependencies._install_optional_extras", unexpected_install)
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})

    assert ensure_optional_deps(["pytest"], "demo", runtime_paths) is False


def test_auto_install_enabled_uses_runtime_env(tmp_path: Path) -> None:
    """Auto-install disable flags should come from the explicit runtime context."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_NO_AUTO_INSTALL_TOOLS": "1"},
    )

    assert auto_install_enabled(runtime_paths) is False


def test_install_optional_extras_skips_uv_sync_outside_virtualenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside virtualenvs, optional extras should install via pip/uv pip instead of uv sync."""
    calls = {"sync": 0, "env": 0}

    def fake_install_via_uv_sync(_extras: list[str]) -> bool:
        calls["sync"] += 1
        return True

    def fake_install_in_environment(_extras: list[str]) -> bool:
        calls["env"] += 1
        return True

    monkeypatch.setattr("mindroom.tool_system.dependencies._is_uv_tool_install", lambda: False)
    monkeypatch.setattr("mindroom.tool_system.dependencies._has_lockfile", lambda: True)
    monkeypatch.setattr("mindroom.tool_system.dependencies._in_virtualenv", lambda: False)
    monkeypatch.setattr("mindroom.tool_system.dependencies.shutil.which", lambda _binary: "/usr/bin/uv")
    monkeypatch.setattr("mindroom.tool_system.dependencies._install_via_uv_sync", fake_install_via_uv_sync)
    monkeypatch.setattr("mindroom.tool_system.dependencies._install_in_environment", fake_install_in_environment)

    assert _install_optional_extras(["wikipedia"])
    assert calls["sync"] == 0
    assert calls["env"] == 1


def _fake_mindroom_site_packages(path: Path) -> Path:
    """Return a site-packages directory holding a MindRoom distribution, as a test process's own installation."""
    info = path / "mindroom-0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: mindroom\nVersion: 0\n")
    return path


_DEPENDENCY_PROCESS_SCRIPT = """
import importlib.metadata
import json
import sys
import tomllib
from pathlib import Path

root = Path(sys.argv[1])
mode = sys.argv[2]
sys.prefix = str(root)
sys.path.insert(0, str(root))

from mindroom.tool_system import dependencies

receipt = root / 'uv-receipt.toml'
info = root / 'transient_embedding-1.dist-info'


def install(extras):
    if mode == 'writer':
        info.mkdir()
        (info / 'top_level.txt').write_text('transient_embedding\\n')
        print('installing', flush=True)
        assert sys.stdin.readline().strip() == 'publish'
        (info / 'METADATA').write_text('Metadata-Version: 2.1\\nName: transient-embedding\\nVersion: 1\\n')
    receipt.write_text('[tool]\\nrequirements = [{ name = "mindroom", extras = ' + json.dumps(extras) + ' }]\\n')
    return True


dependencies._install_via_uv_tool = install
if mode == 'reader':
    from mindroom.embeddings import sentence_transformers_embedder as local_embedder
    from mindroom.constants import resolve_primary_runtime_paths

    def load(model):
        for name in importlib.metadata.packages_distributions()['transient_embedding']:
            assert importlib.metadata.version(name) == '1'
        return object()

    local_embedder.ensure_sentence_transformers_dependencies = lambda paths: None
    local_embedder._load_sentence_transformer = load
    paths = resolve_primary_runtime_paths(config_path=root / 'other-runtime' / 'config.yaml', process_env={})
    print('ready', flush=True)
    local_embedder.create_sentence_transformers_embedder(paths, 'sentence-transformers/test-model')
    print('imported complete metadata', flush=True)
elif mode == 'mem0_reader':
    import asyncio
    from types import ModuleType, SimpleNamespace
    from unittest.mock import MagicMock
    from mindroom.config.main import Config
    from mindroom.constants import resolve_primary_runtime_paths
    from mindroom.memory import config as memory_config

    class Memory:
        llm = SimpleNamespace(config=SimpleNamespace(model='gemma4'), client=SimpleNamespace(chat=lambda **_: None))
        embedding_model = SimpleNamespace()
        vector_store = MagicMock()

        @classmethod
        def from_config(cls, config):
            # Mem0 imports Chroma and the provider SDKs here, so it runs inside the import scope.
            assert dependencies._ENVIRONMENT_LOCK_STATE.held
            for name in importlib.metadata.packages_distributions()['transient_embedding']:
                assert importlib.metadata.version(name) == '1'
            return cls()

    def import_async_memory(name):
        # Importing Mem0 also waits until no install runs.
        if name != 'AsyncMemory':
            raise AttributeError(name)
        assert dependencies._ENVIRONMENT_LOCK_STATE.held
        return Memory

    mem0 = ModuleType('mem0')
    mem0.__getattr__ = import_async_memory
    sys.modules['mem0'] = mem0
    memory_config.ensure_optional_deps = lambda *args, **kwargs: False
    config = Config()
    # A remote embedder, whose Mem0 build imports Chroma and the provider SDKs only from from_config.
    config.memory.embedder.provider = 'ollama'
    config.memory.llm = None
    paths = resolve_primary_runtime_paths(config_path=root / 'mem0-runtime' / 'config.yaml', process_env={})
    print('ready', flush=True)
    asyncio.run(memory_config.create_memory_instance(root / 'memory', config, paths))
    print('imported complete metadata', flush=True)
else:
    if mode != 'writer':
        print('ready', flush=True)
    assert dependencies._install_optional_extras(['duckduckgo' if mode == 'writer' else 'browser'])
    print('installed', flush=True)
"""


def _dependency_process_line(process: subprocess.Popen[str]) -> str:
    assert process.stdout is not None
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
    return lines.get(timeout=20).strip()


def _stop_dependency_processes(*processes: subprocess.Popen[str] | None) -> None:
    for process in processes:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=20)


@pytest.mark.parametrize("contender", ["reader", "mem0_reader", "installer"])
def test_optional_dependency_environment_coordinates_processes(tmp_path: Path, contender: str) -> None:
    """Imports wait for complete metadata, and concurrent uv installs preserve receipt extras."""
    # The scripts put this directory first on sys.path before importing MindRoom,
    # so its MindRoom distribution holds their lock.
    environment = _fake_mindroom_site_packages(tmp_path / "environment")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (environment / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "mindroom", extras = ["website"] }]\n',
    )
    writer = subprocess.Popen(
        [sys.executable, "-u", "-c", textwrap.dedent(_DEPENDENCY_PROCESS_SCRIPT), str(environment), "writer"],
        env={**os.environ, "HOME": str(tmp_path), "TMPDIR": str(scratch)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    reader: subprocess.Popen[str] | None = None
    try:
        assert writer.stdout is not None
        assert writer.stdin is not None
        assert _dependency_process_line(writer) == "installing"
        reader = subprocess.Popen(
            [sys.executable, "-u", "-c", textwrap.dedent(_DEPENDENCY_PROCESS_SCRIPT), str(environment), contender],
            env={**os.environ, "HOME": str(tmp_path), "TMPDIR": str(scratch)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert reader.stdout is not None
        assert _dependency_process_line(reader) == "ready"
        # A contender has reached its dependency operation while the writer's
        # dist-info directory deliberately has no METADATA yet.
        with pytest.raises(subprocess.TimeoutExpired):
            reader.wait(timeout=0.3)
        writer.stdin.write("publish\n")
        writer.stdin.flush()
        writer_output, writer_error = writer.communicate(timeout=20)
        assert writer.returncode == 0, writer_error
        assert "installed" in writer_output
        reader_output, reader_error = reader.communicate(timeout=20)
        assert reader.returncode == 0, reader_error
        expected = "installed" if contender == "installer" else "imported complete metadata"
        assert expected in reader_output
        assert (environment / ".mindroom-optional-dependencies.lock").exists()
        extras = tomllib.loads((environment / "uv-receipt.toml").read_text())["tool"]["requirements"][0]["extras"]
        assert set(extras) == (
            {"duckduckgo", "website"} if contender != "installer" else {"browser", "duckduckgo", "website"}
        )
    finally:
        _stop_dependency_processes(writer, reader)


_LOCK_CONTENDER_SCRIPT = """
import sys

site_packages, prefix, mode = sys.argv[1:]
sys.path.insert(0, site_packages)
sys.prefix = prefix

from mindroom.tool_system.dependencies import optional_dependency_environment

print('ready', flush=True)
with optional_dependency_environment(exclusive=mode == 'installer'):
    print('locked', flush=True)
    if mode == 'installer':
        sys.stdin.readline()
"""


@pytest.mark.parametrize("importer_prefix", ["primary", "worker-venv"])
def test_dependency_lock_is_shared_across_temporary_directories(tmp_path: Path, importer_prefix: str) -> None:
    """An import waits for an install from a process with another TMPDIR, also from a local worker's own venv."""
    site_packages = _fake_mindroom_site_packages(tmp_path / "site-packages")

    def start(mode: str, prefix: str) -> subprocess.Popen[str]:
        scratch = tmp_path / f"{mode}-tmp"
        scratch.mkdir()
        return subprocess.Popen(
            [sys.executable, "-u", "-c", _LOCK_CONTENDER_SCRIPT, str(site_packages), str(tmp_path / prefix), mode],
            env={**os.environ, "TMPDIR": str(scratch)},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    installer = start("installer", "primary")
    importer: subprocess.Popen[str] | None = None
    try:
        assert installer.stdin is not None
        assert _dependency_process_line(installer) == "ready"
        assert _dependency_process_line(installer) == "locked"
        importer = start("importer", importer_prefix)
        assert _dependency_process_line(importer) == "ready"
        with pytest.raises(subprocess.TimeoutExpired):
            importer.wait(timeout=0.5)
        installer.stdin.write("done\n")
        installer.stdin.flush()
        _, installer_error = installer.communicate(timeout=20)
        assert installer.returncode == 0, installer_error
        importer_output, importer_error = importer.communicate(timeout=20)
        assert importer.returncode == 0, importer_error
        assert importer_output.strip() == "locked"
    finally:
        _stop_dependency_processes(installer, importer)


_REINSTALLED_MINDROOM_SCRIPT = """
import shutil
import sys
from pathlib import Path

site_packages = Path(sys.argv[1])
sys.path.insert(0, str(site_packages))

from mindroom.tool_system.dependencies import _environment_lock_path

# An install that reinstalls MindRoom itself removes its metadata before writing the new one,
# here before this process takes its first lock.
shutil.rmtree(site_packages / 'mindroom-0.dist-info')
print(_environment_lock_path())
"""


def test_dependency_lock_stays_put_while_mindroom_itself_is_reinstalled(tmp_path: Path) -> None:
    """The lock does not move, or fail to resolve, while an install replaces MindRoom's own metadata."""
    site_packages = _fake_mindroom_site_packages(tmp_path / "site-packages")
    result = subprocess.run(
        [sys.executable, "-c", _REINSTALLED_MINDROOM_SCRIPT, str(site_packages)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(site_packages / ".mindroom-optional-dependencies.lock")


def test_unrelated_tool_constructors_run_in_parallel(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """Deferred tool builds remain concurrent while sharing the dependency read lock."""
    tool_name = "test_parallel_optional_tool"
    rendezvous = threading.Barrier(2)

    class DummyToolkit(Toolkit):
        name = tool_name

        def __init__(self) -> None:
            rendezvous.wait(timeout=10)
            super().__init__()

    register_test_tool(
        monkeypatch,
        ToolMetadata(
            name=tool_name,
            file_access=ToolFileAccess.NONE,
            display_name="Parallel Test Tool",
            description="Temporary constructor concurrency test",
            category=ToolCategory.DEVELOPMENT,
            status=ToolStatus.AVAILABLE,
            setup_type=SetupType.NONE,
            config_fields=[],
            dependencies=[],
            factory=lambda: DummyToolkit,
        ),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        tools = list(
            pool.map(
                lambda _: get_tool_by_name(
                    tool_name,
                    runtime_paths,
                    worker_target=None,
                    tools=builtin_tool_registry(),
                ),
                range(2),
            ),
        )
    assert all(isinstance(tool, DummyToolkit) for tool in tools)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX file modes")
def test_optional_dependency_lock_is_private_to_the_current_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Other local accounts that can read the installation cannot open its lock to hold it."""
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    monkeypatch.setattr(dependencies, "_INSTALLATION_SITE_PACKAGES", site_packages)
    with optional_dependency_environment():
        pass
    assert (site_packages / ".mindroom-optional-dependencies.lock").stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="Windows temporary directories are per user")
@pytest.mark.skipif(sys.platform != "win32" and os.geteuid() == 0, reason="root can open files without permission")
@pytest.mark.parametrize("installation", ["read-only", "lock-of-another-account"])
def test_optional_dependency_lock_directory_must_be_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installation: str,
) -> None:
    """When this user cannot open the installation's lock, other local users cannot share or plant its own lock."""
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    monkeypatch.setattr(dependencies, "_INSTALLATION_SITE_PACKAGES", site_packages)
    if installation == "read-only":
        site_packages.chmod(0o555)
    else:
        # A lock another account created is private to it, so this user cannot open it.
        (site_packages / ".mindroom-optional-dependencies.lock").touch(mode=0o000)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    lock_dir = tmp_path / f"mindroom-optional-dependencies-{os.getuid()}"
    lock_dir.mkdir()
    lock_dir.chmod(0o755)
    try:
        with pytest.raises(PermissionError, match="only the current user"), optional_dependency_environment():
            pass
        lock_dir.chmod(0o700)
        with optional_dependency_environment():
            assert len(list(lock_dir.glob("*.lock"))) == 1
    finally:
        site_packages.chmod(0o755)
