"""Tests for reactive triggers config, models, dispatch source, and SQLite store."""

from __future__ import annotations

import base64
import sqlite3
from contextlib import closing
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import ValidationError

from mindroom.config.external_trigger_policy import (
    Ed25519EventSourceConfig,
    ExternalTriggerPolicyConfig,
    GitHubEventSourceConfig,
)
from mindroom.config.main import Config
from mindroom.constants import PER_FIRE_THREAD_ROOT_KEY, SOURCE_KIND_KEY, RuntimePaths, resolve_primary_runtime_paths
from mindroom.dispatch_source import (
    REACTIVE_TRIGGER_SOURCE_KIND,
    content_owns_per_fire_thread_root,
    is_automation_source_kind,
    source_kind_allows_self_authored_ingress,
    source_kind_allows_trusted_original_sender,
    source_kind_bypasses_coalescing,
    source_kind_from_content,
)
from mindroom.reactive_triggers import (
    ExternalEventSourceSpec,
    FiringRecord,
    GitHubPRCheckpoint,
    GitHubPRSourceSpec,
    ReactiveTriggerDuplicateError,
    ReactiveTriggerLimitError,
    ReactiveTriggerRecord,
    ReactiveTriggerStore,
    ReactiveTriggerStoreError,
    RoomCursor,
    RoomMessageSourceSpec,
    ThreadCheckpoint,
    ThreadTagSourceSpec,
    TriageSpec,
    TriggerEvent,
    TriggerTargetSpec,
    generate_trigger_id,
)
from mindroom.reactive_triggers.models import TRIGGER_ID_PATTERN

if TYPE_CHECKING:
    from pathlib import Path

_OWNER = "@alice:example.org"
_OTHER_OWNER = "@bob:example.org"
_ROOM_A = "!roomA:example.org"
_ROOM_B = "!roomB:example.org"


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={"MATRIX_HOMESERVER": "https://example.org", "MATRIX_SERVER_NAME": "example.org"},
    )


def _base_config() -> dict[str, object]:
    return {
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
        "router": {"model": "default"},
    }


def _make_record(
    *,
    trigger_id: str | None = None,
    owner_user_id: str = _OWNER,
    source: RoomMessageSourceSpec | ThreadTagSourceSpec | ExternalEventSourceSpec | None = None,
    triage: TriageSpec | None = None,
    target: TriggerTargetSpec | None = None,
    enabled: bool = True,
    once: bool = False,
    max_fires_per_hour: int = 10,
    cooldown_seconds: int = 0,
    expires_at: float | None = None,
    created_at: float = 1000.0,
    updated_at: float = 1000.0,
) -> ReactiveTriggerRecord:
    return ReactiveTriggerRecord(
        trigger_id=trigger_id or generate_trigger_id(),
        owner_user_id=owner_user_id,
        description="test trigger",
        source=source or RoomMessageSourceSpec(room_id=_ROOM_A),
        triage=triage,
        target=target or TriggerTargetSpec(agent="watcher", room_id=_ROOM_A, prompt="Check this event"),
        enabled=enabled,
        once=once,
        max_fires_per_hour=max_fires_per_hour,
        cooldown_seconds=cooldown_seconds,
        expires_at=expires_at,
        created_at=created_at,
        updated_at=updated_at,
    )


def _make_event(
    *,
    source_type: str = "room_message",
    source_id: str = _ROOM_A,
    kind: str = "room.message",
    event_id: str = "$evt1",
    timestamp: float = 1000.0,
    text: str | None = "hello world",
    data: dict[str, object] | None = None,
) -> TriggerEvent:
    return TriggerEvent(
        source_type=source_type,  # type: ignore[arg-type]
        source_id=source_id,
        kind=kind,
        event_id=event_id,
        timestamp=timestamp,
        room_id=_ROOM_A if source_type != "external_event" else None,
        actor="@sender:example.org",
        text=text,
        data=data or {},
    )


# --- Config tests ---


def test_external_trigger_policy_reactive_defaults() -> None:
    """New reactive policy fields default safely without breaking existing configs."""
    config = Config.model_validate(_base_config())
    policy = config.external_trigger_policy

    assert policy.reactive_enabled is True
    assert policy.max_reactive_triggers_per_owner == 50
    assert policy.max_fires_per_hour == 60
    assert policy.event_sources == {}


def test_github_and_ed25519_event_source_configs_and_yaml_roundtrip() -> None:
    """Discriminated union parses both GitHub and Ed25519 sources cleanly and dumps YAML-safely."""
    config = Config.model_validate(
        {
            **_base_config(),
            "external_trigger_policy": {
                "reactive_enabled": True,
                "max_reactive_triggers_per_owner": 25,
                "max_fires_per_hour": 30,
                "event_sources": {
                    "github-main": {
                        "kind": "github",
                        "secret_env": "GITHUB_WEBHOOK_SECRET",
                        "allowed_repos": ["mindroom-ai/mindroom"],
                        "subscribers": [_OWNER],
                    },
                    "ci_signed": {
                        "kind": "ed25519",
                        "public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAOhB7/zzhC+HXDdGOdLwJln5NYwm6UNXx3chmQSVTG4",
                        "allowed_kinds": ["deploy.finished"],
                        "subscribers": [_OWNER],
                    },
                },
            },
        },
    )

    sources = config.external_trigger_policy.event_sources
    github = sources["github-main"]
    assert isinstance(github, GitHubEventSourceConfig)
    assert github.secret_env == "GITHUB_WEBHOOK_SECRET"  # noqa: S105
    assert github.allowed_repos == ["mindroom-ai/mindroom"]
    assert github.allowed_events == [
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "issue_comment",
        "check_run",
        "check_suite",
        "release",
        "issues",
        "push",
    ]
    assert github.max_body_bytes == 262144

    ed25519 = sources["ci_signed"]
    assert isinstance(ed25519, Ed25519EventSourceConfig)
    assert ed25519.key_id == "default"
    assert ed25519.replay_window_seconds == 300
    assert ed25519.max_body_bytes == 65536

    yaml_text = yaml.dump(config.authored_model_dump())
    loaded = yaml.safe_load(yaml_text)
    assert (
        loaded["external_trigger_policy"]["event_sources"]["github-main"]["secret_env"] == "GITHUB_WEBHOOK_SECRET"  # noqa: S105
    )
    assert "!!python" not in yaml_text


@pytest.mark.parametrize("bad_repos", [[], ["noslash"], ["a/b/c"], ["/repo"], ["owner/"], ["../repo"], ["a/.."], [""]])
def test_github_event_source_rejects_empty_or_malformed_repos(bad_repos: list[str]) -> None:
    """GitHub event sources fail closed on empty or malformed allowed_repos."""
    with pytest.raises(ValidationError):
        GitHubEventSourceConfig(secret_env="GH_SECRET", allowed_repos=bad_repos)  # noqa: S106


@pytest.mark.parametrize("bad_env", ["", "   ", "lowercase", "123_BAD", "BAD-ENV", "HAS SPACE"])
def test_github_event_source_rejects_invalid_secret_env(bad_env: str) -> None:
    """GitHub secret_env must be a valid uppercase POSIX env var name."""
    with pytest.raises(ValidationError):
        GitHubEventSourceConfig(secret_env=bad_env, allowed_repos=["org/repo"])


