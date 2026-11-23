"""Tests for shared external event sources (GitHub HMAC-SHA256 + Ed25519 fan-out API)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import time
from typing import TYPE_CHECKING, cast

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from mindroom.api import config_lifecycle
from mindroom.api import main as api_main
from mindroom.config.external_trigger_policy import (
    Ed25519EventSourceConfig,
    GitHubEventSourceConfig,
    HmacEventSourceConfig,
)
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths
from mindroom.external_triggers.auth import sign_trigger_request
from mindroom.external_triggers.store import ExternalTriggerStore, ExternalTriggerTarget, external_trigger_replay_store
from mindroom.reactive_triggers import (
    ExternalEventSourceSpec,
    ExternalSourceAuthError,
    ExternalSourceBadRequestError,
    ExternalSourceForbiddenError,
    ExternalSourceReplayError,
    ExternalSourceUnavailableError,
    ReactiveTriggerRecord,
    ReactiveTriggerStore,
    TriggerEvent,
    TriggerTargetSpec,
    check_external_event_source_access,
    fan_out_external_event,
    is_source_subscriber_allowed,
    validate_subscription_against_source,
    verify_and_normalize_ed25519_event,
    verify_and_normalize_github_webhook,
    verify_and_normalize_hmac_webhook,
)
from tests.runtime_fakes import bind_test_embedded_runtime

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.bot import AgentBot

_ADMIN = "@admin:example.org"
_TRIGGER_ADMIN = "@trigadmin:example.org"
_ALICE = "@alice:example.org"
_ALICE_ALIAS = "@alice_tg:example.org"
_BOB = "@bob:example.org"
_ROOM = "!room:example.org"
_GH_SECRET_ENV = "GITHUB_WEBHOOK_SECRET"  # noqa: S105 - env var name, not a secret
_GH_SECRET = "super-secret-github-hmac-token"  # noqa: S105 - test fixture secret
_GH_WRONG_SECRET = "wrong-secret"  # noqa: S105 - test fixture secret


def _public_key_b64(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(encoding=Encoding.Raw, format=PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def _github_signature(body: bytes, secret: str = _GH_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _runtime_paths(tmp_path: Path, *, env: dict[str, str] | None = None) -> RuntimePaths:
    process_env = {"MATRIX_HOMESERVER": "https://example.org", "MATRIX_SERVER_NAME": "example.org"}
    if env:
        process_env.update(env)
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env=process_env,
    )


def _config(
    *,
    event_sources: dict[str, object] | None = None,
    enabled: bool = True,
    reactive_enabled: bool = True,
) -> Config:
    return Config.model_validate(
        {
            "administrators": [_ADMIN],
            "authorization": {
                "aliases": {_ALICE: [_ALICE_ALIAS]},
            },
            "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
            "router": {"model": "default"},
            "external_trigger_policy": {
                "enabled": enabled,
                "reactive_enabled": reactive_enabled,
                "admin_users": [_TRIGGER_ADMIN],
                "event_sources": event_sources or {},
            },
        },
    )


def _github_source(
    *,
    secret_env: str = _GH_SECRET_ENV,
    allowed_repos: list[str] | None = None,
    allowed_events: list[str] | None = None,
    subscribers: list[str] | None = None,
    max_body_bytes: int = 262144,
) -> GitHubEventSourceConfig:
    kwargs: dict[str, object] = {
        "secret_env": secret_env,
        "allowed_repos": allowed_repos if allowed_repos is not None else ["mindroom-ai/mindroom"],
        "subscribers": subscribers if subscribers is not None else [_ALICE],
        "max_body_bytes": max_body_bytes,
    }
    if allowed_events is not None:
        kwargs["allowed_events"] = allowed_events
    return GitHubEventSourceConfig.model_validate(kwargs)


def _ed25519_source(
    private_key: Ed25519PrivateKey,
    *,
    key_id: str = "ci-key",
    allowed_kinds: list[str] | None = None,
    subscribers: list[str] | None = None,
    replay_window_seconds: int = 300,
    max_body_bytes: int = 65536,
) -> Ed25519EventSourceConfig:
    return Ed25519EventSourceConfig(
        public_key=_public_key_b64(private_key),
        key_id=key_id,
        allowed_kinds=allowed_kinds or [],
        subscribers=subscribers if subscribers is not None else [_ALICE],
        replay_window_seconds=replay_window_seconds,
        max_body_bytes=max_body_bytes,
    )


def _make_external_rule(
    *,
    trigger_id: str,
    owner_user_id: str = _ALICE,
    source_id: str = "github",
    kind: str = "pull_request.merged",
    match: dict[str, str | bool | int] | None = None,
    enabled: bool = True,
    expires_at: float | None = None,
) -> ReactiveTriggerRecord:
    return ReactiveTriggerRecord(
        trigger_id=trigger_id,
        owner_user_id=owner_user_id,
        source=ExternalEventSourceSpec(
            source_id=source_id,
            kind=kind,
            match=match or {},
        ),
        target=TriggerTargetSpec(
            agent="code",
            room_id=_ROOM,
            prompt="Handle external event",
        ),
        enabled=enabled,
        expires_at=expires_at,
        created_at=1000.0,
        updated_at=1000.0,
    )


# ---------------------------------------------------------------------------
# Subscriber authorization & subscription validation
# ---------------------------------------------------------------------------


def test_is_source_subscriber_allowed_checks_subscribers_admins_and_aliases(tmp_path: Path) -> None:
    """Subscribers, trigger/platform admins, and equivalent bridge aliases are allowed; others denied."""
    config = _config()
    runtime_paths = _runtime_paths(tmp_path)
    source = _github_source(subscribers=[_ALICE])

    assert is_source_subscriber_allowed(source, _ALICE, config, runtime_paths) is True
    assert is_source_subscriber_allowed(source, _ALICE_ALIAS, config, runtime_paths) is True
    assert is_source_subscriber_allowed(source, _ADMIN, config, runtime_paths) is True
    assert is_source_subscriber_allowed(source, _TRIGGER_ADMIN, config, runtime_paths) is True
    assert is_source_subscriber_allowed(source, _BOB, config, runtime_paths) is False

    # Alias listed in source.subscribers also authorizes the canonical owner ID.
    aliased_source = _github_source(subscribers=[_ALICE_ALIAS])
    assert is_source_subscriber_allowed(aliased_source, _ALICE, config, runtime_paths) is True
    assert is_source_subscriber_allowed(aliased_source, _BOB, config, runtime_paths) is False

    # Empty subscribers list is fail-closed for non-admins, still allows admins.
    closed_source = _github_source(subscribers=[])
    assert is_source_subscriber_allowed(closed_source, _ALICE, config, runtime_paths) is False
    assert is_source_subscriber_allowed(closed_source, _ADMIN, config, runtime_paths) is True


def test_validate_subscription_against_github_source() -> None:
    """GitHub subscriptions enforce allowed_events on base event and exact allowed_repos matching."""
    source = _github_source(
        allowed_repos=["MindRoom-AI/MindRoom"],
        allowed_events=["pull_request", "push"],
    )

    validate_subscription_against_source(
        source,
        kind="pull_request.merged",
        match={"repo": "MindRoom-AI/MindRoom", "number": 42},
    )
    validate_subscription_against_source(source, kind="push", match={})

    # Mismatched casing is rejected so validated rules match matches_external_event's exact equality
    with pytest.raises(ValueError, match="Repository"):
        validate_subscription_against_source(
            source,
            kind="pull_request.merged",
            match={"repo": "mindroom-ai/mindroom"},
        )

    with pytest.raises(ValueError, match="allowed"):
        validate_subscription_against_source(source, kind="issues.opened", match={})

    with pytest.raises(ValueError, match="Repository"):
        validate_subscription_against_source(
            source,
            kind="pull_request.merged",
            match={"repo": "other-org/private-repo"},
        )

    with pytest.raises(ValueError, match="string"):
        validate_subscription_against_source(
            source,
            kind="pull_request.merged",
            match={"repo": 123},
        )


def test_validate_subscription_against_ed25519_source() -> None:
    """Ed25519 subscriptions enforce allowed_kinds when non-empty and allow any kind when empty."""
    key = Ed25519PrivateKey.generate()
    open_source = _ed25519_source(key, allowed_kinds=[])
    validate_subscription_against_source(open_source, kind="deploy.finished", match={"env": "prod"})

    restricted_source = _ed25519_source(key, allowed_kinds=["deploy.finished", "deploy.failed"])
    validate_subscription_against_source(restricted_source, kind="deploy.finished", match={})
    with pytest.raises(ValueError, match="allowed"):
        validate_subscription_against_source(restricted_source, kind="other.event", match={})


# ---------------------------------------------------------------------------
# GitHub HMAC-SHA256 verification and payload normalization
# ---------------------------------------------------------------------------


def test_github_webhook_missing_secret_raises_unavailable_without_leaking(tmp_path: Path) -> None:
    """Missing or blank webhook secret raises ExternalSourceUnavailableError."""
    source = _github_source(secret_env=_GH_SECRET_ENV)
    runtime_paths = _runtime_paths(tmp_path)
    body = json.dumps({"repository": {"full_name": "mindroom-ai/mindroom"}}).encode("utf-8")

    with pytest.raises(ExternalSourceUnavailableError, match="not configured"):
        verify_and_normalize_github_webhook(
            source_id="github",
            source=source,
            headers={
                "X-GitHub-Delivery": "deliv-1",
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": _github_signature(body),
            },
            body=body,
            runtime_paths=runtime_paths,
        )


def test_github_webhook_header_and_signature_validation(tmp_path: Path) -> None:
    """GitHub verification rejects missing headers, oversized delivery ID, bad signatures, and bad JSON."""
    source = _github_source()
    runtime_paths = _runtime_paths(tmp_path, env={"GITHUB_WEBHOOK_SECRET": _GH_SECRET})
    body = json.dumps({"repository": {"full_name": "mindroom-ai/mindroom"}}).encode("utf-8")
    sig = _github_signature(body)

    # Missing X-GitHub-Delivery
    with pytest.raises(ExternalSourceBadRequestError, match="X-GitHub-Delivery"):
        verify_and_normalize_github_webhook(
            source_id="github",
            source=source,
            headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": sig},
            body=body,
            runtime_paths=runtime_paths,
        )

    # Oversized X-GitHub-Delivery (> 256 bytes)
    with pytest.raises(ExternalSourceBadRequestError, match="X-GitHub-Delivery"):
        verify_and_normalize_github_webhook(
            source_id="github",
            source=source,
            headers={
                "X-GitHub-Delivery": "d" * 257,
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": sig,
            },
            body=body,
            runtime_paths=runtime_paths,
        )

    # Missing X-GitHub-Event
    with pytest.raises(ExternalSourceBadRequestError, match="X-GitHub-Event"):
        verify_and_normalize_github_webhook(
            source_id="github",
            source=source,
            headers={"X-GitHub-Delivery": "deliv-1", "X-Hub-Signature-256": sig},
            body=body,
            runtime_paths=runtime_paths,
        )

    # Bad signature (wrong secret or missing sha256= prefix)
    for bad_sig in (
        "",
        "sha1=deadbeef",
        "sha256=\xff",
        "sha256=café",
        _github_signature(body, secret=_GH_WRONG_SECRET),
    ):
        with pytest.raises(ExternalSourceAuthError, match="signature") as exc_info:
            verify_and_normalize_github_webhook(
                source_id="github",
                source=source,
                headers={
                    "X-GitHub-Delivery": "deliv-1",
                    "X-GitHub-Event": "push",
                    "X-Hub-Signature-256": bad_sig,
                },
                body=body,
                runtime_paths=runtime_paths,
            )
        assert _GH_SECRET not in str(exc_info.value)

    # Non-JSON and non-object body
    for bad_body in (b"not json", b"[1, 2, 3]"):
        with pytest.raises(ExternalSourceBadRequestError):
            verify_and_normalize_github_webhook(
                source_id="github",
                source=source,
                headers={
                    "X-GitHub-Delivery": "deliv-1",
                    "X-GitHub-Event": "push",
                    "X-Hub-Signature-256": _github_signature(bad_body),
                },
                body=bad_body,
                runtime_paths=runtime_paths,
            )

    # Missing repository.full_name
    no_repo_body = json.dumps({"action": "opened"}).encode("utf-8")
    with pytest.raises(ExternalSourceBadRequestError, match="repository"):
        verify_and_normalize_github_webhook(
            source_id="github",
            source=source,
            headers={
                "X-GitHub-Delivery": "deliv-1",
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": _github_signature(no_repo_body),
            },
            body=no_repo_body,
            runtime_paths=runtime_paths,
        )

    # Disallowed repository -> 403 Forbidden error
    other_repo_body = json.dumps({"repository": {"full_name": "other-org/secret"}}).encode("utf-8")
    with pytest.raises(ExternalSourceForbiddenError, match="Repository"):
        verify_and_normalize_github_webhook(
            source_id="github",
            source=source,
            headers={
                "X-GitHub-Delivery": "deliv-1",
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": _github_signature(other_repo_body),
            },
            body=other_repo_body,
            runtime_paths=runtime_paths,
        )

    # Disallowed event type -> BadRequest
    with pytest.raises(ExternalSourceBadRequestError, match="event type"):
        verify_and_normalize_github_webhook(
            source_id="github",
            source=source,
            headers={
                "X-GitHub-Delivery": "deliv-1",
                "X-GitHub-Event": "star",
                "X-Hub-Signature-256": sig,
            },
            body=body,
            runtime_paths=runtime_paths,
        )


def test_github_webhook_normalizes_pull_request_merged_and_closed(tmp_path: Path) -> None:
    """PR merged vs closed-unmerged normalize distinctly and isolate title/body from structured data."""
    source = _github_source(allowed_repos=["MindRoom-AI/MindRoom"])
    runtime_paths = _runtime_paths(tmp_path, env={"GITHUB_WEBHOOK_SECRET": _GH_SECRET})

    long_title = "T" * 600
    long_body = "B" * 2500
    merged_payload = {
        "action": "closed",
        "repository": {"full_name": "mindroom-ai/mindroom"},
        "sender": {"login": "basnijholt"},
        "pull_request": {
            "number": 123,
            "merged": True,
            "title": long_title,
            "body": long_body,
            "head": {"ref": "feat/triggers", "sha": "headsha123"},
            "base": {"ref": "main"},
            "user": {"login": "dev-author"},
            "merge_commit_sha": "mergesha999",
            "html_url": "https://github.com/mindroom-ai/mindroom/pull/123",
        },
    }
    raw_merged = json.dumps(merged_payload).encode("utf-8")
    event = verify_and_normalize_github_webhook(
        source_id="github",
        source=source,
        headers={
            "x-github-delivery": "delivery-pr-123",
            "x-github-event": "pull_request",
            "x-hub-signature-256": _github_signature(raw_merged),
        },
        body=raw_merged,
        runtime_paths=runtime_paths,
        now=1700000000.0,
    )

    assert event.source_type == "external_event"
    assert event.source_id == "github"
    assert event.kind == "pull_request.merged"
    assert event.event_id == "delivery-pr-123"
    assert event.timestamp == 1700000000.0
    assert event.actor == "basnijholt"
    assert event.title == "T" * 500
    assert event.summary == "B" * 2000
    assert "title" not in event.data
    assert "body" not in event.data
    assert event.data == {
        "repo": "MindRoom-AI/MindRoom",
        "number": 123,
        "action": "closed",
        "merged": True,
        "branch": "feat/triggers",
        "base_branch": "main",
        "author": "dev-author",
        "sha": "mergesha999",
        "url": "https://github.com/mindroom-ai/mindroom/pull/123",
    }

    # Closed without merge -> pull_request.closed and head sha fallback
    closed_payload = {
        "action": "closed",
        "repository": {"full_name": "mindroom-ai/mindroom"},
        "sender": {"login": "basnijholt"},
        "pull_request": {
            "number": 124,
            "merged": False,
            "title": "Abandoned PR",
            "body": None,
            "head": {"ref": "feat/abandoned", "sha": "headsha456"},
            "base": {"ref": "main"},
            "user": {"login": "dev-author"},
            "merge_commit_sha": None,
            "html_url": "https://github.com/mindroom-ai/mindroom/pull/124",
        },
    }
    raw_closed = json.dumps(closed_payload).encode("utf-8")
    closed_event = verify_and_normalize_github_webhook(
        source_id="github",
        source=source,
        headers={
            "X-GitHub-Delivery": "delivery-pr-124",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _github_signature(raw_closed),
        },
        body=raw_closed,
        runtime_paths=runtime_paths,
        now=1700000010.0,
    )
    assert closed_event.kind == "pull_request.closed"
    assert closed_event.data["merged"] is False
    assert closed_event.data["sha"] == "headsha456"
    assert closed_event.summary is None


def test_github_webhook_normalizes_other_event_types(tmp_path: Path) -> None:
    """Review, issues, release, check_suite, push, and generic events normalize cleanly."""
    source = _github_source(
        allowed_events=[
            "pull_request_review",
            "issues",
            "release",
            "check_suite",
            "push",
            "workflow_run",
        ],
    )
    runtime_paths = _runtime_paths(tmp_path, env={"GITHUB_WEBHOOK_SECRET": _GH_SECRET})

    def _run(gh_event: str, payload: dict[str, object], delivery_id: str) -> TriggerEvent:
        body = json.dumps(payload).encode("utf-8")
        return verify_and_normalize_github_webhook(
            source_id="github",
            source=source,
            headers={
                "X-GitHub-Delivery": delivery_id,
                "X-GitHub-Event": gh_event,
                "X-Hub-Signature-256": _github_signature(body),
            },
            body=body,
            runtime_paths=runtime_paths,
            now=1700000100.0,
        )

    review_ev = _run(
        "pull_request_review",
        {
            "action": "submitted",
            "repository": {"full_name": "mindroom-ai/mindroom"},
            "pull_request": {"number": 10, "title": "PR title"},
            "review": {
                "state": "approved",
                "body": "LGTM",
                "user": {"login": "reviewer"},
                "html_url": "https://github.com/mindroom-ai/mindroom/pull/10#review-1",
            },
        },
        "d-review",
    )
    assert review_ev.kind == "pull_request_review.submitted"
    assert review_ev.data == {
        "repo": "mindroom-ai/mindroom",
        "number": 10,
        "action": "submitted",
        "state": "approved",
        "author": "reviewer",
        "url": "https://github.com/mindroom-ai/mindroom/pull/10#review-1",
    }

    issue_ev = _run(
        "issues",
        {
            "action": "opened",
            "repository": {"full_name": "mindroom-ai/mindroom"},
            "issue": {
                "number": 77,
                "title": "Bug report",
                "body": "Details",
                "user": {"login": "reporter"},
                "html_url": "https://github.com/mindroom-ai/mindroom/issues/77",
            },
        },
        "d-issue",
    )
    assert issue_ev.kind == "issues.opened"
    assert issue_ev.data == {
        "repo": "mindroom-ai/mindroom",
        "number": 77,
        "action": "opened",
        "author": "reporter",
        "url": "https://github.com/mindroom-ai/mindroom/issues/77",
    }
    assert issue_ev.title == "Bug report"
    assert issue_ev.summary == "Details"

    release_ev = _run(
        "release",
        {
            "action": "published",
            "repository": {"full_name": "mindroom-ai/mindroom"},
            "release": {
                "tag_name": "v1.2.3",
                "prerelease": True,
                "author": {"login": "releaser"},
                "html_url": "https://github.com/mindroom-ai/mindroom/releases/tag/v1.2.3",
            },
        },
        "d-release",
    )
    assert release_ev.kind == "release.published"
    assert release_ev.data == {
        "repo": "mindroom-ai/mindroom",
        "tag": "v1.2.3",
        "action": "published",
        "prerelease": True,
        "author": "releaser",
        "url": "https://github.com/mindroom-ai/mindroom/releases/tag/v1.2.3",
    }

    cs_ev = _run(
        "check_suite",
        {
            "action": "completed",
            "repository": {"full_name": "mindroom-ai/mindroom"},
            "check_suite": {
                "status": "completed",
                "conclusion": "success",
                "head_branch": "main",
                "head_sha": "abc123",
            },
        },
        "d-cs",
    )
    assert cs_ev.kind == "check_suite.completed"
    assert cs_ev.data == {
        "repo": "mindroom-ai/mindroom",
        "action": "completed",
        "status": "completed",
        "conclusion": "success",
        "branch": "main",
        "sha": "abc123",
    }

    push_ev = _run(
        "push",
        {
            "repository": {"full_name": "mindroom-ai/mindroom"},
            "ref": "refs/heads/main",
            "after": "def456",
            "pusher": {"name": "alice"},
            "compare": "https://github.com/mindroom-ai/mindroom/compare/a...b",
        },
        "d-push",
    )
    assert push_ev.kind == "push"
    assert push_ev.data == {
        "repo": "mindroom-ai/mindroom",
        "ref": "refs/heads/main",
        "branch": "main",
        "after": "def456",
        "pusher": "alice",
        "url": "https://github.com/mindroom-ai/mindroom/compare/a...b",
    }

    tag_push_ev = _run(
        "push",
        {
            "repository": {"full_name": "mindroom-ai/mindroom"},
            "ref": "refs/tags/v1.2.3",
            "after": "def456",
        },
        "d-tag-push",
    )
    assert tag_push_ev.kind == "push"
    assert "branch" not in tag_push_ev.data

    generic_ev = _run(
        "workflow_run",
        {
            "action": "completed",
            "repository": {"full_name": "mindroom-ai/mindroom"},
        },
        "d-generic",
    )
    assert generic_ev.kind == "workflow_run.completed"
    assert generic_ev.data == {"repo": "mindroom-ai/mindroom", "action": "completed"}


# ---------------------------------------------------------------------------
# Ed25519 verification, replay protection, and normalization
# ---------------------------------------------------------------------------


def test_ed25519_event_verification_replay_and_kind_allowlist(tmp_path: Path) -> None:
    """Ed25519 event source verifies signatures, rejects replayed nonces, and enforces allowed_kinds."""
    private_key = Ed25519PrivateKey.generate()
    source = _ed25519_source(private_key, allowed_kinds=["deploy.finished"])
    runtime_paths = _runtime_paths(tmp_path)
    assert runtime_paths.control_state_root is not None
    replay_store = external_trigger_replay_store(runtime_paths.control_state_root)
    now = 1700001000.0
    path = "/api/event-sources/ci"

    body = json.dumps(
        {
            "kind": "deploy.finished",
            "message": "Production deploy succeeded.",
            "title": "Deploy #9",
            "data": {"env": "prod", "version": 9},
        },
    ).encode("utf-8")
    headers = sign_trigger_request(
        method="POST",
        path=path,
        body=body,
        key_id="ci-key",
        timestamp=str(int(now)),
        nonce="nonce-1",
        private_key=private_key,
    )

    event = verify_and_normalize_ed25519_event(
        source_id="ci",
        source=source,
        path=path,
        headers=headers,
        body=body,
        replay_store=replay_store,
        now=now,
    )
    assert event.source_type == "external_event"
    assert event.source_id == "ci"
    assert event.kind == "deploy.finished"
    assert event.event_id == "nonce-1"
    assert event.title == "Deploy #9"
    assert event.text == "Production deploy succeeded."
    assert event.data == {"env": "prod", "version": 9}

    # Replaying the exact same nonce raises ExternalSourceReplayError
    with pytest.raises(ExternalSourceReplayError, match="nonce"):
        verify_and_normalize_ed25519_event(
            source_id="ci",
            source=source,
            path=path,
            headers=headers,
            body=body,
            replay_store=replay_store,
            now=now,
        )

    # Wrong private key raises ExternalSourceAuthError
    wrong_key = Ed25519PrivateKey.generate()
    bad_headers = sign_trigger_request(
        method="POST",
        path=path,
        body=body,
        key_id="ci-key",
        timestamp=str(int(now)),
        nonce="nonce-2",
        private_key=wrong_key,
    )
    with pytest.raises(ExternalSourceAuthError):
        verify_and_normalize_ed25519_event(
            source_id="ci",
            source=source,
            path=path,
            headers=bad_headers,
            body=body,
            replay_store=replay_store,
            now=now,
        )

    # Disallowed kind raises ExternalSourceBadRequestError
    disallowed_body = json.dumps(
        {"kind": "deploy.started", "message": "Started", "event_id": "evt-99"},
    ).encode("utf-8")
    disallowed_headers = sign_trigger_request(
        method="POST",
        path=path,
        body=disallowed_body,
        key_id="ci-key",
        timestamp=str(int(now)),
        nonce="nonce-3",
        private_key=private_key,
    )
    with pytest.raises(ExternalSourceBadRequestError, match="allowed"):
        verify_and_normalize_ed25519_event(
            source_id="ci",
            source=source,
            path=path,
            headers=disallowed_headers,
            body=disallowed_body,
            replay_store=replay_store,
            now=now,
        )


# ---------------------------------------------------------------------------
# Fan-out matching, authorization, expiry, and deduplication
# ---------------------------------------------------------------------------


def test_fan_out_external_event_enforces_subscribers_expiry_matching_and_dedupe(tmp_path: Path) -> None:
    """Fan-out enqueues only enabled, unexpired, authorized, matching rules and deduplicates deliveries."""
    runtime_paths = _runtime_paths(tmp_path, env={"GITHUB_WEBHOOK_SECRET": _GH_SECRET})
    config = _config()
    source = _github_source(subscribers=[_ALICE])
    store = ReactiveTriggerStore(runtime_paths)

    # 1. Matching rule owned by subscriber Alice -> SHOULD fire
    store.create_trigger(
        _make_external_rule(
            trigger_id="rule_alice_match",
            owner_user_id=_ALICE,
            match={"repo": "mindroom-ai/mindroom", "number": 42},
        ),
    )
    # 2. Matching rule owned by admin (not explicitly in subscribers) -> SHOULD fire
    store.create_trigger(
        _make_external_rule(
            trigger_id="rule_admin_match",
            owner_user_id=_ADMIN,
            match={"repo": "mindroom-ai/mindroom"},
        ),
    )
    # 3. Matching rule owned by unauthorized Bob -> MUST NOT fire (prevents private-repo leak)
    store.create_trigger(
        _make_external_rule(
            trigger_id="rule_bob_unauthorized",
            owner_user_id=_BOB,
            match={"repo": "mindroom-ai/mindroom"},
        ),
    )
    # 4. Disabled rule -> MUST NOT fire
    store.create_trigger(
        _make_external_rule(
            trigger_id="rule_disabled",
            owner_user_id=_ALICE,
            enabled=False,
        ),
    )
    # 5. Expired rule -> MUST NOT fire
    store.create_trigger(
        _make_external_rule(
            trigger_id="rule_expired",
            owner_user_id=_ALICE,
            expires_at=1500.0,
        ),
    )
    # 6. Non-matching PR number -> MUST NOT fire
    store.create_trigger(
        _make_external_rule(
            trigger_id="rule_other_pr",
            owner_user_id=_ALICE,
            match={"repo": "mindroom-ai/mindroom", "number": 99},
        ),
    )
    # 7. Different source_id -> MUST NOT fire
    store.create_trigger(
        _make_external_rule(
            trigger_id="rule_other_source",
            owner_user_id=_ALICE,
            source_id="other_gh",
        ),
    )

    body = json.dumps(
        {
            "action": "closed",
            "repository": {"full_name": "mindroom-ai/mindroom"},
            "pull_request": {"number": 42, "merged": True, "title": "Ship it"},
        },
    ).encode("utf-8")
    event = verify_and_normalize_github_webhook(
        source_id="github",
        source=source,
        headers={
            "X-GitHub-Delivery": "gh-delivery-42",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _github_signature(body),
        },
        body=body,
        runtime_paths=runtime_paths,
        now=2000.0,
    )

    matched = fan_out_external_event(
        source_id="github",
        source=source,
        event=event,
        store=store,
        config=config,
        runtime_paths=runtime_paths,
        now=2000.0,
    )
    assert matched == 2

    due = store.list_due_firings(now=2000.0)
    assert {f.trigger_id for f in due} == {"rule_alice_match", "rule_admin_match"}
    assert all(f.dedupe_key == "gh-delivery-42" for f in due)

    # Duplicate delivery with same event_id does not enqueue again
    matched_again = fan_out_external_event(
        source_id="github",
        source=source,
        event=event,
        store=store,
        config=config,
        runtime_paths=runtime_paths,
        now=2005.0,
    )
    assert matched_again == 0
    assert len(store.list_due_firings(now=2005.0)) == 2


# ---------------------------------------------------------------------------
# FastAPI endpoint POST /api/event-sources/{source_id}
# ---------------------------------------------------------------------------


def _setup_event_source_api(
    tmp_path: Path,
    *,
    event_sources: dict[str, object],
    enabled: bool = True,
    reactive_enabled: bool = True,
    env: dict[str, str] | None = None,
    on_reactive_event: Callable[[], None] | None = None,
) -> tuple[RuntimePaths, Config]:
    config_payload: dict[str, object] = {
        "administrators": [_ADMIN],
        "authorization": {"aliases": {_ALICE: [_ALICE_ALIAS]}},
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
        "router": {"model": "default"},
        "external_trigger_policy": {
            "enabled": enabled,
            "reactive_enabled": reactive_enabled,
            "admin_users": [_TRIGGER_ADMIN],
            "event_sources": event_sources,
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config_payload), encoding="utf-8")
    config = Config.model_validate(config_payload)
    runtime_paths = _runtime_paths(tmp_path, env=env)
    api_main.initialize_api_app(api_main.app, runtime_paths)
    assert config_lifecycle.load_config_into_app(runtime_paths, api_main.app).loaded
    bind_test_embedded_runtime(api_main.app)
    api_main.unbind_external_trigger_runtime(api_main.app)
    if on_reactive_event is not None:
        api_main.bind_external_trigger_runtime(
            api_main.app,
            runtime_config=config,
            target_bot=lambda _name: cast("AgentBot | None", None),
            on_reactive_event=on_reactive_event,
        )
    return runtime_paths, config


def test_event_sources_endpoint_github_fan_out_dedupe_and_wake_callback(tmp_path: Path) -> None:
    """POST /api/event-sources/github verifies HMAC, enqueues matching rules, wakes loop, and dedupes."""
    woke: list[bool] = []
    runtime_paths, _ = _setup_event_source_api(
        tmp_path,
        event_sources={
            "github": {
                "kind": "github",
                "secret_env": "GITHUB_WEBHOOK_SECRET",
                "allowed_repos": ["mindroom-ai/mindroom"],
                "subscribers": [_ALICE],
                "max_body_bytes": 4096,
            },
        },
        env={"GITHUB_WEBHOOK_SECRET": _GH_SECRET},
        on_reactive_event=lambda: woke.append(True),
    )

    store = ReactiveTriggerStore(runtime_paths)
    store.create_trigger(
        _make_external_rule(
            trigger_id="gh_pr_42",
            owner_user_id=_ALICE,
            source_id="github",
            kind="pull_request.merged",
            match={"repo": "mindroom-ai/mindroom", "number": 42},
        ),
    )
    store.create_trigger(
        _make_external_rule(
            trigger_id="gh_pr_99",
            owner_user_id=_ALICE,
            source_id="github",
            kind="pull_request.merged",
            match={"repo": "mindroom-ai/mindroom", "number": 99},
        ),
    )

    body = json.dumps(
        {
            "action": "closed",
            "repository": {"full_name": "mindroom-ai/mindroom"},
            "pull_request": {"number": 42, "merged": True, "title": "Ready"},
        },
    ).encode("utf-8")
    headers = {
        "X-GitHub-Delivery": "delivery-http-42",
        "X-GitHub-Event": "pull_request",
        "X-Hub-Signature-256": _github_signature(body),
    }

    try:
        with TestClient(api_main.app) as client:
            # First delivery -> 202 with matched_rules=1 and wake callback fired
            resp = client.post("/api/event-sources/github", content=body, headers=headers)
            assert resp.status_code == 202
            assert resp.json() == {
                "accepted": True,
                "source_id": "github",
                "event_id": "delivery-http-42",
                "matched_rules": 1,
            }
            assert len(woke) == 1
            assert [f.trigger_id for f in store.list_due_firings()] == ["gh_pr_42"]

            # Duplicate delivery -> 202 with matched_rules=0 and no wake callback
            dup = client.post("/api/event-sources/github", content=body, headers=headers)
            assert dup.status_code == 202
            assert dup.json()["matched_rules"] == 0
            assert len(woke) == 1

            # Wrong signature -> 401
            bad_sig = client.post(
                "/api/event-sources/github",
                content=body,
                headers={**headers, "X-Hub-Signature-256": "sha256=deadbeef"},
            )
            assert bad_sig.status_code == 401

            # Non-ASCII signature -> 401 (never 500 from hmac.compare_digest TypeError)
            non_ascii_sig = client.post(
                "/api/event-sources/github",
                content=body,
                headers=[
                    (b"x-github-delivery", b"delivery-http-42"),
                    (b"x-github-event", b"pull_request"),
                    (b"x-hub-signature-256", b"sha256=\xff"),
                ],
            )
            assert non_ascii_sig.status_code == 401

            # Missing X-GitHub-Delivery -> 400
            no_deliv = client.post(
                "/api/event-sources/github",
                content=body,
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-Hub-Signature-256": _github_signature(body),
                },
            )
            assert no_deliv.status_code == 400

            # Disallowed repo -> 403
            other_repo_body = json.dumps(
                {
                    "action": "closed",
                    "repository": {"full_name": "evil/repo"},
                    "pull_request": {"number": 42, "merged": True},
                },
            ).encode("utf-8")
            forbidden = client.post(
                "/api/event-sources/github",
                content=other_repo_body,
                headers={
                    "X-GitHub-Delivery": "delivery-evil",
                    "X-GitHub-Event": "pull_request",
                    "X-Hub-Signature-256": _github_signature(other_repo_body),
                },
            )
            assert forbidden.status_code == 403

            # Oversized body -> 413
            oversized = b"x" * 5000
            too_large = client.post("/api/event-sources/github", content=oversized, headers=headers)
            assert too_large.status_code == 413

            # Unknown source -> 404
            missing = client.post("/api/event-sources/unknown", content=body, headers=headers)
            assert missing.status_code == 404
    finally:
        api_main.unbind_external_trigger_runtime(api_main.app)


def test_event_sources_endpoint_ed25519_fan_out_and_replay(tmp_path: Path) -> None:
    """POST /api/event-sources/ci verifies Ed25519, enqueues matching rules, and rejects nonce replay with 409."""
    private_key = Ed25519PrivateKey.generate()
    woke: list[bool] = []
    runtime_paths, _ = _setup_event_source_api(
        tmp_path,
        event_sources={
            "ci": {
                "kind": "ed25519",
                "public_key": _public_key_b64(private_key),
                "key_id": "ci-main",
                "allowed_kinds": ["build.failed"],
                "subscribers": [_ALICE],
            },
        },
        on_reactive_event=lambda: woke.append(True),
    )

    store = ReactiveTriggerStore(runtime_paths)
    store.create_trigger(
        _make_external_rule(
            trigger_id="ci_build_failed",
            owner_user_id=_ALICE,
            source_id="ci",
            kind="build.failed",
            match={"branch": "main"},
        ),
    )

    body = json.dumps(
        {
            "kind": "build.failed",
            "message": "Main branch tests failed",
            "event_id": "build-55",
            "data": {"branch": "main"},
        },
    ).encode("utf-8")
    headers = sign_trigger_request(
        method="POST",
        path="/api/event-sources/ci",
        body=body,
        key_id="ci-main",
        timestamp=str(int(time.time())),
        nonce="ci-nonce-55",
        private_key=private_key,
    )

    try:
        with TestClient(api_main.app) as client:
            resp = client.post("/api/event-sources/ci", content=body, headers=headers)
            assert resp.status_code == 202
            assert resp.json() == {
                "accepted": True,
                "source_id": "ci",
                "event_id": "build-55",
                "matched_rules": 1,
            }
            assert len(woke) == 1
            assert [f.trigger_id for f in store.list_due_firings()] == ["ci_build_failed"]

            # Replayed nonce -> 409 Conflict
            replay = client.post("/api/event-sources/ci", content=body, headers=headers)
            assert replay.status_code == 409

            # Disallowed kind -> 422
            bad_kind_body = json.dumps({"kind": "build.passed", "message": "ok"}).encode("utf-8")
            bad_kind_headers = sign_trigger_request(
                method="POST",
                path="/api/event-sources/ci",
                body=bad_kind_body,
                key_id="ci-main",
                timestamp=str(int(time.time())),
                nonce="ci-nonce-56",
                private_key=private_key,
            )
            disallowed = client.post("/api/event-sources/ci", content=bad_kind_body, headers=bad_kind_headers)
            assert disallowed.status_code == 422
    finally:
        api_main.unbind_external_trigger_runtime(api_main.app)


@pytest.mark.parametrize(
    ("enabled", "reactive_enabled"),
    [(False, True), (True, False)],
)
def test_event_sources_endpoint_returns_404_when_policy_disabled(
    tmp_path: Path,
    enabled: bool,
    reactive_enabled: bool,
) -> None:
    """Endpoint returns 404 whenever external_trigger_policy.enabled or reactive_enabled is False."""
    _setup_event_source_api(
        tmp_path,
        event_sources={
            "github": {
                "kind": "github",
                "secret_env": "GITHUB_WEBHOOK_SECRET",
                "allowed_repos": ["mindroom-ai/mindroom"],
                "subscribers": [_ALICE],
            },
        },
        enabled=enabled,
        reactive_enabled=reactive_enabled,
        env={"GITHUB_WEBHOOK_SECRET": _GH_SECRET},
    )
    body = json.dumps({"repository": {"full_name": "mindroom-ai/mindroom"}}).encode("utf-8")
    try:
        with TestClient(api_main.app) as client:
            resp = client.post(
                "/api/event-sources/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "d1",
                    "X-GitHub-Event": "push",
                    "X-Hub-Signature-256": _github_signature(body),
                },
            )
            assert resp.status_code == 404
    finally:
        api_main.unbind_external_trigger_runtime(api_main.app)


def test_event_sources_endpoint_missing_secret_returns_503(tmp_path: Path) -> None:
    """Missing GitHub webhook secret maps to HTTP 503."""
    _setup_event_source_api(
        tmp_path,
        event_sources={
            "github": {
                "kind": "github",
                "secret_env": "GITHUB_WEBHOOK_SECRET",
                "allowed_repos": ["mindroom-ai/mindroom"],
                "subscribers": [_ALICE],
            },
        },
    )
    body = json.dumps({"repository": {"full_name": "mindroom-ai/mindroom"}}).encode("utf-8")
    try:
        with TestClient(api_main.app) as client:
            resp = client.post(
                "/api/event-sources/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "d1",
                    "X-GitHub-Event": "push",
                    "X-Hub-Signature-256": _github_signature(body),
                },
            )
            assert resp.status_code == 503
    finally:
        api_main.unbind_external_trigger_runtime(api_main.app)


def test_bind_and_unbind_external_trigger_runtime_with_reactive_callback(tmp_path: Path) -> None:
    """bind_external_trigger_runtime stores optional on_reactive_event and unbind clears it."""
    _, config = _setup_event_source_api(tmp_path, event_sources={})
    cb = lambda: None  # noqa: E731

    try:
        api_main.bind_external_trigger_runtime(
            api_main.app,
            runtime_config=config,
            target_bot=lambda _name: cast("AgentBot | None", None),
        )
        bound = config_lifecycle.app_state(api_main.app).external_trigger_runtime
        assert bound is not None
        assert bound.on_reactive_event is None

        api_main.bind_external_trigger_runtime(
            api_main.app,
            runtime_config=config,
            target_bot=lambda _name: cast("AgentBot | None", None),
            on_reactive_event=cb,
        )
        bound_with_cb = config_lifecycle.app_state(api_main.app).external_trigger_runtime
        assert bound_with_cb is not None
        assert bound_with_cb.on_reactive_event is cb

        api_main.unbind_external_trigger_runtime(api_main.app)
        assert config_lifecycle.app_state(api_main.app).external_trigger_runtime is None
    finally:
        api_main.unbind_external_trigger_runtime(api_main.app)


def test_event_sources_endpoint_sqlite_error_returns_503(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite OperationalError during event-source fan-out maps to HTTP 503 instead of 500."""
    _setup_event_source_api(
        tmp_path,
        event_sources={
            "github": {
                "kind": "github",
                "secret_env": "GITHUB_WEBHOOK_SECRET",
                "allowed_repos": ["mindroom-ai/mindroom"],
                "subscribers": [_ALICE],
            },
        },
        env={"GITHUB_WEBHOOK_SECRET": _GH_SECRET},
    )

    def _raise_sqlite_error(*_args: object, **_kwargs: object) -> int:
        msg = "database is locked"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr("mindroom.api.external_triggers.fan_out_external_event", _raise_sqlite_error)

    body = json.dumps({"repository": {"full_name": "mindroom-ai/mindroom"}}).encode("utf-8")
    try:
        with TestClient(api_main.app) as client:
            resp = client.post(
                "/api/event-sources/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "d-sqlite-err",
                    "X-GitHub-Event": "push",
                    "X-Hub-Signature-256": _github_signature(body),
                },
            )
            assert resp.status_code == 503
    finally:
        api_main.unbind_external_trigger_runtime(api_main.app)


