"""Test that ConfigField definitions match actual tool parameters from agno."""

import inspect
from pathlib import Path
from types import UnionType
from typing import Any, Union, cast, get_args, get_origin, get_type_hints

import pytest
from agno.tools import Toolkit
from agno.tools.dalle import DalleTools

# Import tools to ensure they're registered
import mindroom.tools  # noqa: F401
from mindroom.constants import RuntimePaths
from mindroom.tool_system.declarations import ToolManagedInitArg, ToolStatus
from mindroom.tool_system.metadata import TOOL_METADATA, TOOL_REGISTRY, validate_authored_tool_entry_overrides
from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

SKIP_CONFIG_FIELD_VALIDATION = {
    "agent_vault_access",
    "homeassistant",
    "gmail",
    "google_calendar",
    "google_drive",
    "google_sheets",
    "openclaw_compat",
}
# Tools whose backing package may legitimately be absent, so a missing import is
# not a contract failure. `apify` is here because `pyproject.toml` declares
# `apify-client` for `platform_machine != 'aarch64'`, which means every arm64
# host runs this suite without it.
OPTIONAL_TOOL_IMPORTS = frozenset({"apify", "scrapegraph", "telegram"})
IGNORED_AGNO_PARAMS = {
    # Agno still exposes deprecated BigQuery aliases in its constructor, but MindRoom intentionally only surfaces canonical flags.
    "google_bigquery": {"enable_list_tables", "enable_describe_table", "enable_run_sql_query"},
    # Mapping-only inputs have no safe authored ConfigField representation.
    "mem0": {"config"},
    "scrapegraph": {"headers"},
    # Agno accepts an SSLContext for Slack, but MindRoom has no safe serialized UI/config path for it.
    "slack": {"ssl"},
    "youtube": {"proxies"},
    # Agno accepts a live HTTP session object, which MindRoom cannot serialize safely in UI/YAML config.
    "yfinance": {"session"},
}
IGNORED_EXTRA_CONFIG_FIELDS = {
    # DockerTools accepts toolkit options through **kwargs, so inspect.signature cannot see include_tools.
    "docker": {"include_tools"},
}


def test_dalle_default_model_is_accepted_by_agno() -> None:
    """The dashboard default for the DALL-E tool must satisfy Agno's constructor validation."""
    model_field = next(field for field in TOOL_METADATA["dalle"].config_fields or [] if field.name == "model")

    assert isinstance(model_field.default, str)
    assert model_field.default
    DalleTools(model=model_field.default, api_key="sk-test")


def test_youtube_languages_accepts_authored_string_list() -> None:
    """YouTube language preferences should preserve the list expected by Agno."""
    overrides = validate_authored_tool_entry_overrides("youtube", {"languages": ["en", "nl"]})

    assert overrides == {"languages": ["en", "nl"]}


@pytest.mark.parametrize(
    ("tool_name", "mapping_field"),
    [("youtube", "proxies"), ("mem0", "config"), ("scrapegraph", "headers")],
)
def test_mapping_only_upstream_fields_are_not_authored(tool_name: str, mapping_field: str) -> None:
    """Mapping-only upstream inputs should not be exposed as misleading text fields."""
    authored_names = {field.name for field in TOOL_METADATA[tool_name].config_fields or []}

    assert mapping_field not in authored_names


def test_arxiv_download_directory_is_converted_to_path(tmp_path: Path) -> None:
    """Authored ArXiv download paths should reach Agno as Path objects."""
    tool_class = cast("Any", TOOL_REGISTRY["arxiv"]())

    tool = tool_class(download_dir=str(tmp_path / "papers"))

    assert tool.download_dir == tmp_path / "papers"


def test_arxiv_blank_download_directory_preserves_upstream_default() -> None:
    """A blank optional path should retain Agno's default download directory."""
    tool_class = cast("Any", TOOL_REGISTRY["arxiv"]())

    default_tool = tool_class()
    blank_tool = tool_class(download_dir="")

    assert blank_tool.download_dir == default_tool.download_dir