@pytest.mark.parametrize("bad_source_key", ["", "bad key", "a/b", "x" * 65, "a.b"])
def test_policy_rejects_invalid_event_source_keys(bad_source_key: str) -> None:
    """Event source keys must match ^[A-Za-z0-9_-]{1,64}$."""
    with pytest.raises(ValidationError):
        ExternalTriggerPolicyConfig(
            event_sources={
                bad_source_key: GitHubEventSourceConfig(
                    secret_env="GH_SECRET",  # noqa: S106
                    allowed_repos=["org/repo"],
                ),
            },
        )


def test_ed25519_event_source_validation() -> None:
    """Ed25519 event sources reject empty keys and out-of-range limits."""
    with pytest.raises(ValidationError):
        Ed25519EventSourceConfig(public_key="   ")
    with pytest.raises(ValidationError):
        Ed25519EventSourceConfig(public_key="abc", replay_window_seconds=10)
    with pytest.raises(ValidationError):
        Ed25519EventSourceConfig(public_key="abc", max_body_bytes=512)


# --- dispatch_source tests ---


def test_reactive_trigger_source_kind_registered() -> None:
    """REACTIVE_TRIGGER_SOURCE_KIND integrates with dispatch source helpers."""
    assert REACTIVE_TRIGGER_SOURCE_KIND == "reactive_trigger"
    assert is_automation_source_kind(REACTIVE_TRIGGER_SOURCE_KIND)
    assert source_kind_bypasses_coalescing(REACTIVE_TRIGGER_SOURCE_KIND)
    assert source_kind_allows_trusted_original_sender(REACTIVE_TRIGGER_SOURCE_KIND)
    assert source_kind_allows_self_authored_ingress(REACTIVE_TRIGGER_SOURCE_KIND)
    assert source_kind_from_content({SOURCE_KIND_KEY: REACTIVE_TRIGGER_SOURCE_KIND}) == REACTIVE_TRIGGER_SOURCE_KIND
    assert content_owns_per_fire_thread_root(
        {SOURCE_KIND_KEY: REACTIVE_TRIGGER_SOURCE_KIND, PER_FIRE_THREAD_ROOT_KEY: True},
    )


# --- Models tests ---


def test_generate_trigger_id_matches_pattern() -> None:
    """Generated IDs match TRIGGER_ID_PATTERN and use the tr_ prefix."""
    trigger_id = generate_trigger_id()
    assert trigger_id.startswith("tr_")
    assert len(trigger_id) == 15
    assert TRIGGER_ID_PATTERN.fullmatch(trigger_id) is not None


def test_source_spec_validations() -> None:
    """Source specs validate regex patterns and preserve exact external match types."""
    spec = RoomMessageSourceSpec(room_id=_ROOM_A, pattern=r"deploy\s+prod")
    assert spec.pattern == r"deploy\s+prod"

    with pytest.raises(ValidationError):
        RoomMessageSourceSpec(room_id=_ROOM_A, pattern="[unclosed")

    with pytest.raises(ValidationError):
        RoomMessageSourceSpec(room_id=_ROOM_A, pattern="a" * 257)

    ext = ExternalEventSourceSpec(
        source_id="github",
        kind="pull_request.merged",
        match={"repo": "mindroom-ai/mindroom", "number": 4198, "merged": True},
    )
    roundtripped = ExternalEventSourceSpec.model_validate_json(ext.model_dump_json())
    assert roundtripped.match["repo"] == "mindroom-ai/mindroom"
    assert type(roundtripped.match["number"]) is int
    assert roundtripped.match["number"] == 4198
    assert type(roundtripped.match["merged"]) is bool
    assert roundtripped.match["merged"] is True


def test_triage_spec_validations() -> None:
    """TriageSpec enforces provider-specific model and threshold rules."""
    llm_triage = TriageSpec(instructions="Act if urgent", provider="llm", model="haiku")
    assert llm_triage.model == "haiku"
    assert llm_triage.threshold is None

    with pytest.raises(ValidationError):
        TriageSpec(instructions="Act if urgent", provider="llm", model=None)

    with pytest.raises(ValidationError):
        TriageSpec(instructions="Act if urgent", provider="llm", model="haiku", threshold=0.8)

    typesafe_triage = TriageSpec(instructions="Act if urgent", provider="typesafe")
    assert typesafe_triage.model is None
    assert typesafe_triage.threshold == pytest.approx(0.8)

    decisions_triage = TriageSpec(instructions="Act if urgent", provider="openai_decisions", threshold=0.95)
    assert decisions_triage.threshold == pytest.approx(0.95)

    with pytest.raises(ValidationError):
        TriageSpec(instructions="Act if urgent", provider="typesafe", model="haiku")

    with pytest.raises(ValidationError):
        TriageSpec(instructions="Act if urgent", provider="llm", model="haiku", timeout_seconds=31.0)


def test_target_and_record_origin_validations() -> None:
    """Target rejects conflicting thread placement; origin is restricted to room_message sources."""
    with pytest.raises(ValidationError):
        TriggerTargetSpec(agent="watcher", room_id=_ROOM_A, thread_id="$t1", new_thread=True, prompt="go")

    with pytest.raises(ValidationError):
        TriggerTargetSpec(agent="watcher", room_id=_ROOM_A, new_thread=False, thread_key="group-1", prompt="go")

    valid_new_thread = TriggerTargetSpec(
        agent="watcher",
        room_id=_ROOM_A,
        new_thread=True,
        thread_key="pr-4198",
        prompt="go",
    )
    assert valid_new_thread.thread_key == "pr-4198"

    # origin allowed for same-room room_message
    room_record = _make_record(
        source=RoomMessageSourceSpec(room_id=_ROOM_A),
        target=TriggerTargetSpec(agent="watcher", room_id=_ROOM_A, thread_id="origin", prompt="reply"),
    )
    assert room_record.target.thread_id == "origin"

    # origin rejected when target room differs from source room
    with pytest.raises(ValidationError, match=r"target\.room_id to match source\.room_id"):
        _make_record(
            source=RoomMessageSourceSpec(room_id=_ROOM_A),
            target=TriggerTargetSpec(agent="watcher", room_id=_ROOM_B, thread_id="origin", prompt="reply"),
        )

    # origin rejected for thread_tag and external_event
    with pytest.raises(ValidationError):
        _make_record(
            source=ThreadTagSourceSpec(room_id=_ROOM_A, thread_id="$t1"),
            target=TriggerTargetSpec(agent="watcher", room_id=_ROOM_A, thread_id="origin", prompt="reply"),
        )
    with pytest.raises(ValidationError):
        _make_record(
            source=ExternalEventSourceSpec(source_id="github", kind="pull_request.merged"),
            target=TriggerTargetSpec(agent="watcher", room_id=_ROOM_A, thread_id="origin", prompt="reply"),
        )

    # invalid trigger_id rejected
    with pytest.raises(ValidationError):
        _make_record(trigger_id="bad id!")


# --- SQLite Store tests ---


def test_store_requires_control_state_and_initializes_wal_schema(tmp_path: Path) -> None:
    """Store requires control_state_root and initializes WAL DB with schema_version=1."""
    paths = _runtime_paths(tmp_path)
    no_control = replace(paths, control_state_root=None)
    with pytest.raises(ReactiveTriggerStoreError):
        ReactiveTriggerStore(no_control)

    store = ReactiveTriggerStore(paths)
    assert paths.control_state_root is not None
    expected_db = paths.control_state_root / "reactive_triggers.db"
    assert store.db_path == expected_db
    assert expected_db.exists()

    with closing(sqlite3.connect(expected_db)) as conn:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(journal_mode).lower() == "wal"
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == 2
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "github_pr_checkpoints" in tables