def test_github_webhook_canonicalizes_repo_casing_for_rule_matching(tmp_path: Path) -> None:
    """GitHub repo casing normalizes to the configured allowed_repos spelling so validated rules match."""
    source = _github_source(allowed_repos=["mindroom-ai/mindroom"], subscribers=[_ALICE])
    config = _config()
    runtime_paths = _runtime_paths(tmp_path, env={"GITHUB_WEBHOOK_SECRET": _GH_SECRET})
    store = ReactiveTriggerStore(runtime_paths)

    validate_subscription_against_source(
        source,
        kind="pull_request.merged",
        match={"repo": "mindroom-ai/mindroom"},
    )
    rule = _make_external_rule(
        trigger_id="gh-pr-canonical-repo",
        kind="pull_request.merged",
        match={"repo": "mindroom-ai/mindroom"},
    )
    store.create_trigger(rule)

    payload = {
        "action": "closed",
        "repository": {"full_name": "MindRoom-AI/mindroom"},
        "sender": {"login": "basnijholt"},
        "pull_request": {
            "number": 4198,
            "merged": True,
            "title": "Add reactive triggers",
            "body": "Details",
            "head": {"ref": "feat/reactive-triggers", "sha": "abc123"},
            "base": {"ref": "main"},
            "user": {"login": "basnijholt"},
            "merge_commit_sha": "merge999",
            "html_url": "https://github.com/MindRoom-AI/mindroom/pull/4198",
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    event = verify_and_normalize_github_webhook(
        source_id="github",
        source=source,
        headers={
            "X-GitHub-Delivery": "delivery-canonical-repo",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _github_signature(raw_body),
        },
        body=raw_body,
        runtime_paths=runtime_paths,
        now=1700000000.0,
    )

    assert event.data["repo"] == "mindroom-ai/mindroom"

    matched = fan_out_external_event(
        source_id="github",
        source=source,
        event=event,
        store=store,
        config=config,
        runtime_paths=runtime_paths,
        now=1700000000.0,
    )
    assert matched == 1
    pending = store.list_due_firings(now=1700000000.0)
    assert [row.trigger_id for row in pending] == ["gh-pr-canonical-repo"]
    assert pending[0].event.data["repo"] == "mindroom-ai/mindroom"


def test_ed25519_event_source_replay_store_isolated_from_direct_trigger_writes(tmp_path: Path) -> None:
    """Direct trigger store writes must NOT prune shared Ed25519 event source replay nonces."""
    private_key = Ed25519PrivateKey.generate()
    runtime_paths, _config = _setup_event_source_api(
        tmp_path,
        event_sources={
            "ci": {
                "kind": "ed25519",
                "public_key": _public_key_b64(private_key),
                "key_id": "ci-main",
                "allowed_kinds": ["build.failed"],
                "subscribers": [_ALICE],
            },
        },
    )
    direct_config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
            "agents": {"research": {"display_name": "Research", "model": "default", "rooms": [_ROOM]}},
            "rooms": {"main": {"display_name": "Main"}},
            "external_trigger_policy": {"enabled": True, "reactive_enabled": True},
        },
    )

    body = json.dumps({"kind": "build.failed", "message": "CI failed", "event_id": "evt-isolated-1"}).encode("utf-8")
    headers = sign_trigger_request(
        method="POST",
        path="/api/event-sources/ci",
        body=body,
        key_id="ci-main",
        timestamp=str(int(time.time())),
        nonce="nonce-isolated-replay-1",
        private_key=private_key,
    )

    with TestClient(api_main.app) as client:
        first = client.post("/api/event-sources/ci", content=body, headers=headers)
        assert first.status_code == 202

        # Creating a direct trigger calls ExternalTriggerStore._write_records -> retain_scopes on direct replay store
        direct_key = Ed25519PrivateKey.generate()
        direct_store = ExternalTriggerStore(runtime_paths)
        direct_store.create_record(
            trigger_id="direct_other",
            owner_user_id=_ALICE,
            created_by_agent_name="research",
            created_in_room_id=_ROOM,
            created_in_thread_id=None,
            target=ExternalTriggerTarget(room_id=_ROOM, agent="research"),
            public_key=_public_key_b64(direct_key),
            config=direct_config,
        )

        # Replaying the exact same signed Ed25519 event MUST still return 409
        replayed = client.post("/api/event-sources/ci", content=body, headers=headers)
        assert replayed.status_code == 409


