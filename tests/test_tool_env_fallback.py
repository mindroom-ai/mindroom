"""Tool settings read from the environment and the `.env` beside `config.yaml`."""

from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any

import pytest

import mindroom.tools  # noqa: F401
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.credentials.manager import CredentialsManager
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, ToolCategory, ToolFileAccess
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tool_system.registry_state import (
    BUILTIN_TOOL_METADATA,
    capture_tool_registry_snapshot,
    restore_tool_registry_snapshot,
)

# Variables Agno toolkits read that no config field declares, by the Agno module that reads them.
UNDECLARED_AGNO_ENV_READS: dict[str, frozenset[str]] = {
    # Agno counts only the exact text `true` and google-genai also counts `1`, while the boolean `vertexai` field
    # accepts only `true` and `false`, so a declaration would fail builds that work.
    "agno.tools.models.gemini": frozenset({"GOOGLE_GENAI_USE_VERTEXAI"}),
    # Read while building the service or logging in interactively, which MindRoom's Google toolkits replace; no
    # constructor parameter backs them.
    "agno.tools.google.base": frozenset({"GOOGLE_API_TIMEOUT", "GOOGLE_PROJECT_ID"}),
    # MindRoom's Google Drive toolkit reads it from the runtime environment itself and passes it on.
    "agno.tools.google.drive": frozenset({"GOOGLE_CLOUD_QUOTA_PROJECT_ID"}),
    # The provider key import supplies it for unscoped agents; whether scoped agents read it from the environment is
    # an open decision.
    "agno.tools.openai": frozenset({"OPENAI_API_KEY"}),
    # The same open decision as `OPENAI_API_KEY`.
    "agno.tools.models.groq": frozenset({"GROQ_API_KEY"}),
}


def _is_environ(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "environ") or (
        isinstance(node, ast.Attribute) and node.attr == "environ"
    )


def _env_reads(source: str) -> set[str]:
    """Return the variables `source` reads with `getenv`, `environ.get`, or `environ[...]`."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and (
            (isinstance(node.func, ast.Name) and node.func.id == "getenv")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "getenv")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "get" and _is_environ(node.func.value))
        ):
            name = node.args[0]
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and _is_environ(node.value):
            name = node.slice
        else:
            continue
        assert isinstance(name, ast.Constant), f"line {node.lineno} reads a variable it does not name"
        assert isinstance(name.value, str)
        names.add(name.value)
    return names


def _module_env_reads(module_name: str) -> set[str]:
    """Return the variables one Agno module reads, from its source file."""
    spec = importlib.util.find_spec(module_name)
    assert spec is not None
    assert spec.origin is not None
    return _env_reads(Path(spec.origin).read_text(encoding="utf-8"))


def _agno_modules(tool_name: str) -> set[str]:
    """Return the Agno modules that define a built-in tool's toolkit class and its bases."""
    factory = BUILTIN_TOOL_METADATA[tool_name].factory
    if factory is None:
        return set()
    try:
        toolkit_class = factory()
    except ImportError:
        # The toolkit's SDK is not installed, so read the Agno modules its registration imports.
        registration = ast.parse(inspect.getsource(sys.modules[factory.__module__]))
        return {
            node.module
            for node in ast.walk(registration)
            if isinstance(node, ast.ImportFrom) and node.module is not None and node.module.startswith("agno.")
        }
    return {klass.__module__ for klass in inspect.getmro(toolkit_class) if klass.__module__.startswith("agno.")}


@pytest.mark.parametrize("tool_name", sorted(BUILTIN_TOOL_METADATA))
def test_each_tool_declares_the_environment_variables_its_toolkit_reads(tool_name: str) -> None:
    """Each variable the tool's Agno toolkit reads is declared or allowlisted, and each declared variable is read."""
    modules = _agno_modules(tool_name)
    reads = set().union(*map(_module_env_reads, modules))
    allowlisted = set().union(*(UNDECLARED_AGNO_ENV_READS.get(module, ()) for module in modules))
    declared = {name for field in BUILTIN_TOOL_METADATA[tool_name].config_fields or () for name in field.env}

    assert {"undeclared": reads - allowlisted - declared, "never read": declared - reads} == {
        "undeclared": set(),
        "never read": set(),
    }


@pytest.mark.parametrize("module_name", sorted(UNDECLARED_AGNO_ENV_READS))
def test_each_undeclared_agno_read_is_still_in_agno(module_name: str) -> None:
    """An allowlisted read leaves the list once Agno stops reading it."""
    assert _module_env_reads(module_name) >= UNDECLARED_AGNO_ENV_READS[module_name]


def test_the_source_scan_finds_every_way_a_module_reads_the_environment() -> None:
    """`getenv`, `os.getenv`, `environ.get`, and `environ[...]` count as reads; assigning a variable does not."""
    source = (
        "import os\nfrom os import environ, getenv\n"
        'getenv("A")\nos.getenv("B")\nenviron.get("C")\nos.environ.get("D")\nos.environ["E"]\nos.environ["F"] = ""\n'
    )

    assert _env_reads(source) == {"A", "B", "C", "D", "E"}