def test_arxiv_string_union_config_type_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Path-or-string authored fields must not escape config type validation."""
    tool_class = cast("Any", TOOL_REGISTRY["arxiv"]())
    download_field = next(field for field in TOOL_METADATA["arxiv"].config_fields or [] if field.name == "download_dir")
    monkeypatch.setattr(download_field, "type", "number")

    with pytest.raises(pytest.fail.Exception, match="download_dir: expected type 'text'"):
        verify_tool_configfields("arxiv", tool_class, inspect.signature(tool_class.__init__))


def test_pubmed_configured_max_results_applies_when_call_omits_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured PubMed result limits should be the default for model calls."""
    tool_class = cast("Any", TOOL_REGISTRY["pubmed"]())
    tool = tool_class(max_results=5)
    captured: dict[str, int] = {}

    def fetch_pubmed_ids(_query: str, max_results: int, _email: str) -> list[str]:
        captured["max_results"] = max_results
        return []

    monkeypatch.setattr(tool, "fetch_pubmed_ids", fetch_pubmed_ids)
    monkeypatch.setattr(tool, "fetch_details", lambda _ids: object())
    monkeypatch.setattr(tool, "parse_details", lambda _root: [])

    tool.search_pubmed("durable agents")

    assert captured == {"max_results": 5}