def test_github_webhook_ping_returns_202_after_signature_and_repo_checks(tmp_path: Path) -> None:
    """X-GitHub-Event: ping passes cleanly with 202 and matched_rules=0 after HMAC + allowed_repos verification."""
    secret = _GH_SECRET
    _setup_event_source_api(
        tmp_path,
        event_sources={
            "gh": {
                "kind": "github",
                "secret_env": "GH_PING_SECRET",
                "allowed_repos": ["mindroom-ai/mindroom"],
                "allowed_events": ["pull_request"],
                "subscribers": [_ALICE],
            },
        },
        env={"GH_PING_SECRET": secret},
    )

    payload = json.dumps({"zen": "Design for failure.", "repository": {"full_name": "mindroom-ai/mindroom"}}).encode()
    headers = {
        "X-GitHub-Event": "ping",
        "X-GitHub-Delivery": "deliv-ping-1",
        "X-Hub-Signature-256": _github_signature(payload, secret),
        "Content-Type": "application/json",
    }
    with TestClient(api_main.app) as client:
        resp = client.post("/api/event-sources/gh", content=payload, headers=headers)
        assert resp.status_code == 202
        assert resp.json() == {
            "accepted": True,
            "source_id": "gh",
            "event_id": "deliv-ping-1",
            "matched_rules": 0,
        }

        # Ping with bad signature still returns 401
        bad_sig_resp = client.post(
            "/api/event-sources/gh",
            content=payload,
            headers={**headers, "X-Hub-Signature-256": "sha256=deadbeef"},
        )
        assert bad_sig_resp.status_code == 401

        # Ping with disallowed repo still returns 403
        bad_repo_payload = json.dumps({"zen": "nope", "repository": {"full_name": "evil/repo"}}).encode()
        bad_repo_resp = client.post(
            "/api/event-sources/gh",
            content=bad_repo_payload,
            headers={
                "X-GitHub-Event": "ping",
                "X-GitHub-Delivery": "deliv-ping-2",
                "X-Hub-Signature-256": _github_signature(bad_repo_payload, secret),
                "Content-Type": "application/json",
            },
        )
        assert bad_repo_resp.status_code == 403


