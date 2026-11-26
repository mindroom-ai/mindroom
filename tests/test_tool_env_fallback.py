"""Tool settings read from the environment and the `.env` beside `config.yaml`."""

from __future__ import annotations

import ast
import importlib.util
import inspect
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
from mindroom.tool_system.registry import builtin_tool_registry

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
    # The provider key import supplies it for unscoped agents. Scoped agents get no environment fallback on purpose:
    # they need the granted shared key or their own saved setting, so no field declares it.
    "agno.tools.openai": frozenset({"OPENAI_API_KEY"}),
    # Undeclared for the same reason as `OPENAI_API_KEY`: scoped agents need the granted shared key or their own setting.
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


_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
# Native tools that read one named internal runtime variable directly (never a user credential).
_ALLOWED_NATIVE_ENV_VARS: dict[str, frozenset[str]] = {
    "mindroom.tools.shell": frozenset({"SHELL_SUPERVISOR_SOCKET_ENV"}),
}


def _mindroom_module_path(module_name: str) -> Path | None:
    candidate = _SRC_ROOT / Path(*module_name.split("."))
    if candidate.with_suffix(".py").is_file():
        return candidate.with_suffix(".py")
    if (candidate / "__init__.py").is_file():
        return candidate / "__init__.py"
    return None


def _ast_referenced_modules(module_name: str) -> tuple[set[str], set[str]]:
    """Collect referenced local tool modules and Agno modules by AST without importing optional SDKs."""
    visited: set[str] = set()
    local_modules: set[str] = set()
    agno_modules: set[str] = set()
    pending = [module_name]
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        path = _mindroom_module_path(current)
        if path is None:
            continue
        local_modules.add(current)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                if node.module.startswith("agno.tools."):
                    agno_modules.add(node.module)
                elif node.module.startswith(("mindroom.tools.", "mindroom.custom_tools.")) and not node.module.endswith(
                    ".__init__",
                ):
                    pending.append(node.module)
    return local_modules, agno_modules


def _agno_modules(tool_name: str) -> set[str]:
    """Return the Agno modules that define a built-in tool's toolkit class and its bases."""
    factory = builtin_tool_registry().entries[tool_name].factory
    if factory is None:
        return set()
    _, ast_agno = _ast_referenced_modules(factory.__module__)
    if not ast_agno:
        return set()
    try:
        toolkit_class = factory()
    except ImportError:
        return ast_agno
    return {klass.__module__ for klass in inspect.getmro(toolkit_class) if klass.__module__.startswith("agno.")}


def _native_env_reads(module_name: str) -> set[str]:
    """Return direct os.getenv/os.environ reads in a native MindRoom tool module via AST."""
    path = _mindroom_module_path(module_name)
    if path is None:
        return set()
    allowed = _ALLOWED_NATIVE_ENV_VARS.get(module_name, frozenset())
    reads: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and (
            (isinstance(node.func, ast.Name) and node.func.id == "getenv")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "getenv")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "get" and _is_environ(node.func.value))
        ):
            target = node.args[0]
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and _is_environ(node.value):
            target = node.slice
        else:
            continue
        if isinstance(target, ast.Name) and target.id in allowed:
            continue
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            reads.add(target.value)
        else:
            reads.add(f"<dynamic line {node.lineno}>")
    return reads


@pytest.mark.parametrize("tool_name", sorted(builtin_tool_registry().entries))
def test_each_tool_declares_the_environment_variables_its_toolkit_reads(tool_name: str) -> None:
    """Agno toolkits declare every env var they read; native toolkits never read os.environ directly."""
    factory = builtin_tool_registry().entries[tool_name].factory
    modules = _agno_modules(tool_name)
    if not modules:
        if factory is None:
            return
        # mindroom.tools.__init__ registers multiple built-in tools; only scan dedicated tool modules directly.
        if factory.__module__ == "mindroom.tools":
            return
        local_modules, _ = _ast_referenced_modules(factory.__module__)
        direct_reads = {mod: reads for mod in sorted(local_modules) if (reads := _native_env_reads(mod))}
        assert direct_reads == {}, f"Native toolkit {tool_name!r} must not read os.environ directly: {direct_reads}"
        return

    reads = set().union(*map(_module_env_reads, modules))
    allowlisted = set().union(*(UNDECLARED_AGNO_ENV_READS.get(module, ()) for module in modules))
    declared = {name for field in builtin_tool_registry().entries[tool_name].config_fields or () for name in field.env}

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
        tools=builtin_tool_registry(),
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


def test_a_file_variant_supplies_a_tool_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `<NAME>_FILE` variable names a file whose contents set the field, as it does for model provider keys."""
    secret_file = tmp_path / "tavily-key"
    secret_file.write_text("file-key\n")

    tool = _tavily(tmp_path, monkeypatch, process_env={"TAVILY_API_KEY_FILE": str(secret_file)})

    assert tool.api_key == "file-key"


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
        tools=builtin_tool_registry(),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        runs_in_worker=False,
        worker_target=None,
    )

    assert tool.region == "process-region"


@pytest.mark.usefixtures("restored_builtin_tools")
def test_a_number_setting_from_the_env_file_reaches_the_toolkit_as_a_number(tmp_path: Path) -> None:
    """A variable is converted to its field's type, as a saved value is."""
    tool_name = "test_env_number_tool"

    class _NumberToolkit(Toolkit):
        def __init__(self, limit: int | None = None) -> None:
            self.limit = limit
            super().__init__(name=tool_name)

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
    tool = get_tool_by_name(
        tool_name,
        runtime_paths,
        tools=builtin_tool_registry(),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        runs_in_worker=False,
        worker_target=None,
    )

    assert tool.limit == 42
    assert isinstance(tool.limit, int)