def test_pubmed_explicit_zero_max_results_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit PubMed result limit must not fall back to the configured default."""
    tool_class = cast("Any", TOOL_REGISTRY["pubmed"]())
    tool = tool_class(max_results=5)
    captured: dict[str, int] = {}

    def fetch_pubmed_ids(_query: str, max_results: int, _email: str) -> list[str]:
        captured["max_results"] = max_results
        return []

    monkeypatch.setattr(tool, "fetch_pubmed_ids", fetch_pubmed_ids)
    monkeypatch.setattr(tool, "fetch_details", lambda _ids: object())
    monkeypatch.setattr(tool, "parse_details", lambda _root: [])

    result = tool.search_pubmed("durable agents", max_results=0)

    assert result == "[]"
    assert captured == {}


def test_pubmed_model_description_explains_configured_default() -> None:
    """The model-facing PubMed method description should explain omitted limits."""
    tool_class = cast("Any", TOOL_REGISTRY["pubmed"]())

    assert "configured max_results" in inspect.getdoc(tool_class.search_pubmed)


def test_modelslabs_converts_authored_file_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard-authored media types should reach Agno as FileType values."""
    from agno.tools.models_labs import ModelsLabTools  # noqa: PLC0415

    captured: dict[str, object] = {}

    def capture_init(_self: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(ModelsLabTools, "__init__", capture_init)
    tool_class = cast("Any", TOOL_REGISTRY["modelslabs"]())

    tool_class(file_type="gif")

    assert getattr(captured["file_type"], "value", None) == "gif"


@pytest.mark.parametrize("authored_file_type", [None, "", "   "])
def test_modelslabs_blank_file_type_uses_mp4_default(
    authored_file_type: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleared optional file types should preserve the ModelsLab MP4 default."""
    from agno.tools.models_labs import ModelsLabTools  # noqa: PLC0415

    captured: dict[str, object] = {}

    def capture_init(_self: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(ModelsLabTools, "__init__", capture_init)
    overrides = validate_authored_tool_entry_overrides("modelslabs", {"file_type": authored_file_type})
    tool_class = cast("Any", TOOL_REGISTRY["modelslabs"]())

    tool_class(**overrides)

    assert getattr(captured["file_type"], "value", None) == "mp4"


def test_daytona_converts_authored_sandbox_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard-authored Daytona values should reach Agno with its declared types."""
    from agno.tools.daytona import DaytonaTools  # noqa: PLC0415

    captured: dict[str, object] = {}

    def capture_init(_self: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(DaytonaTools, "__init__", capture_init)
    tool_class = cast("Any", TOOL_REGISTRY["daytona"]())

    tool_class(
        sandbox_language="PYTHON",
        sandbox_env_vars='{"TOKEN": "value"}',
        sandbox_labels='{"team": "agents"}',
    )

    assert getattr(captured["sandbox_language"], "value", None) == "python"
    assert captured["sandbox_env_vars"] == {"TOKEN": "value"}
    assert captured["sandbox_labels"] == {"team": "agents"}


def test_daytona_blank_optional_sandbox_values_become_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cleared dashboard fields should preserve Daytona's optional defaults."""
    from agno.tools.daytona import DaytonaTools  # noqa: PLC0415

    captured: dict[str, object] = {}

    def capture_init(_self: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(DaytonaTools, "__init__", capture_init)
    tool_class = cast("Any", TOOL_REGISTRY["daytona"]())

    tool_class(sandbox_language="", sandbox_env_vars="", sandbox_labels="")

    assert captured["sandbox_language"] is None
    assert captured["sandbox_env_vars"] is None
    assert captured["sandbox_labels"] is None


def test_tool_metadata_lists_only_model_callable_functions() -> None:
    """Tool metadata must not advertise internal toolkit helpers."""
    assert TOOL_METADATA["pubmed"].function_names == ("search_pubmed",)
    assert TOOL_METADATA["youtube"].function_names == (
        "get_video_timestamps",
        "get_youtube_video_captions",
        "get_youtube_video_data",
    )
    assert TOOL_METADATA["twilio"].function_names == ("get_call_details", "list_messages", "send_sms")
    assert TOOL_METADATA["x"].function_names == (
        "create_post",
        "get_home_timeline",
        "get_user_info",
        "reply_to_post",
        "search_posts",
        "send_dm",
    )
    assert TOOL_METADATA["slack"].function_names == (
        "download_file",
        "get_channel_history",
        "get_channel_info",
        "get_thread",
        "get_user_info",
        "list_channels",
        "list_users",
        "search_messages",
        "search_workspace",
        "send_message",
        "send_message_thread",
        "upload_file",
    )


def test_zep_metadata_lists_only_model_callable_functions() -> None:
    """Internal Zep initialization must not be advertised as a model-callable function."""
    assert TOOL_METADATA["zep"].function_names == (
        "add_zep_message",
        "get_zep_memory",
        "search_zep_memory",
    )


@pytest.mark.parametrize("tool_name", list(TOOL_REGISTRY.keys()))
def test_registered_tool_contract(tool_name: str) -> None:
    """Validate one registered tool's import, managed inputs, and authored config fields."""
    metadata = TOOL_METADATA[tool_name]
    tool_factory = TOOL_REGISTRY[tool_name]
    try:
        tool_class = tool_factory()
    except Exception as exc:
        if (
            metadata.status == ToolStatus.REQUIRES_CONFIG
            and isinstance(exc, ImportError)
            and tool_name in OPTIONAL_TOOL_IMPORTS
        ):
            pytest.skip(f"{tool_name} optional dependency not installed: {exc}")
        if metadata.status == ToolStatus.REQUIRES_CONFIG and isinstance(exc, NotImplementedError):
            pytest.skip(f"{tool_name} tool is not implemented: {exc}")
        raise

    assert isinstance(tool_class, type)
    if metadata.status != ToolStatus.REQUIRES_CONFIG:
        assert issubclass(tool_class, Toolkit)

    init_signature = inspect.signature(tool_class.__init__)
    verify_managed_init_args(tool_name, init_signature)
    if tool_name not in SKIP_CONFIG_FIELD_VALIDATION:
        verify_tool_configfields(tool_name, tool_class, init_signature)


def verify_managed_init_args(tool_name: str, init_signature: inspect.Signature) -> None:
    """Verify one tool explicitly declares every MindRoom-managed constructor input."""
    metadata = TOOL_METADATA[tool_name]
    managed_arg_names = {managed_arg.value for managed_arg in ToolManagedInitArg}
    constructor_param_names = {name for name in init_signature.parameters if name != "self"}
    expected_managed_args = tuple(
        managed_arg for managed_arg in ToolManagedInitArg if managed_arg.value in constructor_param_names
    )

    assert metadata.managed_init_args == expected_managed_args, (
        f"{tool_name} declares constructor inputs "
        f"{sorted(constructor_param_names & managed_arg_names)} but metadata lists "
        f"{[managed_arg.value for managed_arg in metadata.managed_init_args]}"
    )


def verify_tool_configfields(  # noqa: C901, PLR0912, PLR0915
    tool_name: str,
    tool_class: type,
    init_signature: inspect.Signature,
) -> None:
    """Verify tool ConfigFields match agno tool parameters.

    Args:
        tool_name: Name of the tool in the registry
        tool_class: The agno tool class to check against
        init_signature: Constructor signature shared with managed-input validation

    """
    # Get the actual parameters from agno
    resolved_type_hints = get_type_hints(
        tool_class.__init__,
        globalns=tool_class.__init__.__globals__
        | {
            "ResolvedWorkerTarget": ResolvedWorkerTarget,
            "RuntimePaths": RuntimePaths,
        },
    )
    agno_params = {}

    for name, param in init_signature.parameters.items():
        if name == "self":
            continue
        # Skip **kwargs as it's for forward compatibility
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            continue
        # Managed init args are injected by MindRoom, not end-user tool config.
        if name in {managed_arg.value for managed_arg in ToolManagedInitArg}:
            continue
        agno_params[name] = {
            "type": resolved_type_hints.get(name),
        }

    ignored_param_names = IGNORED_AGNO_PARAMS.get(tool_name, set())
    agno_params = {name: param_info for name, param_info in agno_params.items() if name not in ignored_param_names}

    # Get our ConfigFields for the tool
    tool_metadata = TOOL_METADATA[tool_name]

    config_fields = tool_metadata.config_fields or []
    config_field_map = {field.name: field for field in config_fields}

    # Check parameter names
    agno_param_names = set(agno_params.keys())
    config_field_names = set(config_field_map.keys())

    missing_fields = agno_param_names - config_field_names
    extra_fields = config_field_names - agno_param_names - IGNORED_EXTRA_CONFIG_FIELDS.get(tool_name, set())

    # Build error message if there are issues
    errors = []
    if missing_fields:
        errors.append(f"Missing ConfigFields for agno parameters: {', '.join(sorted(missing_fields))}")
    if extra_fields:
        errors.append(f"Extra ConfigFields not in agno: {', '.join(sorted(extra_fields))}")

    # Check types for matching parameters
    type_mismatches = []
    for param_name, param_info in agno_params.items():
        if param_name not in config_field_map:
            continue

        field = config_field_map[param_name]
        param_type = param_info["type"]

        # Handle Optional types
        actual_type = param_type
        origin = get_origin(param_type)
        if origin in {Union, UnionType}:
            concrete_types = tuple(arg for arg in get_args(param_type) if arg is not type(None))
            if str in concrete_types:
                actual_type = str
            elif len(concrete_types) == 1:
                actual_type = concrete_types[0]

        if actual_type is bool:
            expected_type = "boolean"
        elif actual_type is int or actual_type is float:
            expected_type = "number"
        elif actual_type is str:
            # String parameters - check name patterns for special types
            if (
                "token" in param_name.lower()
                or "password" in param_name.lower()
                or "secret" in param_name.lower()
                or "key" in param_name.lower()
            ):
                expected_type = "password"
            elif (
                "url" in param_name.lower()
                or "uri" in param_name.lower()
                or "proxy" in param_name.lower()
                or "endpoint" in param_name.lower()
                or "host" in param_name.lower()
            ):
                expected_type = "url"
            else:
                expected_type = "text"
        else:
            # For Any or other types, we can't determine automatically
            continue

        if field.type != expected_type:
            type_mismatches.append(
                f"{param_name}: expected type '{expected_type}' (from {param_type}), got '{field.type}'",
            )

    if type_mismatches:
        errors.append("Type mismatches:\n  " + "\n  ".join(type_mismatches))

    # Assert no errors
    if errors:
        error_msg = "\n\n".join(errors)
        pytest.fail(f"{tool_name} ConfigField validation failed:\n{error_msg}")

    # Success message (will only show with -v flag)
    print(f"\n✅ All {len(config_fields)} {tool_name} ConfigFields match agno parameter names and types!")