def test_trigger_crud_filtering_quota_and_cascade_delete(tmp_path: Path) -> None:
    """CRUD, filtering, duplicate detection, per-owner quota, and cascade delete work atomically."""
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))

    room_rec = _make_record(
        trigger_id="room_watch",
        owner_user_id=_OWNER,
        source=RoomMessageSourceSpec(room_id=_ROOM_A),
    )
    tag_rec = _make_record(
        trigger_id="tag_watch",
        owner_user_id=_OWNER,
        source=ThreadTagSourceSpec(room_id=_ROOM_B, thread_id="$threadA"),
    )
    ext_rec = _make_record(
        trigger_id="gh_watch",
        owner_user_id=_OTHER_OWNER,
        source=ExternalEventSourceSpec(source_id="github", kind="pull_request.merged"),
    )

    store.create_trigger(
        room_rec,
        initial_cursor=RoomCursor(created_ts=100, logical_event_id="$init"),
        max_per_owner=2,
    )
    store.create_trigger(
        tag_rec,
        initial_checkpoint=ThreadCheckpoint(tag_present=False, state_event_id="$s0", updated_at=1000.0),
        max_per_owner=2,
    )

    # Duplicate ID raises ReactiveTriggerDuplicateError
    with pytest.raises(ReactiveTriggerDuplicateError):
        store.create_trigger(room_rec, max_per_owner=2)

    # Owner quota enforced atomically
    with pytest.raises(ReactiveTriggerLimitError):
        store.create_trigger(_make_record(trigger_id="third_rule", owner_user_id=_OWNER), max_per_owner=2)

    # Different owner has its own quota
    store.create_trigger(ext_rec, max_per_owner=2)

    assert store.get_trigger("room_watch") is not None
    assert store.get_trigger("nonexistent") is None
    assert len(store.list_triggers()) == 3
    assert [r.trigger_id for r in store.list_triggers(owner_user_id=_OWNER)] == ["room_watch", "tag_watch"]
    assert [r.trigger_id for r in store.list_triggers(source_type="external_event")] == ["gh_watch"]
    assert [r.trigger_id for r in store.list_triggers(source_room_id=_ROOM_B)] == ["tag_watch"]
    assert [r.trigger_id for r in store.list_triggers(source_id="github")] == ["gh_watch"]

    # Disable & include_disabled filtering
    disabled = store.set_enabled("room_watch", False)
    assert disabled is not None
    assert disabled.enabled is False
    assert [r.trigger_id for r in store.list_triggers(owner_user_id=_OWNER, include_disabled=False)] == ["tag_watch"]
    assert store.set_enabled("missing", False) is None

    # Re-enable
    reenabled = store.set_enabled("room_watch", True)
    assert reenabled is not None
    assert reenabled.enabled is True

    # Add related firing & thread key binding, then delete and verify cascade
    assert store.enqueue_firing("room_watch", "$ev1", _make_event(event_id="$ev1")) is True
    store.save_thread_key_binding("room_watch", "k1", "$root1", now=1000.0)
    assert store.delete_trigger("room_watch") is True
    assert store.delete_trigger("room_watch") is False
    assert store.get_room_cursor("room_watch") is None
    assert store.get_thread_key_binding("room_watch", "k1", now=1000.0) is None
    assert store.list_due_firings(now=1000.0) == []


def test_room_cursors_and_atomic_enqueue_advance(tmp_path: Path) -> None:
    """Room cursors persist, advance explicitly, and advance atomically with enqueue_firing."""
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    rec = _make_record(trigger_id="room_cursor_rule")
    store.create_trigger(rec, initial_cursor=RoomCursor(created_ts=1000, logical_event_id="$e0"))

    assert store.get_room_cursor("room_cursor_rule") == RoomCursor(created_ts=1000, logical_event_id="$e0")

    store.advance_room_cursor("room_cursor_rule", RoomCursor(created_ts=1050, logical_event_id="$e1"))
    assert store.get_room_cursor("room_cursor_rule") == RoomCursor(created_ts=1050, logical_event_id="$e1")

    inserted = store.enqueue_firing(
        "room_cursor_rule",
        "$e2",
        _make_event(event_id="$e2"),
        advance_cursor=RoomCursor(created_ts=1100, logical_event_id="$e2"),
    )
    assert inserted is True
    assert store.get_room_cursor("room_cursor_rule") == RoomCursor(created_ts=1100, logical_event_id="$e2")

    # Duplicate dedupe_key returns False without double-queueing
    dup = store.enqueue_firing(
        "room_cursor_rule",
        "$e2",
        _make_event(event_id="$e2"),
        advance_cursor=RoomCursor(created_ts=1100, logical_event_id="$e2"),
    )
    assert dup is False
    assert len(store.list_due_firings()) == 1


def test_thread_checkpoint_atomic_update_and_enqueue(tmp_path: Path) -> None:
    """Thread checkpoints update atomically with firing enqueue and deduplicate transitions."""
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    rec = _make_record(
        trigger_id="thread_rule",
        source=ThreadTagSourceSpec(room_id=_ROOM_A, thread_id="$threadA"),
    )
    store.create_trigger(
        rec,
        initial_checkpoint=ThreadCheckpoint(tag_present=False, state_event_id="$s0", updated_at=1000.0),
    )

    cp = store.get_thread_checkpoint("thread_rule")
    assert cp is not None
    assert cp.tag_present is False
    assert cp.state_event_id == "$s0"

    event = _make_event(source_type="thread_tag", kind="thread_tag.added", event_id="$s1")
    enqueued = store.update_thread_checkpoint_and_enqueue(
        "thread_rule",
        tag_present=True,
        state_event_id="$s1",
        firing_dedupe_key="$s1",
        event=event,
    )
    assert enqueued is True

    cp2 = store.get_thread_checkpoint("thread_rule")
    assert cp2 is not None
    assert cp2.tag_present is True
    assert cp2.state_event_id == "$s1"

    # Re-delivering same state event deduplicates
    enqueued_dup = store.update_thread_checkpoint_and_enqueue(
        "thread_rule",
        tag_present=True,
        state_event_id="$s1",
        firing_dedupe_key="$s1",
        event=event,
    )
    assert enqueued_dup is False
    assert len(store.list_due_firings()) == 1

    # Checkpoint-only update (e.g. tag removed re-arms without enqueueing)
    no_enqueue = store.update_thread_checkpoint_and_enqueue(
        "thread_rule",
        tag_present=False,
        state_event_id="$s2",
    )
    assert no_enqueue is False
    cp3 = store.get_thread_checkpoint("thread_rule")
    assert cp3 is not None
    assert cp3.tag_present is False
    assert cp3.state_event_id == "$s2"


def test_mark_triaged_no_preserves_once_trigger(tmp_path: Path) -> None:
    """A triaged_no firing must leave a once=True trigger enabled with fire_count=0."""
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    rec = _make_record(trigger_id="once_triage", once=True)
    store.create_trigger(rec)

    assert store.enqueue_firing("once_triage", "$m1", _make_event(event_id="$m1")) is True
    due = store.list_due_firings()
    assert len(due) == 1
    assert isinstance(due[0], FiringRecord)

    store.mark_triaged_no(due[0].id, reason="Routine chatter")
    assert store.list_due_firings() == []

    # Trigger stays armed
    still_armed = store.get_trigger("once_triage")
    assert still_armed is not None
    assert still_armed.enabled is True
    assert still_armed.fire_count == 0
    assert still_armed.last_fired_at is None

    # Next matching message claims and disables the once=True trigger
    assert store.enqueue_firing("once_triage", "$m2", _make_event(event_id="$m2")) is True
    due2 = store.list_due_firings()
    assert len(due2) == 1
    assert store.claim_firing(due2[0].id, now=2000.0, triage_reason="Urgent alert") == "claimed"

    fired = store.get_trigger("once_triage")
    assert fired is not None
    assert fired.enabled is False
    assert fired.fire_count == 1
    assert fired.last_fired_at == pytest.approx(2000.0)


