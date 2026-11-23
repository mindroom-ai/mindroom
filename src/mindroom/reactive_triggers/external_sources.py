"""Shared external event source verification, normalization, and fan-out."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from mindroom.config.external_trigger_policy import (
    Ed25519EventSourceConfig,
    ExternalEventSourceConfig,
    GitHubEventSourceConfig,
)
from mindroom.credentials.environment import get_secret_from_env
from mindroom.delivery_replay import DeliveryReplayStore, DeliveryReplayStoreError
from mindroom.external_triggers.auth import (
    TriggerAuthError,
    TriggerSignatureHeaders,
    verify_trigger_request,
)
from mindroom.external_triggers.models import ExternalTriggerPayload
from mindroom.external_triggers.policy import is_external_trigger_administrator
from mindroom.reactive_triggers.matching import matches_external_event
from mindroom.reactive_triggers.models import ExternalEventSourceSpec, TriggerEvent
from mindroom.requester_identity import equivalent_requester_ids

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.reactive_triggers.store import ReactiveTriggerStore

_MAX_GITHUB_DELIVERY_BYTES = 256
_MAX_TITLE_CHARS = 500
_MAX_SUMMARY_CHARS = 2000


class ExternalSourceAuthError(RuntimeError):
    """Raised when an external event source request fails authentication (HTTP 401)."""


class ExternalSourceBadRequestError(RuntimeError):
    """Raised when an external event source request is malformed or disallowed (HTTP 400 / 422)."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class ExternalSourceForbiddenError(RuntimeError):
    """Raised when an authenticated external event violates a source access boundary (HTTP 403)."""


class ExternalSourceReplayError(RuntimeError):
    """Raised when an external event source signature nonce is replayed (HTTP 409)."""


class ExternalSourceUnavailableError(RuntimeError):
    """Raised when an external event source dependency or secret is unavailable (HTTP 503)."""


def event_source_replay_store(runtime_paths: RuntimePaths) -> DeliveryReplayStore:
    """Return the dedicated replay store for shared Ed25519 event sources."""
    control_root = runtime_paths.control_state_root
    if control_root is None:
        msg = "Event source replay store requires primary control state"
        raise ExternalSourceUnavailableError(msg)
    return DeliveryReplayStore(root=control_root / "event_source_replay")


