"""Tool settings read from the environment and the `.env` beside `config.yaml`."""

from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
import textwrap
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
    # Read while building the service or logging in interactively, which MindRoom's Google toolkits replace; no
    # constructor parameter backs them.
    "agno.tools.google.base": frozenset({"GOOGLE_API_TIMEOUT", "GOOGLE_PROJECT_ID"}),
    # MindRoom's Google Drive toolkit reads it from the runtime environment itself and passes it on.
    "agno.tools.google.drive": frozenset({"GOOGLE_CLOUD_QUOTA_PROJECT_ID"}),
}

# Non-config process IPC variables native toolkits read directly from os.environ.
UNDECLARED_NATIVE_ENV_READS: dict[str, frozenset[str]] = {
    # Internal sandbox-runner IPC socket injected into tool subprocesses, not an end-user tool setting.
    "mindroom.tools.shell": frozenset({"SHELL_SUPERVISOR_SOCKET_ENV"}),
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
        assert isinstance(name, ast.Constant | ast.Name), f"line {node.lineno} reads a variable it does not name"
        if isinstance(name, ast.Constant):
            assert isinstance(name.value, str)
            names.add(name.value)
        else:
            names.add(name.id)
    return names


def _module_env_reads(module_name: str) -> set[str]:
    """Return the variables one module reads from the process environment, from its source file."""
    spec = importlib.util.find_spec(module_name)
    assert spec is not None
    assert spec.origin is not None
    return _env_reads(Path(spec.origin).read_text(encoding="utf-8"))


def _agno_modules(tool_name: str) -> set[str]:
    """Return the Agno modules that define a built-in tool's toolkit class and its bases."""
    factory = builtin_tool_registry().entries[tool_name].factory
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


def _imported_mindroom_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None and node.module.startswith("mindroom."):
            if node.module != "mindroom.tool_system.authoring":
                modules.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("mindroom.") and alias.name != "mindroom.tool_system.authoring":
                    modules.add(alias.name)
    return modules


def _native_base_modules(module_name: str) -> set[str]:
    spec = importlib.util.find_spec(module_name)
    if spec is None or spec.origin is None:
        return set()
    tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
    symbol_modules: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None and node.module.startswith("mindroom."):
            for alias in node.names:
                symbol_modules[alias.asname or alias.name] = node.module
    base_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                base_name = base.id if isinstance(base, ast.Name) else None
                if (
                    base_name is not None
                    and (base_mod := symbol_modules.get(base_name)) is not None
                    and base_mod != "mindroom.tool_system.authoring"
                ):
                    base_modules.add(base_mod)
    return base_modules


def _native_modules(tool_name: str) -> set[str]:
    """Return native MindRoom toolkit modules via source AST without importing optional SDKs."""
    factory = builtin_tool_registry().entries[tool_name].factory
    if factory is None:
        return set()
    factory_tree = ast.parse(textwrap.dedent(inspect.getsource(factory)))
    pending = _imported_mindroom_modules(factory_tree)
    if not pending and factory.__module__ != "mindroom.tools":
        pending = {factory.__module__}
    discovered: set[str] = set()
    while pending:
        module_name = pending.pop()
        if module_name in discovered:
            continue
        discovered.add(module_name)
        pending.update(_native_base_modules(module_name) - discovered)
    return discovered


@pytest.mark.parametrize("tool_name", sorted(builtin_tool_registry().entries))
def test_each_tool_declares_the_environment_variables_its_toolkit_reads(tool_name: str) -> None:
    """Agno toolkits match declared/allowlisted env reads; native toolkits read none directly unless allowlisted."""
    modules = _agno_modules(tool_name)
    declared = {name for field in builtin_tool_registry().entries[tool_name].config_fields or () for name in field.env}
    if not modules:
        native_modules = _native_modules(tool_name)
        if declared:
            assert native_modules, f"{tool_name} declares env fallbacks without a native toolkit module"
        native_reads = set().union(*map(_module_env_reads, native_modules)) if native_modules else set()
        allowlisted_native = set().union(*(UNDECLARED_NATIVE_ENV_READS.get(module, ()) for module in native_modules))
        unexpected_native_reads = native_reads - allowlisted_native
        assert unexpected_native_reads == set(), (
            f"Native toolkit {tool_name} must receive env values via config/metadata.py or runtime_paths, "
            f"not direct environment reads: {sorted(unexpected_native_reads)}"
        )
        return

    reads = set().union(*map(_module_env_reads, modules))
    allowlisted = set().union(*(UNDECLARED_AGNO_ENV_READS.get(module, ()) for module in modules))

    assert {"undeclared": reads - allowlisted - declared, "never read": declared - reads} == {
        "undeclared": set(),
        "never read": set(),
    }


@pytest.mark.parametrize("module_name", sorted(UNDECLARED_AGNO_ENV_READS))
def test_each_undeclared_agno_read_is_still_in_agno(module_name: str) -> None:
    """An allowlisted read leaves the list once Agno stops reading it."""
    assert _module_env_reads(module_name) >= UNDECLARED_AGNO_ENV_READS[module_name]


@pytest.mark.parametrize("module_name", sorted(UNDECLARED_NATIVE_ENV_READS))
def test_each_undeclared_native_read_is_still_in_mindroom(module_name: str) -> None:
    """An allowlisted native read leaves the list once MindRoom stops reading it."""
    assert _module_env_reads(module_name) >= UNDECLARED_NATIVE_ENV_READS[module_name]


def test_native_module_scan_works_without_importing_optional_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inspecting native modules via AST succeeds even if the optional SDK cannot be imported."""
    real_import = __import__

    def fail_cartesia(name: str, *args: object, **kwargs: object) -> object:
        if name == "cartesia" or name.startswith("cartesia."):
            msg = "simulated missing cartesia SDK"
            raise ImportError(msg)
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "cartesia", raising=False)
    monkeypatch.delitem(sys.modules, "mindroom.custom_tools.cartesia", raising=False)
    monkeypatch.setattr("builtins.__import__", fail_cartesia)

    assert _agno_modules("cartesia") == set()
    assert _native_modules("cartesia") == {"mindroom.custom_tools.cartesia"}


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


@pytest.mark.parametrize(
    ("tool_name", "env_var"),
    [("openai", "OPENAI_API_KEY"), ("groq", "GROQ_API_KEY")],
)
def test_provider_backed_media_tools_honor_unscoped_fallback_and_scoped_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    env_var: str,
) -> None:
    """Unscoped and granted agents receive provider keys; ungranted scoped agents fail closed despite ambient env."""
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target  # noqa: PLC0415

    # Set ambient host env too, proving native toolkits cannot bypass MindRoom's scoped credential isolation.
    monkeypatch.setenv(env_var, "ambient-host-secret")
    (tmp_path / "config.yaml").write_text("agents: {}\n")
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_NO_AUTO_INSTALL_TOOLS": "1", env_var: "provider-shared-key"},
    )
    tools = builtin_tool_registry()

    unscoped = get_tool_by_name(
        tool_name,
        runtime_paths,
        tools=tools,
        runs_in_worker=False,
        worker_target=None,
    )
    assert unscoped.api_key == "provider-shared-key"

    scoped_target = resolve_worker_target(
        "user",
        "alpha",
        ToolExecutionIdentity("matrix", "alpha", "@alice:example.test", None, None, None, None, tenant_id="t"),
    )
    with pytest.raises(ValueError, match=f"{env_var} not set"):
        get_tool_by_name(
            tool_name,
            runtime_paths,
            tools=tools,
            runs_in_worker=False,
            worker_target=scoped_target,
            allowed_shared_services=frozenset(),
        )

    granted = get_tool_by_name(
        tool_name,
        runtime_paths,
        tools=tools,
        runs_in_worker=False,
        worker_target=scoped_target,
        allowed_shared_services=frozenset({tool_name}),
    )
    assert granted.api_key == "provider-shared-key"