def test_claim_firing_enforces_once_disabled_expired_cooldown_and_rate_limit(tmp_path: Path) -> None:
    """claim_firing atomically enforces all guards and marks blocked firings skipped."""
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))

    # Missing firing
    assert store.claim_firing(99999, now=1000.0) == "missing"

    # Disabled trigger
    store.create_trigger(_make_record(trigger_id="disabled_rule", enabled=False))
    store.enqueue_firing("disabled_rule", "$d1", _make_event(event_id="$d1"))
    f_disabled = store.list_due_firings()[0]
    assert store.claim_firing(f_disabled.id, now=1000.0) == "disabled"
    assert store.list_due_firings() == []  # marked skipped, won't loop

    # Expired trigger
    store.create_trigger(_make_record(trigger_id="expired_rule", expires_at=1500.0))
    store.enqueue_firing("expired_rule", "$x1", _make_event(event_id="$x1"))
    f_expired = store.list_due_firings()[0]
    assert store.claim_firing(f_expired.id, now=1500.0) == "expired"
    assert store.list_due_firings() == []

    # Once trigger with two queued firings: first claims & disables, second gets disabled & skipped
    store.create_trigger(_make_record(trigger_id="once_rule", once=True))
    store.enqueue_firing("once_rule", "$o1", _make_event(event_id="$o1"))
    store.enqueue_firing("once_rule", "$o2", _make_event(event_id="$o2"))
    f_o1, f_o2 = store.list_due_firings()
    assert store.claim_firing(f_o1.id, now=2000.0) == "claimed"
    assert store.claim_firing(f_o2.id, now=2001.0) == "disabled"
    store.mark_delivered(f_o1.id, "$sent_o1")
    assert store.list_due_firings() == []

    # Cooldown enforcement
    store.create_trigger(_make_record(trigger_id="cooldown_rule", cooldown_seconds=60))
    store.enqueue_firing("cooldown_rule", "$c1", _make_event(event_id="$c1"))
    store.enqueue_firing("cooldown_rule", "$c2", _make_event(event_id="$c2"))
    store.enqueue_firing("cooldown_rule", "$c3", _make_event(event_id="$c3"))
    f_c1, f_c2, f_c3 = store.list_due_firings()
    assert store.claim_firing(f_c1.id, now=3000.0) == "claimed"
    assert store.claim_firing(f_c2.id, now=3059.0) == "cooldown"
    assert store.claim_firing(f_c3.id, now=3060.0) == "claimed"
    store.mark_delivered(f_c1.id, "$sent_c1")
    store.mark_delivered(f_c3.id, "$sent_c3")
    assert store.list_due_firings() == []

    # Hourly rate limit (max_fires_per_hour = 2)
    store.create_trigger(_make_record(trigger_id="rate_rule", max_fires_per_hour=2))
    store.enqueue_firing("rate_rule", "$r1", _make_event(event_id="$r1"))
    store.enqueue_firing("rate_rule", "$r2", _make_event(event_id="$r2"))
    store.enqueue_firing("rate_rule", "$r3", _make_event(event_id="$r3"))
    store.enqueue_firing("rate_rule", "$r4", _make_event(event_id="$r4"))
    f_r1, f_r2, f_r3, f_r4 = store.list_due_firings()

    assert store.claim_firing(f_r1.id, now=4000.0) == "claimed"
    # Even when delivered (without synthetic now), claim timestamp governs the hourly window
    store.mark_delivered(f_r1.id, "$sent_r1")
    assert store.claim_firing(f_r2.id, now=4010.0) == "claimed"
    assert store.claim_firing(f_r3.id, now=4020.0) == "rate_limited"

    # After 3600s from f_r1, slot opens up again
    assert store.claim_firing(f_r4.id, now=7600.0) == "claimed"


def test_delivery_retry_exponential_backoff_and_terminal_failure(tmp_path: Path) -> None:
    """mark_delivery_retry backs off exponentially and marks failed after max_attempts."""
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    store.create_trigger(_make_record(trigger_id="retry_rule"))
    store.enqueue_firing("retry_rule", "$e1", _make_event(event_id="$e1"))
    firing = store.list_due_firings(now=1000.0)[0]
    assert store.claim_firing(firing.id, now=1000.0) == "claimed"

    # Expected backoffs for attempts 1..4: 5, 10, 20, 40; attempt 5 -> failed
    expected_delays = [5, 10, 20, 40]
    t = 1000.0
    for idx, delay in enumerate(expected_delays, start=1):
        result = store.mark_delivery_retry(firing.id, f"transient error {idx}", now=t)
        assert result == "retrying"
        # Not due just before retry time
        assert store.list_due_firings(now=t + delay - 0.1) == []
        # Due at retry time
        due = store.list_due_firings(now=t + delay)
        assert len(due) == 1
        assert due[0].attempt_count == idx
        assert due[0].next_retry_at == pytest.approx(t + delay)
        assert due[0].error_message == f"transient error {idx}"
        t += delay

    # 5th attempt -> terminal failed
    assert store.mark_delivery_retry(firing.id, "final error", now=t) == "failed"
    assert store.list_due_firings(now=t + 10_000.0) == []

    # Verify 300s backoff cap with higher max_attempts
    store.enqueue_firing("retry_rule", "$e2", _make_event(event_id="$e2"))
    firing2 = store.list_due_firings(now=2000.0)[0]
    assert store.claim_firing(firing2.id, now=2000.0) == "claimed"
    for _ in range(7):
        assert store.mark_delivery_retry(firing2.id, "err", now=2000.0, max_attempts=10) == "retrying"
    due2 = store.list_due_firings(now=2300.0)
    assert len(due2) == 1
    assert due2[0].next_retry_at == pytest.approx(2300.0)


def test_thread_key_bindings_and_ttl(tmp_path: Path) -> None:
    """Thread key bindings persist, update in place, and honor TTL."""
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    store.create_trigger(_make_record(trigger_id="tk_rule"))

    assert store.get_thread_key_binding("tk_rule", "pr-4198", now=1000.0) is None

    store.save_thread_key_binding("tk_rule", "pr-4198", "$root1", now=1000.0)
    assert store.get_thread_key_binding("tk_rule", "pr-4198", ttl_seconds=600.0, now=1599.0) == "$root1"
    assert store.get_thread_key_binding("tk_rule", "pr-4198", ttl_seconds=600.0, now=1600.0) is None

    # Update binding refreshes timestamp and root
    store.save_thread_key_binding("tk_rule", "pr-4198", "$root2", now=2000.0)
    assert store.get_thread_key_binding("tk_rule", "pr-4198", ttl_seconds=600.0, now=2100.0) == "$root2"


