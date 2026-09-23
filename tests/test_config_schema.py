"""Dashboard-facing guarantees of the configuration JSON schema."""

from __future__ import annotations

import inspect
import json
import typing
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel

from mindroom.config.main import Config, dashboard_config_schema
from mindroom.config.schema_hints import dashboard_hint

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pydantic.fields import FieldInfo

# The dashboard's configSchema.ts understands exactly this hint vocabulary.
HINT_KEY = "x-mindroom"
_HINT_FIELDS = {"reference", "key_reference", "secret", "multiline", "clears_inherited"}
_REFERENCE_KINDS = {"model", "agent", "room", "tool"}


def _nested_models(annotation: object) -> list[type[BaseModel]]:
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        return [annotation]
    models = [model for argument in typing.get_args(annotation) for model in _nested_models(argument)]
    if isinstance(annotation, typing.TypeAliasType):
        models.extend(_nested_models(annotation.__value__))
    return models


def _walk_fields() -> Iterator[tuple[type[BaseModel], str, FieldInfo]]:
    seen: set[type[BaseModel]] = set()
    pending: list[type[BaseModel]] = [Config]
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.add(model)
        for name, field in model.model_fields.items():
            yield model, name, field
            pending.extend(_nested_models(field.annotation))


def _collect_hints(node: object) -> Iterator[dict[str, object]]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == HINT_KEY:
                assert isinstance(value, dict)
                yield cast("dict[str, object]", value)
            else:
                yield from _collect_hints(value)
    elif isinstance(node, list):
        for item in node:
            yield from _collect_hints(item)


def test_every_config_field_has_description() -> None:
    """The dashboard shows each description as helper text, so none may be missing."""
    missing = [f"{model.__name__}.{name}" for model, name, field in _walk_fields() if not field.description]
    assert missing == []


def test_schema_hints_use_known_vocabulary() -> None:
    """Every hint in the schema must be one the dashboard understands."""
    hints = list(_collect_hints(Config.model_json_schema()))
    assert hints
    for hint in hints:
        assert hint
        assert set(hint) <= _HINT_FIELDS
        for key in ("reference", "key_reference"):
            if key in hint:
                assert hint[key] in _REFERENCE_KINDS


def test_dashboard_hint_omits_unset_fields() -> None:
    """Hints carry only the annotations that apply to the field."""
    assert dashboard_hint(reference="model") == {HINT_KEY: {"reference": "model"}}
    assert dashboard_hint(key_reference="room", reference="model") == {
        HINT_KEY: {"reference": "model", "key_reference": "room"},
    }
    assert dashboard_hint(secret=True) == {HINT_KEY: {"secret": True}}
    assert dashboard_hint(multiline=True) == {HINT_KEY: {"multiline": True}}
    assert dashboard_hint(clears_inherited=True) == {HINT_KEY: {"clears_inherited": True}}


def test_reference_fields_are_annotated() -> None:
    """Fields naming configured entities render as pickers in the dashboard."""
    schema = Config.model_json_schema()
    defs = schema["$defs"]
    assert defs["RouterConfig"]["properties"]["model"][HINT_KEY] == {"reference": "model"}
    assert defs["LLMJudgmentConfig"]["properties"]["model"][HINT_KEY] == {"reference": "model"}
    assert defs["PersonalRoomsConfig"]["properties"]["agent"][HINT_KEY] == {"reference": "agent"}
    assert defs["PersonalRoomsConfig"]["properties"]["onboarding_rooms"][HINT_KEY] == {"reference": "room"}
    assert defs["DefaultsConfig"]["properties"]["tools"][HINT_KEY] == {"reference": "tool"}
    assert schema["properties"]["room_thread_summary_models"][HINT_KEY] == {
        "reference": "model",
        "key_reference": "room",
    }


def test_secret_fields_are_annotated() -> None:
    """Credential-bearing strings render as password inputs."""
    defs = Config.model_json_schema()["$defs"]
    assert defs["ModelConfig"]["properties"]["extra_kwargs"][HINT_KEY] == {"secret": True}
    assert defs["_MemoryLLMConfig"]["properties"]["config"][HINT_KEY] == {"secret": True}
    assert defs["PluginEntryConfig"]["properties"]["settings"][HINT_KEY] == {"secret": True}
    assert defs["EventJournalConfig"]["properties"]["database_url"][HINT_KEY] == {"secret": True}
    assert defs["MCPServerConfig"]["properties"]["headers"][HINT_KEY] == {"secret": True}
    assert defs["KnowledgeGitConfig"]["properties"]["repo_url"][HINT_KEY] == {"secret": True}


def test_compaction_override_fields_clear_inherited_values() -> None:
    """An authored null in an override drops the defaults.compaction value, so forms offer a Built-in default choice."""
    properties = Config.model_json_schema()["$defs"]["CompactionOverrideConfig"]["properties"]
    clearing = {name for name, field in properties.items() if field.get(HINT_KEY, {}).get("clears_inherited")}
    # An authored null enabled turns compaction off instead of clearing back to the inherited value.
    assert clearing == set(properties) - {"enabled"}


def test_dashboard_schema_reports_default_factory_values() -> None:
    """Forms show effective defaults for collections and nested blocks."""
    schema = dashboard_config_schema()
    defaults = schema["$defs"]["DefaultsConfig"]["properties"]
    assert defaults["tools"]["default"] == ["scheduler"]
    assert defaults["compaction"]["default"]["enabled"] is True
    assert schema["properties"]["router"]["default"]["model"] == "default"
    # Factories that read other field values have no single default to report.
    assert "default" not in schema["$defs"]["VoiceSTTConfig"]["properties"]["credentials_service"]


def test_dashboard_schema_snapshot_is_current() -> None:
    """Frontend tests render the checked-in snapshot, so it must match the models."""
    snapshot_path = (
        Path(__file__).resolve().parents[1] / "frontend" / "src" / "test" / "fixtures" / "config-schema.json"
    )
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot == dashboard_config_schema(), "Run .venv/bin/python .github/scripts/generate_config_schema.py"