@pytest.mark.parametrize(
    ("gh_event", "missing_key", "bad_value"),
    [
        ("pull_request", "pull_request", None),
        ("pull_request", "pull_request", "not-a-dict"),
        ("pull_request_review", "review", None),
        ("issues", "issue", ["list"]),
        ("release", "release", None),
        ("check_suite", "check_suite", 123),
    ],
)
def test_github_typed_events_reject_missing_or_non_dict_object_key(
    tmp_path: Path,
    gh_event: str,
    missing_key: str,
    bad_value: object,
) -> None:
    """Typed GitHub webhook events reject mismatched/spoofed payloads missing their expected dict key."""
    secret = _GH_SECRET
    _setup_event_source_api(
        tmp_path,
        event_sources={
            "gh": {
                "kind": "github",
                "secret_env": "GH_TYPED_SECRET",
                "allowed_repos": ["mindroom-ai/mindroom"],
                "subscribers": [_ALICE],
            },
        },
        env={"GH_TYPED_SECRET": secret},
    )
    body_obj: dict[str, object] = {"repository": {"full_name": "mindroom-ai/mindroom"}, "action": "opened"}
    if bad_value is not None:
        body_obj[missing_key] = bad_value
    payload = json.dumps(body_obj).encode("utf-8")
    with TestClient(api_main.app) as client:
        resp = client.post(
            "/api/event-sources/gh",
            content=payload,
            headers={
                "X-GitHub-Event": gh_event,
                "X-GitHub-Delivery": f"deliv-bad-{gh_event}",
                "X-Hub-Signature-256": _github_signature(payload, secret),
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 400


def test_github_check_suite_preserves_id_and_url_for_subscription_matching(tmp_path: Path) -> None:
    """check_suite.completed preserves check_suite.id and check_suite.url so match={'id': ...} fans out cleanly."""
    secret = _GH_SECRET
    runtime_paths, config = _setup_event_source_api(
        tmp_path,
        event_sources={
            "gh": {
                "kind": "github",
                "secret_env": "GH_CS_SECRET",
                "allowed_repos": ["mindroom-ai/mindroom"],
                "subscribers": [_ALICE],
            },
        },
        env={"GH_CS_SECRET": secret},
    )
    store = ReactiveTriggerStore(runtime_paths)
    store.create_trigger(
        _make_external_rule(
            trigger_id="cs-match-id",
            owner_user_id=_ALICE,
            source_id="gh",
            kind="check_suite.completed",
            match={"repo": "mindroom-ai/mindroom", "id": 987654, "conclusion": "failure"},
        ),
    )

    body_obj = {
        "action": "completed",
        "repository": {"full_name": "mindroom-ai/mindroom"},
        "sender": {"login": "github-actions[bot]"},
        "check_suite": {
            "id": 987654,
            "status": "completed",
            "conclusion": "failure",
            "head_branch": "main",
            "head_sha": "abc1234",
            "url": "https://api.github.com/repos/mindroom-ai/mindroom/check-suites/987654",
            "head_commit": {"message": "ci run"},
        },
    }
    payload = json.dumps(body_obj).encode("utf-8")
    with TestClient(api_main.app) as client:
        resp = client.post(
            "/api/event-sources/gh",
            content=payload,
            headers={
                "X-GitHub-Event": "check_suite",
                "X-GitHub-Delivery": "deliv-cs-987654",
                "X-Hub-Signature-256": _github_signature(payload, secret),
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 202
    assert resp.json()["matched_rules"] == 1
    due = store.list_due_firings()
    assert len(due) == 1
    assert due[0].event.data["id"] == 987654
    assert due[0].event.data["url"] == "https://api.github.com/repos/mindroom-ai/mindroom/check-suites/987654"
    assert config.external_trigger_policy.enabled is True


def _hmac_sig(body: bytes, secret: str, *, prefix: str = "sha256=") -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{prefix}{digest}"


def test_verify_and_normalize_hmac_webhook_variants_and_errors(tmp_path: Path) -> None:
    """verify_and_normalize_hmac_webhook enforces 503/401/400 errors and normalizes header, dot-path, and fallback fields."""
    secret = "hmac-super-secret"  # noqa: S105
    paths_no_secret = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={},
    )
    paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={"CUSTOM_HMAC_SECRET": secret},
    )
    src = HmacEventSourceConfig(
        secret_env="CUSTOM_HMAC_SECRET",  # noqa: S106
        signature_header="X-Custom-Sig",
        signature_prefix="sha256=",
        kind_field="meta.event_type",
        allowed_kinds=["deploy.done", "alert.fired"],
        subscribers=[_ALICE],
    )
    body = json.dumps(
        {
            "meta": {"event_type": "deploy.done"},
            "id": 42,
            "actor": "ci-bot",
            "title": "T" * 600,
            "summary": "S" * 2500,
            "text": "Deploy finished",
            "env": "prod",
        },
    ).encode("utf-8")

    # 503 when secret env var is missing
    with pytest.raises(ExternalSourceUnavailableError):
        verify_and_normalize_hmac_webhook(
            source_id="ops",
            source=src,
            headers={"X-Custom-Sig": _hmac_sig(body, secret)},
            body=body,
            runtime_paths=paths_no_secret,
        )

    # 401 on missing / wrong / wrong-prefix signature
    for bad_headers in (
        {},
        {"X-Custom-Sig": "sha256=deadbeef"},
        {"X-Custom-Sig": _hmac_sig(body, secret, prefix="")},
    ):
        with pytest.raises(ExternalSourceAuthError):
            verify_and_normalize_hmac_webhook(
                source_id="ops",
                source=src,
                headers=bad_headers,
                body=body,
                runtime_paths=paths,
            )

    # Uppercase hex is normalized and accepted; dot-path kind, int id, bounded fields, and full data preserved
    event = verify_and_normalize_hmac_webhook(
        source_id="ops",
        source=src,
        headers={"x-custom-sig": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest().upper()},
        body=body,
        runtime_paths=paths,
        now=1234.0,
    )
    assert event.source_type == "external_event"
    assert event.source_id == "ops"
    assert event.kind == "deploy.done"
    assert event.event_id == "42"
    assert event.actor == "ci-bot"
    assert event.title is not None
    assert len(event.title) == 500
    assert event.summary is not None
    assert len(event.summary) == 2000
    assert event.text == "Deploy finished"
    assert event.data["env"] == "prod"

    # 400 on bad JSON, non-dict JSON, missing kind, disallowed kind, oversized event_id
    for bad_body in (b"not-json", b"[1,2]", b"{}", json.dumps({"meta": {"event_type": "disallowed"}}).encode()):
        with pytest.raises(ExternalSourceBadRequestError):
            verify_and_normalize_hmac_webhook(
                source_id="ops",
                source=src,
                headers={"X-Custom-Sig": _hmac_sig(bad_body, secret)},
                body=bad_body,
                runtime_paths=paths,
            )

    huge_id_body = json.dumps({"meta": {"event_type": "deploy.done"}, "event_id": "x" * 300}).encode()
    with pytest.raises(ExternalSourceBadRequestError):
        verify_and_normalize_hmac_webhook(
            source_id="ops",
            source=src,
            headers={"X-Custom-Sig": _hmac_sig(huge_id_body, secret)},
            body=huge_id_body,
            runtime_paths=paths,
        )

    # Bare hex signature + kind_header + event_id_header fallback to sha256 body hash (and ignores bool id)
    bare_src = HmacEventSourceConfig(
        secret_env="CUSTOM_HMAC_SECRET",  # noqa: S106
        signature_header="Linear-Signature",
        signature_prefix="",
        kind_header="Linear-Event",
        event_id_header="Linear-Delivery",
    )
    no_id_body = json.dumps({"id": True, "team": "eng"}).encode()
    ev_fallback = verify_and_normalize_hmac_webhook(
        source_id="linear",
        source=bare_src,
        headers={
            "Linear-Signature": _hmac_sig(no_id_body, secret, prefix=""),
            "Linear-Event": "Issue",
        },
        body=no_id_body,
        runtime_paths=paths,
    )
    assert ev_fallback.kind == "Issue"
    assert ev_fallback.event_id == f"sha256:{hashlib.sha256(no_id_body).hexdigest()[:32]}"

    ev_header_id = verify_and_normalize_hmac_webhook(
        source_id="linear",
        source=bare_src,
        headers={
            "Linear-Signature": _hmac_sig(no_id_body, secret, prefix=""),
            "Linear-Event": "Issue",
            "Linear-Delivery": "lin-deliv-99",
        },
        body=no_id_body,
        runtime_paths=paths,
    )
    assert ev_header_id.event_id == "lin-deliv-99"

    with pytest.raises(ExternalSourceBadRequestError):
        verify_and_normalize_hmac_webhook(
            source_id="linear",
            source=bare_src,
            headers={"Linear-Signature": _hmac_sig(no_id_body, secret, prefix="")},
            body=no_id_body,
            runtime_paths=paths,
        )


def test_hmac_event_source_api_fanout_dedupe_and_access_recheck(tmp_path: Path) -> None:
    """POST /api/event-sources/{source_id} verifies HMAC webhooks, fans out, deduplicates, and rechecks allowed_kinds."""
    secret = "api-hmac-secret"  # noqa: S105
    runtime_paths, config = _setup_event_source_api(
        tmp_path,
        event_sources={
            "alerts": {
                "kind": "hmac",
                "secret_env": "ALERT_HMAC_SECRET",
                "signature_header": "X-Alert-Sig",
                "signature_prefix": "sha256=",
                "kind_field": "alert.type",
                "allowed_kinds": ["page.triggered"],
                "subscribers": [_ALICE],
            },
        },
        env={"ALERT_HMAC_SECRET": secret},
    )
    source_cfg = config.external_trigger_policy.event_sources["alerts"]
    validate_subscription_against_source(source_cfg, kind="page.triggered", match={"service": "api"})
    with pytest.raises(ValueError, match="not allowed"):
        validate_subscription_against_source(source_cfg, kind="page.ignored", match={})

    store = ReactiveTriggerStore(runtime_paths)
    store.create_trigger(
        _make_external_rule(
            trigger_id="hmac-sub",
            owner_user_id=_ALICE,
            source_id="alerts",
            kind="page.triggered",
            match={"service": "api"},
        ),
    )

    payload = json.dumps({"alert": {"type": "page.triggered"}, "service": "api", "text": "5xx spike"}).encode()
    headers = {"X-Alert-Sig": _hmac_sig(payload, secret), "Content-Type": "application/json"}
    with TestClient(api_main.app) as client:
        bad_sig = client.post("/api/event-sources/alerts", content=payload, headers={"X-Alert-Sig": "sha256=bad"})
        assert bad_sig.status_code == 401

        ok = client.post("/api/event-sources/alerts", content=payload, headers=headers)
        assert ok.status_code == 202
        assert ok.json()["matched_rules"] == 1

        # Same body hashes to same sha256 dedupe_key -> deduplicated
        dup = client.post("/api/event-sources/alerts", content=payload, headers=headers)
        assert dup.status_code == 202
        assert dup.json()["matched_rules"] == 0

    due = store.list_due_firings()
    assert len(due) == 1
    spec = ExternalEventSourceSpec(source_id="alerts", kind="page.triggered", match={"service": "api"})
    assert (
        check_external_event_source_access(
            owner_user_id=_ALICE,
            source_spec=spec,
            event=due[0].event,
            config=config,
            runtime_paths=runtime_paths,
        )
        is None
    )
    # Revoking allowed_kinds fails closed at dispatch access check
    revoked_config = config.model_copy(deep=True)
    revoked_src = revoked_config.external_trigger_policy.event_sources["alerts"]
    assert isinstance(revoked_src, HmacEventSourceConfig)
    revoked_src.allowed_kinds = ["other.kind"]
    assert (
        check_external_event_source_access(
            owner_user_id=_ALICE,
            source_spec=spec,
            event=due[0].event,
            config=revoked_config,
            runtime_paths=runtime_paths,
        )
        == "source_grant_revoked"
    )