def test_prune_old_firings_only_deletes_old_finished_rows(tmp_path: Path) -> None:
    """Pruning removes old finished rows while keeping recent finished and active rows."""
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    store.create_trigger(_make_record(trigger_id="prune_rule"))

    for key in [
        "$old_delivered",
        "$old_skipped",
        "$old_triaged_no",
        "$old_failed",
        "$old_pending",
        "$old_claimed",
        "$recent_delivered",
    ]:
        store.enqueue_firing("prune_rule", key, _make_event(event_id=key))

    firings = {f.dedupe_key: f.id for f in store.list_due_firings()}

    store.claim_firing(firings["$old_delivered"], now=100.0)
    store.mark_delivered(firings["$old_delivered"], "$m1")
    store.mark_skipped(firings["$old_skipped"], "no auth")
    store.mark_triaged_no(firings["$old_triaged_no"], "noise")
    store.claim_firing(firings["$old_failed"], now=100.0)
    store.mark_delivery_retry(firings["$old_failed"], "boom", now=100.0, max_attempts=1)
    store.claim_firing(firings["$old_claimed"], now=100.0)
    store.claim_firing(firings["$recent_delivered"], now=100.0)
    store.mark_delivered(firings["$recent_delivered"], "$m2")

    # Backdate old rows to timestamp 100.0; keep recent_delivered at 9900.0
    with closing(sqlite3.connect(store.db_path)) as conn:
        conn.execute(
            "UPDATE trigger_firings SET updated_at = 100.0 WHERE dedupe_key != '$recent_delivered'",
        )
        conn.execute(
            "UPDATE trigger_firings SET updated_at = 9900.0 WHERE dedupe_key = '$recent_delivered'",
        )
        conn.commit()

    pruned = store.prune_old_firings(retention_seconds=1000.0, now=10000.0)
    assert pruned == 4

    with closing(sqlite3.connect(store.db_path)) as conn:
        remaining = {row[0] for row in conn.execute("SELECT dedupe_key FROM trigger_firings").fetchall()}

    assert remaining == {"$old_pending", "$old_claimed", "$recent_delivered"}


def test_firing_state_transition_guards(tmp_path: Path) -> None:
    """State transition helpers reject invalid source states without mutating rows or triggers."""
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    store.create_trigger(_make_record(trigger_id="guard_once", once=True))

    # Pending firing: mark_delivered returns False; mark_delivery_retry raises without mutating row or once trigger
    assert store.enqueue_firing("guard_once", "$p1", _make_event(event_id="$p1")) is True
    pending = store.list_due_firings()[0]
    assert store.mark_delivered(pending.id, "$sent_bad") is False
    with pytest.raises(ReactiveTriggerStoreError, match="expected 'claimed'"):
        store.mark_delivery_retry(pending.id, "should fail", now=1000.0)

    still_pending = store.get_firing(pending.id)
    assert still_pending is not None
    assert still_pending.status == "pending"
    assert still_pending.attempt_count == 0
    assert still_pending.error_message is None
    assert still_pending.matrix_event_id is None
    trigger = store.get_trigger("guard_once")
    assert trigger is not None
    assert trigger.enabled is True
    assert trigger.fire_count == 0

    # Claimed firing: mark_triaged_no returns False and leaves row claimed
    assert store.claim_firing(pending.id, now=1000.0) == "claimed"
    assert store.mark_triaged_no(pending.id, reason="too late") is False
    claimed = store.get_firing(pending.id)
    assert claimed is not None
    assert claimed.status == "claimed"
    assert claimed.triage_reason is None

    # Delivered firing: second mark_delivered, mark_triaged_no, mark_skipped return False; retry raises
    assert store.mark_delivered(pending.id, "$sent_ok", now=1001.0) is True
    assert store.mark_delivered(pending.id, "$sent_dup", now=1002.0) is False
    assert store.mark_triaged_no(pending.id, reason="after delivery") is False
    assert store.mark_skipped(pending.id, "after delivery", now=1003.0) is False
    with pytest.raises(ReactiveTriggerStoreError, match="expected 'claimed'"):
        store.mark_delivery_retry(pending.id, "after delivery", now=1004.0)

    delivered = store.get_firing(pending.id)
    assert delivered is not None
    assert delivered.status == "delivered"
    assert delivered.matrix_event_id == "$sent_ok"
    assert delivered.error_message is None


def test_mark_skipped_transition_guards(tmp_path: Path) -> None:
    """mark_skipped applies to pending and claimed firings and rejects terminal states."""
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    store.create_trigger(_make_record(trigger_id="skip_rule"))
    store.enqueue_firing("skip_rule", "$s_pending", _make_event(event_id="$s_pending"))
    store.enqueue_firing("skip_rule", "$s_claimed", _make_event(event_id="$s_claimed"))
    store.enqueue_firing("skip_rule", "$s_triaged", _make_event(event_id="$s_triaged"))
    f_pending, f_claimed, f_triaged = store.list_due_firings()

    assert store.mark_skipped(f_pending.id, "owner unsubscribed") is True
    assert store.mark_skipped(f_pending.id, "already skipped") is False

    assert store.claim_firing(f_claimed.id, now=2000.0) == "claimed"
    assert store.mark_skipped(f_claimed.id, "owner left room") is True
    skipped_claimed = store.get_firing(f_claimed.id)
    assert skipped_claimed is not None
    assert skipped_claimed.status == "skipped"
    assert skipped_claimed.error_message == "owner left room"

    assert store.mark_triaged_no(f_triaged.id, reason="noise") is True
    assert store.mark_skipped(f_triaged.id, "should not overwrite") is False
    triaged = store.get_firing(f_triaged.id)
    assert triaged is not None
    assert triaged.status == "triaged_no"


def test_advance_room_cursor_is_monotonic(tmp_path: Path) -> None:
    """advance_room_cursor never regresses to an older (created_ts, logical_event_id)."""
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    store.create_trigger(
        _make_record(trigger_id="monotonic_cursor"),
        initial_cursor=RoomCursor(created_ts=2000, logical_event_id="$e2"),
    )

    # Older timestamp does not regress
    store.advance_room_cursor("monotonic_cursor", RoomCursor(created_ts=1500, logical_event_id="$e9"))
    assert store.get_room_cursor("monotonic_cursor") == RoomCursor(created_ts=2000, logical_event_id="$e2")

    # Same timestamp with smaller logical_event_id does not regress
    store.advance_room_cursor("monotonic_cursor", RoomCursor(created_ts=2000, logical_event_id="$e1"))
    assert store.get_room_cursor("monotonic_cursor") == RoomCursor(created_ts=2000, logical_event_id="$e2")

    # Same timestamp with greater logical_event_id advances
    store.advance_room_cursor("monotonic_cursor", RoomCursor(created_ts=2000, logical_event_id="$e3"))
    assert store.get_room_cursor("monotonic_cursor") == RoomCursor(created_ts=2000, logical_event_id="$e3")

    # Greater timestamp advances even with smaller logical_event_id
    store.advance_room_cursor("monotonic_cursor", RoomCursor(created_ts=2001, logical_event_id="$e0"))
    assert store.get_room_cursor("monotonic_cursor") == RoomCursor(created_ts=2001, logical_event_id="$e0")