def is_source_subscriber_allowed(
    source: ExternalEventSourceConfig,
    owner_user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether owner_user_id may subscribe to or receive events from source."""
    if is_external_trigger_administrator(config, runtime_paths, owner_user_id):
        return True
    owner_ids = {owner_user_id, *equivalent_requester_ids(owner_user_id, config, runtime_paths)}
    for subscriber in source.subscribers:
        subscriber_ids = {subscriber, *equivalent_requester_ids(subscriber, config, runtime_paths)}
        if owner_ids & subscriber_ids:
            return True
    return False


def validate_subscription_against_source(
    source: ExternalEventSourceConfig,
    *,
    kind: str,
    match: Mapping[str, str | bool | int],
) -> None:
    """Validate that a rule's event kind and structured match filters fit source allowlists."""
    stripped_kind = kind.strip()
    if not stripped_kind:
        msg = "Event kind must not be empty"
        raise ValueError(msg)

    if isinstance(source, GitHubEventSourceConfig):
        base_event = stripped_kind.split(".", 1)[0]
        if base_event not in source.allowed_events:
            msg = f"GitHub event {base_event!r} is not allowed for this event source"
            raise ValueError(msg)
        if "repo" in match:
            repo_value = match["repo"]
            if not isinstance(repo_value, str):
                msg = "GitHub repo match value must be a string"
                raise ValueError(msg)
            if repo_value not in source.allowed_repos:
                msg = f"Repository {repo_value!r} is not allowed for this event source"
                raise ValueError(msg)
        return

    if (
        isinstance(source, Ed25519EventSourceConfig)
        and source.allowed_kinds
        and stripped_kind not in source.allowed_kinds
    ):
        msg = f"Event kind {stripped_kind!r} is not allowed for this event source"
        raise ValueError(msg)


def check_external_event_source_access(
    *,
    owner_user_id: str,
    source_spec: ExternalEventSourceSpec,
    event: TriggerEvent,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Recheck shared external event source existence, subscriber allowlist, and event/repo grants."""
    source = config.external_trigger_policy.event_sources.get(source_spec.source_id)
    if source is None:
        return "source_removed"
    if not is_source_subscriber_allowed(source, owner_user_id, config, runtime_paths):
        return "source_subscriber_revoked"
    try:
        validate_subscription_against_source(source, kind=source_spec.kind, match=source_spec.match)
    except ValueError:
        return "source_grant_revoked"
    if isinstance(source, GitHubEventSourceConfig):
        repo = event.data.get("repo")
        base_event = event.kind.split(".", 1)[0]
        if (
            not isinstance(repo, str)
            or not any(allowed.lower() == repo.lower() for allowed in source.allowed_repos)
            or base_event not in source.allowed_events
        ):
            return "source_grant_revoked"
    elif (
        isinstance(source, Ed25519EventSourceConfig) and source.allowed_kinds and event.kind not in source.allowed_kinds
    ):
        return "source_grant_revoked"
    return None


def verify_and_normalize_github_webhook(
    *,
    source_id: str,
    source: GitHubEventSourceConfig,
    headers: Mapping[str, str],
    body: bytes,
    runtime_paths: RuntimePaths,
    now: float | None = None,
) -> TriggerEvent:
    """Verify a GitHub HMAC-SHA256 webhook and normalize it into a TriggerEvent."""
    normalized_headers = {name.lower(): value for name, value in headers.items()}

    secret = get_secret_from_env(source.secret_env, runtime_paths=runtime_paths)
    if secret is None or not secret.strip():
        msg = "GitHub webhook secret is not configured"
        raise ExternalSourceUnavailableError(msg)

    delivery_id = normalized_headers.get("x-github-delivery", "").strip()
    if not delivery_id:
        msg = "Missing X-GitHub-Delivery header"
        raise ExternalSourceBadRequestError(msg)
    if len(delivery_id.encode("utf-8")) > _MAX_GITHUB_DELIVERY_BYTES:
        msg = "X-GitHub-Delivery header exceeds maximum length"
        raise ExternalSourceBadRequestError(msg)

    gh_event = normalized_headers.get("x-github-event", "").strip()
    if not gh_event:
        msg = "Missing X-GitHub-Event header"
        raise ExternalSourceBadRequestError(msg)

    _verify_github_signature(normalized_headers, body=body, secret=secret)

    payload = _parse_json_object(body)
    repo = _extract_allowed_github_repo(payload, source)
    current_time = time.time() if now is None else now
    actor = _optional_str(_mapping(payload.get("sender")).get("login"))

    if gh_event == "ping":
        return TriggerEvent(
            source_type="external_event",
            source_id=source_id,
            kind="ping",
            event_id=delivery_id,
            timestamp=current_time,
            actor=actor,
            data={"repo": repo},
        )

    if gh_event not in source.allowed_events:
        msg = "GitHub event type is not allowed for this event source"
        raise ExternalSourceBadRequestError(msg)

    kind, data, title, summary = _normalize_github_payload(gh_event, repo, payload)

    return TriggerEvent(
        source_type="external_event",
        source_id=source_id,
        kind=kind,
        event_id=delivery_id,
        timestamp=current_time,
        actor=actor,
        title=title,
        summary=summary,
        data=data,
    )


def verify_and_normalize_ed25519_event(
    *,
    source_id: str,
    source: Ed25519EventSourceConfig,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    replay_store: DeliveryReplayStore,
    now: float | None = None,
    method: str = "POST",
) -> TriggerEvent:
    """Verify an Ed25519-signed event source request, enforce nonce replay, and normalize it."""
    current_time = time.time() if now is None else now

    try:
        signature_headers = TriggerSignatureHeaders.from_mapping(headers)
        verify_trigger_request(
            method=method,
            path=path,
            body=body,
            headers=signature_headers,
            expected_key_id=source.key_id,
            public_key_b64=source.public_key,
            replay_window_seconds=source.replay_window_seconds,
            now=int(current_time),
        )
    except TriggerAuthError as exc:
        msg = "Invalid event source signature"
        raise ExternalSourceAuthError(msg) from exc

    try:
        claimed = replay_store.claim_nonce(
            f"event_source:{source_id}",
            signature_headers.nonce,
            now=int(current_time),
            ttl_seconds=source.replay_window_seconds,
        )
    except DeliveryReplayStoreError as exc:
        msg = "Event source replay store is not available"
        raise ExternalSourceUnavailableError(msg) from exc

    if not claimed:
        msg = "Event source nonce has already been used"
        raise ExternalSourceReplayError(msg)

    try:
        payload = ExternalTriggerPayload.model_validate_json(body)
    except ValidationError as exc:
        msg = "Invalid event source payload"
        raise ExternalSourceBadRequestError(msg, status_code=422) from exc

    if source.allowed_kinds and payload.kind not in source.allowed_kinds:
        msg = "Event kind is not allowed for this event source"
        raise ExternalSourceBadRequestError(msg, status_code=422)

    return TriggerEvent(
        source_type="external_event",
        source_id=source_id,
        kind=payload.kind,
        event_id=payload.event_id or signature_headers.nonce,
        timestamp=current_time,
        title=payload.title,
        text=payload.message,
        data=dict(payload.data),
    )


def fan_out_external_event(
    *,
    source_id: str,
    source: ExternalEventSourceConfig,
    event: TriggerEvent,
    store: ReactiveTriggerStore,
    config: Config,
    runtime_paths: RuntimePaths,
    now: float | None = None,
) -> int:
    """Enqueue deduplicated firings for all matching, authorized, unexpired rules subscribed to source_id."""
    if isinstance(source, GitHubEventSourceConfig) and event.kind == "ping" and "ping" not in source.allowed_events:
        return 0
    current_time = time.time() if now is None else now
    rules = store.list_triggers(
        include_disabled=False,
        source_type="external_event",
        source_id=source_id,
    )

    matched_count = 0
    for rule in rules:
        if rule.expires_at is not None and rule.expires_at <= current_time:
            continue
        if not isinstance(rule.source, ExternalEventSourceSpec):
            continue
        if not is_source_subscriber_allowed(source, rule.owner_user_id, config, runtime_paths):
            continue
        if not matches_external_event(rule.source, event):
            continue
        if store.enqueue_firing(rule.trigger_id, event.event_id, event, expected_rule_uid=rule.rule_uid):
            matched_count += 1
    return matched_count


def _verify_github_signature(headers: Mapping[str, str], *, body: bytes, secret: str) -> None:
    signature_header = headers.get("x-hub-signature-256", "").strip()
    if not signature_header.startswith("sha256="):
        msg = "Invalid GitHub webhook signature"
        raise ExternalSourceAuthError(msg)
    provided = signature_header.removeprefix("sha256=")
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode("ascii"), provided.encode("utf-8", "surrogateescape")):
        msg = "Invalid GitHub webhook signature"
        raise ExternalSourceAuthError(msg)


def _parse_json_object(body: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = "GitHub webhook body must be valid JSON"
        raise ExternalSourceBadRequestError(msg) from exc
    if not isinstance(parsed, dict):
        msg = "GitHub webhook body must be a JSON object"
        raise ExternalSourceBadRequestError(msg)
    return parsed


def _extract_allowed_github_repo(payload: Mapping[str, Any], source: GitHubEventSourceConfig) -> str:
    repository = _mapping(payload.get("repository"))
    repo = _optional_str(repository.get("full_name"))
    if repo is None:
        msg = "Missing repository.full_name in GitHub webhook payload"
        raise ExternalSourceBadRequestError(msg)
    repo_lower = repo.lower()
    canonical_repo = next(
        (allowed for allowed in source.allowed_repos if allowed.lower() == repo_lower),
        None,
    )
    if canonical_repo is None:
        msg = "Repository is not allowed for this event source"
        raise ExternalSourceForbiddenError(msg)
    return canonical_repo


_REQUIRED_GITHUB_OBJECT_KEYS: dict[str, str] = {
    "pull_request": "pull_request",
    "pull_request_review": "review",
    "issues": "issue",
    "release": "release",
    "check_suite": "check_suite",
}


def _normalize_github_payload(
    gh_event: str,
    repo: str,
    payload: Mapping[str, Any],
) -> tuple[str, dict[str, str | int | bool], str | None, str | None]:
    required_key = _REQUIRED_GITHUB_OBJECT_KEYS.get(gh_event)
    if required_key is not None and not isinstance(payload.get(required_key), dict):
        msg = f"Missing or invalid {required_key!r} object for GitHub {gh_event!r} webhook"
        raise ExternalSourceBadRequestError(msg)
    action = _optional_str(payload.get("action"))
    normalizer = _GITHUB_EVENT_NORMALIZERS.get(gh_event)
    if normalizer is not None:
        return normalizer(repo, action, payload)
    kind = f"{gh_event}.{action}" if action else gh_event
    data = _clean_data({"repo": repo, "action": action})
    return kind, data, None, None


def _normalize_pull_request(
    repo: str,
    action: str | None,
    payload: Mapping[str, Any],
) -> tuple[str, dict[str, str | int | bool], str | None, str | None]:
    pr = _mapping(payload.get("pull_request"))
    merged = pr.get("merged") is True
    if action == "closed" and merged:
        kind = "pull_request.merged"
    elif action:
        kind = f"pull_request.{action}"
    else:
        kind = "pull_request"
    head = _mapping(pr.get("head"))
    base = _mapping(pr.get("base"))
    user = _mapping(pr.get("user"))
    data = _clean_data(
        {
            "repo": repo,
            "number": pr.get("number"),
            "action": action,
            "merged": merged,
            "branch": head.get("ref"),
            "base_branch": base.get("ref"),
            "author": user.get("login"),
            "sha": pr.get("merge_commit_sha") or head.get("sha"),
            "url": pr.get("html_url"),
        },
    )
    title = _clip_text(pr.get("title"), _MAX_TITLE_CHARS)
    summary = _clip_text(pr.get("body"), _MAX_SUMMARY_CHARS)
    return kind, data, title, summary


def _normalize_pull_request_review(
    repo: str,
    action: str | None,
    payload: Mapping[str, Any],
) -> tuple[str, dict[str, str | int | bool], str | None, str | None]:
    pr = _mapping(payload.get("pull_request"))
    review = _mapping(payload.get("review"))
    kind = f"pull_request_review.{action}" if action else "pull_request_review"
    data = _clean_data(
        {
            "repo": repo,
            "number": pr.get("number"),
            "action": action,
            "state": review.get("state"),
            "author": _mapping(review.get("user")).get("login"),
            "url": review.get("html_url"),
        },
    )
    title = _clip_text(pr.get("title"), _MAX_TITLE_CHARS)
    summary = _clip_text(review.get("body"), _MAX_SUMMARY_CHARS)
    return kind, data, title, summary


def _normalize_issues(
    repo: str,
    action: str | None,
    payload: Mapping[str, Any],
) -> tuple[str, dict[str, str | int | bool], str | None, str | None]:
    issue = _mapping(payload.get("issue"))
    kind = f"issues.{action}" if action else "issues"
    data = _clean_data(
        {
            "repo": repo,
            "number": issue.get("number"),
            "action": action,
            "author": _mapping(issue.get("user")).get("login"),
            "url": issue.get("html_url"),
        },
    )
    title = _clip_text(issue.get("title"), _MAX_TITLE_CHARS)
    summary = _clip_text(issue.get("body"), _MAX_SUMMARY_CHARS)
    return kind, data, title, summary


def _normalize_release(
    repo: str,
    action: str | None,
    payload: Mapping[str, Any],
) -> tuple[str, dict[str, str | int | bool], str | None, str | None]:
    rel = _mapping(payload.get("release"))
    kind = f"release.{action}" if action else "release"
    data = _clean_data(
        {
            "repo": repo,
            "tag": rel.get("tag_name"),
            "action": action,
            "prerelease": rel.get("prerelease") is True,
            "author": _mapping(rel.get("author")).get("login"),
            "url": rel.get("html_url"),
        },
    )
    title = _clip_text(rel.get("name") or rel.get("tag_name"), _MAX_TITLE_CHARS)
    summary = _clip_text(rel.get("body"), _MAX_SUMMARY_CHARS)
    return kind, data, title, summary


def _normalize_check_suite(
    repo: str,
    action: str | None,
    payload: Mapping[str, Any],
) -> tuple[str, dict[str, str | int | bool], str | None, str | None]:
    cs = _mapping(payload.get("check_suite"))
    kind = f"check_suite.{action}" if action else "check_suite"
    data = _clean_data(
        {
            "repo": repo,
            "id": cs.get("id"),
            "action": action,
            "status": cs.get("status"),
            "conclusion": cs.get("conclusion"),
            "branch": cs.get("head_branch"),
            "sha": cs.get("head_sha"),
            "url": cs.get("url"),
        },
    )
    title = _clip_text(_mapping(cs.get("head_commit")).get("message"), _MAX_TITLE_CHARS)
    return kind, data, title, None


def _normalize_push(
    repo: str,
    _action: str | None,
    payload: Mapping[str, Any],
) -> tuple[str, dict[str, str | int | bool], str | None, str | None]:
    ref = payload.get("ref")
    branch = ref.removeprefix("refs/heads/") if isinstance(ref, str) and ref.startswith("refs/heads/") else None
    data = _clean_data(
        {
            "repo": repo,
            "ref": ref,
            "branch": branch,
            "after": payload.get("after"),
            "pusher": _mapping(payload.get("pusher")).get("name"),
            "url": payload.get("compare"),
        },
    )
    title = _clip_text(_mapping(payload.get("head_commit")).get("message"), _MAX_TITLE_CHARS)
    return "push", data, title, None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _clip_text(value: object, max_chars: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:max_chars]


def _clean_data(values: Mapping[str, object]) -> dict[str, str | int | bool]:
    return {key: value for key, value in values.items() if isinstance(value, str | int | bool)}


_GITHUB_EVENT_NORMALIZERS: dict[
    str,
    Callable[[str, str | None, Mapping[str, Any]], tuple[str, dict[str, str | int | bool], str | None, str | None]],
] = {
    "pull_request": _normalize_pull_request,
    "pull_request_review": _normalize_pull_request_review,
    "issues": _normalize_issues,
    "release": _normalize_release,
    "check_suite": _normalize_check_suite,
    "push": _normalize_push,
}