def _tavily(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    process_env: dict[str, str] | None = None,
    env_file: str = "",
    saved: dict[str, Any] | None = None,
    inline: dict[str, object] | None = None,
) -> Any:  # noqa: ANN401
    """Build `tavily` the way a bare `mindroom run` does, with the given environment, `.env`, and settings."""
    # A bare run reads the process environment once, into its runtime; nothing outside it may reach the toolkit.
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_BASE_URL", raising=False)
    (tmp_path / "config.yaml").write_text("agents: {}\n")
    (tmp_path / ".env").write_text(env_file)
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env={"MINDROOM_NO_AUTO_INSTALL_TOOLS": "1", **(process_env or {})},
    )
    credentials = CredentialsManager(tmp_path / "credentials")
    if saved is not None:
        credentials.save_credentials("tavily", saved)
    return get_tool_by_name(
        "tavily",
        runtime_paths,
        credentials_manager=credentials,
        tool_config_overrides=inline,
        runs_in_worker=False,
        worker_target=None,
    )


def test_a_bare_run_reads_a_tool_setting_set_only_in_the_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key and endpoint only in the `.env` beside `config.yaml` configure the toolkit."""
    tool = _tavily(
        tmp_path,
        monkeypatch,
        env_file="TAVILY_API_KEY=env-file-key\nTAVILY_API_BASE_URL=https://env-file.example\n",
    )

    assert (tool.api_key, tool.api_base_url) == ("env-file-key", "https://env-file.example")


@pytest.mark.parametrize(
    ("process_env", "env_file", "saved", "expected"),
    [
        pytest.param({"TAVILY_API_KEY": "process-key"}, "TAVILY_API_KEY=file-key\n", None, "process-key", id="process"),
        pytest.param(
            {"TAVILY_API_KEY": "process-key"},
            "TAVILY_API_KEY=file-key\n",
            {"api_key": "saved-key"},
            "saved-key",
            id="saved",
        ),
        pytest.param({}, "TAVILY_API_KEY=file-key\n", {"api_key": "  "}, "file-key", id="blank-saved"),
        pytest.param({"TAVILY_API_KEY": " "}, "TAVILY_API_KEY=file-key\n", None, "file-key", id="blank-process"),
    ],
)
def test_saved_settings_then_the_process_environment_win_over_the_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process_env: dict[str, str],
    env_file: str,
    saved: dict[str, Any] | None,
    expected: str,
) -> None:
    """A saved value wins, then the process environment, then the `.env`; a blank value counts as unset."""
    tool = _tavily(tmp_path, monkeypatch, process_env=process_env, env_file=env_file, saved=saved)

    assert tool.api_key == expected


def test_an_inline_setting_wins_over_saved_settings_and_the_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An authored `tools:` setting beats every lower layer."""
    tool = _tavily(
        tmp_path,
        monkeypatch,
        process_env={"TAVILY_API_BASE_URL": "https://process.example"},
        env_file="TAVILY_API_BASE_URL=https://env-file.example\n",
        saved={"api_key": "saved-key", "api_base_url": "https://saved.example"},
        inline={"api_base_url": "https://inline.example"},
    )

    assert tool.api_base_url == "https://inline.example"


def test_every_variable_in_the_process_environment_wins_over_the_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A field with two variables reads both from the process environment before either from the `.env`."""
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    (tmp_path / "config.yaml").write_text("agents: {}\n")
    (tmp_path / ".env").write_text("AWS_REGION=file-region\n")
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env={"MINDROOM_NO_AUTO_INSTALL_TOOLS": "1", "AWS_DEFAULT_REGION": "process-region"},
    )

    tool = get_tool_by_name(
        "redshift",
        runtime_paths,
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        runs_in_worker=False,
        worker_target=None,
    )

    assert tool.region == "process-region"


def test_a_number_setting_from_the_env_file_reaches_the_toolkit_as_a_number(tmp_path: Path) -> None:
    """A variable is converted to its field's type, as a saved value is."""
    tool_name = "test_env_number_tool"

    class _NumberToolkit(Toolkit):
        def __init__(self, limit: int | None = None) -> None:
            self.limit = limit
            super().__init__(name=tool_name)

    snapshot = capture_tool_registry_snapshot()

    @register_tool_with_metadata(
        name=tool_name,
        file_access=ToolFileAccess.NONE,
        display_name="Env Number Tool",
        description="Test-only coverage of a number setting from the environment.",
        category=ToolCategory.DEVELOPMENT,
        config_fields=[ConfigField(name="limit", label="Limit", type="number", required=False, env=("PROBE_LIMIT",))],
    )
    def _number_tool_factory() -> type[_NumberToolkit]:
        return _NumberToolkit

    (tmp_path / "config.yaml").write_text("agents: {}\n")
    (tmp_path / ".env").write_text("PROBE_LIMIT=42\n")
    runtime_paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})
    try:
        tool = get_tool_by_name(
            tool_name,
            runtime_paths,
            credentials_manager=CredentialsManager(tmp_path / "credentials"),
            runs_in_worker=False,
            worker_target=None,
        )
    finally:
        restore_tool_registry_snapshot(snapshot)

    assert tool.limit == 42
    assert isinstance(tool.limit, int)