def test_ed25519_event_source_normalizes_key_formats_and_validates_subscribers() -> None:
    """Ed25519EventSourceConfig accepts raw base64, OpenSSH, and PEM keys and rejects invalid keys/subscribers."""
    pub = Ed25519PrivateKey.generate().public_key()
    raw_b64 = base64.b64encode(pub.public_bytes(Encoding.Raw, PublicFormat.Raw)).decode("ascii")
    openssh = pub.public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode("ascii") + " ci@example"
    pem = pub.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode("ascii")

    for fmt in (raw_b64, openssh, pem):
        cfg = Ed25519EventSourceConfig(public_key=fmt, subscribers=["@alice:example.org"])
        assert cfg.public_key == raw_b64

    for bad_key in ("not-a-key", "AAAA", "ssh-ed25519 AAAA_bad", "-----BEGIN PUBLIC KEY-----\nbad"):
        with pytest.raises(ValidationError):
            Ed25519EventSourceConfig(public_key=bad_key)

    for bad_sub in ("alice", "alice:example.org", "@alice", "@a b:example.org"):
        with pytest.raises(ValidationError):
            Ed25519EventSourceConfig(public_key=raw_b64, subscribers=[bad_sub])
        with pytest.raises(ValidationError):
            GitHubEventSourceConfig(secret_env="GH_SEC", allowed_repos=["org/repo"], subscribers=[bad_sub])  # noqa: S106


def test_expected_rule_uid_prevents_stale_writes_after_rule_replacement(tmp_path: Path) -> None:
    """Deleting rule 'x' (Alice) and recreating 'x' (Bob) blocks stale enqueue/cursor/checkpoint/toggle/delete."""
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    alice_rule = _make_record(trigger_id="shared_x", owner_user_id="@alice:example.org")
    store.create_trigger(alice_rule)

    assert store.delete_trigger("shared_x", expected_rule_uid=alice_rule.rule_uid) is True

    bob_rule = _make_record(trigger_id="shared_x", owner_user_id="@bob:example.org")
    assert bob_rule.rule_uid != alice_rule.rule_uid
    store.create_trigger(bob_rule)

    # Stale expected_rule_uid from Alice's deleted rule must cleanly no-op (False / None)
    assert (
        store.enqueue_firing(
            "shared_x",
            "$alice-ev",
            _make_event(event_id="$alice-ev"),
            expected_rule_uid=alice_rule.rule_uid,
        )
        is False
    )
    assert (
        store.update_thread_checkpoint_and_enqueue(
            "shared_x",
            tag_present=True,
            state_event_id="$st1",
            firing_dedupe_key="$alice-tag",
            event=_make_event(event_id="$alice-tag"),
            expected_rule_uid=alice_rule.rule_uid,
        )
        is False
    )
    assert (
        store.advance_room_cursor(
            "shared_x",
            RoomCursor(created_ts=9999, logical_event_id="$ev"),
            expected_rule_uid=alice_rule.rule_uid,
        )
        is False
    )
    assert store.set_enabled("shared_x", False, expected_rule_uid=alice_rule.rule_uid) is None
    assert store.delete_trigger("shared_x", expected_rule_uid=alice_rule.rule_uid) is False

    # Bob's rule is untouched and has zero firings
    live_bob = store.get_trigger("shared_x")
    assert live_bob is not None
    assert live_bob.owner_user_id == "@bob:example.org"
    assert live_bob.rule_uid == bob_rule.rule_uid
    assert live_bob.enabled is True
    assert store.list_due_firings() == []


def test_disabled_reason_distinguishes_manual_from_once_consumed(tmp_path: Path) -> None:
    """claim_firing on once=True sets disabled_reason='once_consumed'; set_enabled sets 'manual' or None."""
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    store.create_trigger(_make_record(trigger_id="once_r", once=True))
    store.enqueue_firing("once_r", "$e1", _make_event(event_id="$e1"))
    firing = store.list_due_firings()[0]

    assert store.claim_firing(firing.id, now=1000.0) == "claimed"
    consumed = store.get_trigger("once_r")
    assert consumed is not None
    assert consumed.enabled is False
    assert consumed.disabled_reason == "once_consumed"

    reenabled = store.set_enabled("once_r", True)
    assert reenabled is not None
    assert reenabled.enabled is True
    assert reenabled.disabled_reason is None

    manual = store.set_enabled("once_r", False)
    assert manual is not None
    assert manual.enabled is False
    assert manual.disabled_reason == "manual"


def test_claim_firing_enforces_live_policy_max_fires_per_hour(tmp_path: Path) -> None:
    """Lowering policy_max_fires_per_hour immediately rate-limits an existing rule with a higher per-rule cap."""
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    store.create_trigger(_make_record(trigger_id="high_cap", max_fires_per_hour=60))
    store.enqueue_firing("high_cap", "$e1", _make_event(event_id="$e1"))
    store.enqueue_firing("high_cap", "$e2", _make_event(event_id="$e2"))
    f1, f2 = store.list_due_firings(now=1000.0)

    assert store.claim_firing(f1.id, now=1000.0, policy_max_fires_per_hour=1) == "claimed"
    assert store.can_claim_now(f2.id, now=1010.0, policy_max_fires_per_hour=1) == "rate_limited"
    assert store.claim_firing(f2.id, now=1010.0, policy_max_fires_per_hour=1) == "rate_limited"


def test_list_due_firings_isolates_corrupt_event_json_row(tmp_path: Path) -> None:
    """A single corrupt event_json row is marked failed without aborting the remaining valid due firings."""
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    store.create_trigger(_make_record(trigger_id="corrupt_test"))
    store.enqueue_firing("corrupt_test", "$bad", _make_event(event_id="$bad"))
    store.enqueue_firing("corrupt_test", "$good", _make_event(event_id="$good"))

    with closing(sqlite3.connect(store.db_path)) as conn:
        conn.execute("UPDATE trigger_firings SET event_json = '{not valid json' WHERE dedupe_key = '$bad'")
        conn.commit()

    due = store.list_due_firings(now=1000.0)
    assert [f.dedupe_key for f in due] == ["$good"]

    with closing(sqlite3.connect(store.db_path)) as conn:
        row = conn.execute("SELECT status, error_message FROM trigger_firings WHERE dedupe_key = '$bad'").fetchone()
    assert row == ("failed", "corrupt_event_json")


def test_update_trigger_preserves_uid_stats_cursors_checkpoints_and_thread_keys(tmp_path: Path) -> None:
    """update_trigger updates mutable columns/spec atomically while preserving rule_uid, stats, and auxiliary tables."""
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    rec = _make_record(trigger_id="upd_rule", once=False, cooldown_seconds=10, max_fires_per_hour=5)
    store.create_trigger(
        rec,
        initial_cursor=RoomCursor(created_ts=5000, logical_event_id="$c0"),
        initial_checkpoint=ThreadCheckpoint(tag_present=True, state_event_id="$s0", updated_at=100.0),
    )
    store.save_thread_key_binding("upd_rule", "tk1", "$root1", expected_rule_uid=rec.rule_uid)
    store.enqueue_firing("upd_rule", "$e1", _make_event(event_id="$e1"))
    assert store.claim_firing(1, now=2000.0) == "claimed"
    assert store.mark_delivered(1, "$sent1", now=2001.0) is True

    modified = rec.model_copy(
        update={
            "description": "updated desc",
            "once": True,
            "cooldown_seconds": 90,
            "max_fires_per_hour": 12,
            "expires_at": 9999.0,
            "fire_count": 0,
            "last_fired_at": None,
            "updated_at": 3000.0,
        },
    )
    assert store.update_trigger(modified, expected_rule_uid="wrong_uid") is None
    updated = store.update_trigger(modified, expected_rule_uid=rec.rule_uid)
    assert updated is not None
    assert updated.rule_uid == rec.rule_uid
    assert updated.description == "updated desc"
    assert updated.once is True
    assert updated.cooldown_seconds == 90
    assert updated.max_fires_per_hour == 12
    assert updated.expires_at == 9999.0
    assert updated.fire_count == 1
    assert updated.last_fired_at == 2000.0
    assert store.get_room_cursor("upd_rule") == RoomCursor(created_ts=5000, logical_event_id="$c0")
    cp = store.get_thread_checkpoint("upd_rule")
    assert cp is not None
    assert cp.tag_present is True
    assert store.get_thread_key_binding("upd_rule", "tk1") == "$root1"


def test_list_firings_filters_orders_desc_and_clamps_limit(tmp_path: Path) -> None:
    """list_firings orders by id DESC, filters by trigger_id and owner_user_id, and clamps limit to [1, 100]."""
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    store.create_trigger(_make_record(trigger_id="r_alice", owner_user_id=_OWNER))
    store.create_trigger(_make_record(trigger_id="r_bob", owner_user_id="@bob:example.org"))
    store.enqueue_firing("r_alice", "$a1", _make_event(event_id="$a1"))
    store.enqueue_firing("r_bob", "$b1", _make_event(event_id="$b1"))
    store.enqueue_firing("r_alice", "$a2", _make_event(event_id="$a2"))

    all_firings = store.list_firings()
    assert [f.dedupe_key for f in all_firings] == ["$a2", "$b1", "$a1"]
    assert [f.dedupe_key for f in store.list_firings(before_id=all_firings[0].id)] == ["$b1", "$a1"]
    assert [f.dedupe_key for f in store.list_firings(owner_user_id=_OWNER, before_id=all_firings[0].id)] == ["$a1"]
    assert [f.dedupe_key for f in store.list_firings(owner_user_id=_OWNER)] == ["$a2", "$a1"]
    assert [f.dedupe_key for f in store.list_firings(owner_user_id="@bob:example.org")] == ["$b1"]
    assert [f.dedupe_key for f in store.list_firings(trigger_id="r_alice", limit=1)] == ["$a2"]
    assert [f.dedupe_key for f in store.list_firings(limit=0)] == ["$a2"]
    assert [f.dedupe_key for f in store.list_firings(trigger_ids=["r_alice"])] == ["$a2", "$a1"]
    assert [r.trigger_id for r in store.list_triggers_with_firings(owner_user_ids=[_OWNER])] == ["r_alice"]


def test_update_trigger_preserves_live_enabled_and_disabled_reason_by_default(tmp_path: Path) -> None:
    """update_trigger(update_enabled=False) keeps live enabled/disabled_reason synced in columns and spec_json."""
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    rec = _make_record(trigger_id="once_race", once=True)
    store.create_trigger(rec)
    store.enqueue_firing("once_race", "$e1", _make_event(event_id="$e1"))
    # Concurrent claim consumes once=True and disables the row live in SQLite
    assert store.claim_firing(1, now=2000.0) == "claimed"

    # An unrelated update built from the stale enabled=True snapshot must NOT re-enable the trigger
    stale_update = rec.model_copy(update={"description": "new desc", "enabled": True, "disabled_reason": None})
    preserved = store.update_trigger(stale_update, expected_rule_uid=rec.rule_uid)
    assert preserved is not None
    assert preserved.description == "new desc"
    assert preserved.enabled is False
    assert preserved.disabled_reason == "once_consumed"
    # Verify spec_json itself was synced with the live enabled/disabled_reason inside the transaction
    with store._read_connection() as conn:
        raw_spec = conn.execute(
            "SELECT spec_json FROM reactive_triggers WHERE trigger_id = ?",
            ("once_race",),
        ).fetchone()
    assert raw_spec is not None
    spec_record = ReactiveTriggerRecord.model_validate_json(str(raw_spec["spec_json"]))
    assert (spec_record.enabled, spec_record.disabled_reason) == (False, "once_consumed")

    # Explicitly setting update_enabled=True does re-enable the trigger in both columns and spec_json
    reenabled = store.update_trigger(stale_update, expected_rule_uid=rec.rule_uid, update_enabled=True)
    assert reenabled is not None
    assert (reenabled.enabled, reenabled.disabled_reason) == (True, None)


def test_schema_v1_migrates_to_v2_preserving_rows_and_rejects_unknown_version(tmp_path: Path) -> None:
    """Opening a schema v1 DB upgrades it cleanly to v2 without losing existing triggers or firings."""
    paths = _runtime_paths(tmp_path)
    assert paths.control_state_root is not None
    db_path = paths.control_state_root / "reactive_triggers.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    existing_rec = _make_record(trigger_id="legacy_msg")
    existing_event = _make_event(event_id="$legacy1")
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
            INSERT INTO schema_version (version) VALUES (1);
            CREATE TABLE reactive_triggers (
                trigger_id TEXT PRIMARY KEY,
                rule_uid TEXT NOT NULL,
                owner_user_id TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_room_id TEXT,
                source_id TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                disabled_reason TEXT,
                once INTEGER NOT NULL DEFAULT 0,
                expires_at REAL,
                max_fires_per_hour INTEGER NOT NULL,
                cooldown_seconds INTEGER NOT NULL DEFAULT 0,
                fire_count INTEGER NOT NULL DEFAULT 0,
                last_fired_at REAL,
                spec_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE room_cursors (
                trigger_id TEXT PRIMARY KEY REFERENCES reactive_triggers(trigger_id) ON DELETE CASCADE,
                created_ts INTEGER NOT NULL,
                logical_event_id TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE thread_checkpoints (
                trigger_id TEXT PRIMARY KEY REFERENCES reactive_triggers(trigger_id) ON DELETE CASCADE,
                tag_present INTEGER NOT NULL,
                state_event_id TEXT,
                updated_at REAL NOT NULL
            );
            CREATE TABLE trigger_firings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_id TEXT NOT NULL REFERENCES reactive_triggers(trigger_id) ON DELETE CASCADE,
                rule_uid TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                status TEXT NOT NULL,
                event_json TEXT NOT NULL,
                triage_reason TEXT,
                matrix_event_id TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at REAL,
                error_message TEXT,
                claimed_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(trigger_id, dedupe_key)
            );
            CREATE TABLE trigger_thread_keys (
                trigger_id TEXT NOT NULL REFERENCES reactive_triggers(trigger_id) ON DELETE CASCADE,
                thread_key TEXT NOT NULL,
                thread_root_id TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (trigger_id, thread_key)
            );
            """,
        )
        conn.execute(
            """
            INSERT INTO reactive_triggers (
                trigger_id, rule_uid, owner_user_id, source_type, source_room_id, source_id,
                enabled, disabled_reason, once, expires_at, max_fires_per_hour, cooldown_seconds,
                fire_count, last_fired_at, spec_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'room_message', ?, NULL, 1, NULL, 0, NULL, 10, 0, 0, NULL, ?, 1000.0, 1000.0)
            """,
            (
                existing_rec.trigger_id,
                existing_rec.rule_uid,
                existing_rec.owner_user_id,
                "!room:example.org",
                existing_rec.model_dump_json(),
            ),
        )
        conn.execute(
            """
            INSERT INTO trigger_firings (
                trigger_id, rule_uid, dedupe_key, status, event_json, attempt_count, created_at, updated_at
            ) VALUES (?, ?, '$legacy1', 'pending', ?, 0, 1000.0, 1000.0)
            """,
            (existing_rec.trigger_id, existing_rec.rule_uid, existing_event.model_dump_json()),
        )
        conn.commit()

    store = ReactiveTriggerStore(paths)
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 2
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "github_pr_checkpoints" in tables

    loaded = store.get_trigger("legacy_msg")
    assert loaded is not None
    assert loaded.rule_uid == existing_rec.rule_uid
    firings = store.list_firings(trigger_id="legacy_msg")
    assert [f.dedupe_key for f in firings] == ["$legacy1"]

    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("UPDATE schema_version SET version = 99")
        conn.commit()
    with pytest.raises(ReactiveTriggerStoreError, match="Unsupported reactive triggers schema version: 99"):
        ReactiveTriggerStore(paths)


def test_github_pr_checkpoint_terminal_claim_and_owner_quota(tmp_path: Path) -> None:
    """Terminal github_pr claim sets disabled_reason='pr_closed'; quota enforces limit and nudges preserve order."""
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    source = GitHubPRSourceSpec(repo="MindRoom-AI/mindroom", number=42)
    rec = ReactiveTriggerRecord(
        trigger_id="gh_pr_42",
        owner_user_id=_OWNER,
        source=source,
        target=TriggerTargetSpec(agent="dev", room_id="!room:example.org", prompt="Handle PR"),
        created_at=1000.0,
        updated_at=1000.0,
    )
    cp = GitHubPRCheckpoint(head_sha="abc123", seeded=True, next_poll_at=1100.0, updated_at=1000.0)
    store.create_trigger(rec, initial_github_pr_checkpoint=cp, max_github_pr_per_owner=1)

    assert [r.trigger_id for r in store.list_triggers(source_type="github_pr", source_id="mindroom-ai/mindroom")] == [
        "gh_pr_42",
    ]
    assert store.get_github_pr_checkpoint("gh_pr_42") == cp

    second = rec.model_copy(update={"trigger_id": "gh_pr_43", "source": GitHubPRSourceSpec(repo="org/repo", number=43)})
    with pytest.raises(ReactiveTriggerLimitError, match="GitHub PR watch limit"):
        store.create_trigger(second, max_github_pr_per_owner=1)

    merged_event = TriggerEvent(
        source_type="github_pr",
        source_id="mindroom-ai/mindroom",
        kind="github_pr.merged",
        event_id="deliv_merge",
        timestamp=1200.0,
        data={"repo": "MindRoom-AI/mindroom", "number": 42, "terminal": True, "state": "merged", "merged": True},
    )
    merged_cp = cp.model_copy(update={"pr_state": "merged", "updated_at": 1200.0})
    assert store.update_github_pr_checkpoint_and_enqueue(
        "gh_pr_42",
        merged_cp,
        event=merged_event,
        expected_rule_uid=rec.rule_uid,
    )
    before_claim = store.get_trigger("gh_pr_42")
    assert before_claim is not None
    assert before_claim.enabled is True

    firing = store.list_due_firings(now=1200.0)[0]
    assert store.claim_firing(firing.id, now=1201.0) == "claimed"
    after_claim = store.get_trigger("gh_pr_42")
    assert after_claim is not None
    assert (after_claim.enabled, after_claim.disabled_reason) == (False, "pr_closed")

    wh_rec = rec.model_copy(
        update={
            "trigger_id": "gh_pr_wh",
            "source": GitHubPRSourceSpec(repo="MindRoom-AI/mindroom", number=99, webhook_source_id="github"),
        },
    )
    wh_cp = GitHubPRCheckpoint(head_sha="sha99", seeded=True, next_poll_at=None, updated_at=1000.0)
    store.create_trigger(wh_rec, initial_github_pr_checkpoint=wh_cp, max_github_pr_per_owner=1)
    assert store.list_due_github_pr_triggers(now=1500.0) == []
    assert store.nudge_github_pr_checkpoints(["gh_pr_wh"], now=1500.0) == 1
    assert [r.trigger_id for r, _ in store.list_due_github_pr_triggers(now=1500.0)] == ["gh_pr_wh"]
    assert store.nudge_github_pr_checkpoints(["gh_pr_wh"], now=1505.0) == 1
    store.update_github_pr_checkpoint_and_enqueue(
        "gh_pr_wh",
        wh_cp.model_copy(update={"next_poll_at": None, "updated_at": 1500.0}),
        expected_rule_uid=wh_rec.rule_uid,
        poll_started_at=1500.0,
    )
    preserved = store.get_github_pr_checkpoint("gh_pr_wh")
    assert preserved is not None
    assert preserved.next_poll_at == 1505.0


def test_pr_closed_and_expired_watches_do_not_consume_owner_quotas(tmp_path: Path) -> None:
    """Auto-retired pr_closed watches and expired PR watches do not consume owner trigger quotas."""
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))

    def _pr_rec(
        trigger_id: str,
        number: int,
        *,
        created_at: float = 1000.0,
        expires_at: float | None = None,
    ) -> ReactiveTriggerRecord:
        return ReactiveTriggerRecord(
            trigger_id=trigger_id,
            owner_user_id=_OWNER,
            source=GitHubPRSourceSpec(repo="MindRoom-AI/mindroom", number=number),
            target=TriggerTargetSpec(agent="dev", room_id="!room:example.org", prompt="Watch PR"),
            expires_at=expires_at,
            created_at=created_at,
            updated_at=created_at,
        )

    rec1 = _pr_rec("gh_pr_1", 1)
    cp1 = GitHubPRCheckpoint(head_sha="sha1", seeded=True, next_poll_at=1100.0, updated_at=1000.0)
    store.create_trigger(rec1, initial_github_pr_checkpoint=cp1, max_per_owner=1, max_github_pr_per_owner=1)

    rec2 = _pr_rec("gh_pr_2", 2, created_at=1050.0)
    with pytest.raises(ReactiveTriggerLimitError, match="reactive trigger limit"):
        store.create_trigger(rec2, max_per_owner=1, max_github_pr_per_owner=1)

    # Auto-disable first watch with pr_closed -> excluded from both max_per_owner and max_github_pr_per_owner.
    store.update_github_pr_checkpoint_and_enqueue(
        "gh_pr_1",
        cp1.model_copy(update={"pr_state": "closed", "updated_at": 1020.0}),
        disable_reason="pr_closed",
        expected_rule_uid=rec1.rule_uid,
    )
    store.create_trigger(rec2, max_per_owner=1, max_github_pr_per_owner=1)

    # Reviving pr_closed enforces overall max_per_owner (even when max_github_pr_per_owner has headroom).
    with pytest.raises(ReactiveTriggerLimitError, match="reactive trigger limit"):
        store.set_enabled("gh_pr_1", enabled=True, max_per_owner=1, max_github_pr_per_owner=5)
    with pytest.raises(ReactiveTriggerLimitError, match="reactive trigger limit"):
        store.update_trigger(
            rec1.model_copy(update={"enabled": True, "disabled_reason": None, "updated_at": 1060.0}),
            update_enabled=True,
            max_per_owner=1,
            max_github_pr_per_owner=5,
        )
    still_closed = store.set_enabled("gh_pr_1", enabled=False)
    assert still_closed is not None
    assert still_closed.disabled_reason == "pr_closed"

    # Expired PR watch does not consume max_github_pr_per_owner.
    store.delete_trigger("gh_pr_2")
    expired_pr = _pr_rec("gh_pr_expired", 3, created_at=1000.0, expires_at=1100.0)
    store.create_trigger(expired_pr, max_github_pr_per_owner=1)
    fresh_pr = _pr_rec("gh_pr_fresh", 4, created_at=1200.0)
    store.create_trigger(fresh_pr, max_github_pr_per_owner=1)
