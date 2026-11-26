"""Unified GitHub PR watcher: webhook-nudge routing, access checks, seeding, and ETag REST poller."""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, NoReturn
from urllib.parse import quote

import httpx

from mindroom import github_client as ghc
from mindroom.config.external_trigger_policy import GitHubEventSourceConfig
from mindroom.credentials.manager import CredentialsUnreadableError
from mindroom.external_triggers.policy import is_source_subscriber_allowed
from mindroom.logging_config import get_logger
from mindroom.oauth.providers import OAuthProviderError
from mindroom.reactive_triggers import models as rm

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.reactive_triggers.store import ReactiveTriggerStore

logger = get_logger(__name__)

# Excludes "cancelled" so cancel-in-progress workflows on new pushes never wake as CI failures or all-green passes.
_CI_FAIL_CONCLUSIONS = frozenset({"failure", "timed_out", "action_required"})
_CI_PASS_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
_ACTIVITY_WEBHOOK_EVENTS = frozenset({"issue_comment", "pull_request_review", "pull_request_review_comment"})
_CI_WEBHOOK_EVENTS = frozenset({"check_run", "check_suite"})
_IGNORED_TIMELINE_EVENTS = frozenset(
    [
        "subscribed",
        "unsubscribed",
        "mentioned",
        "deployed",
        "head_ref_deleted",
        "head_ref_force_pushed",
        "committed",
        "base_ref_changed",
        "automatic_base_change_succeeded",
        "closed",
        "merged",
    ],
)
_SEEN_AND_TEXT_LIMIT = 500
_MAX_PAGES = 3
_MAX_BACKOFF_SECONDS = 3600.0
_API_ERRORS = (httpx.HTTPError, TimeoutError, ghc.GitHubClientError)

type _JsonDict = dict[str, Any]
type _FetchFn = Callable[[str, str | None], Awaitable[ghc.GitHubGetResult]]
type _EventPairs = list[tuple[_JsonDict, str]]
type _SyncResult = tuple[Literal["open", "closed", "merged"], str | None, _JsonDict, _EventPairs]


class _GitHubPRWatchError(ValueError):
    """Raised when a GitHub PR watch cannot be created or seeded."""


class _RateLimitedError(RuntimeError):
    """Internal signal that a credential bucket is rate-limited until ``args[0]``."""


class _SeededPRState(NamedTuple):
    """Initial checkpoint and auth mode captured when creating a github_pr watch."""

    checkpoint: rm.GitHubPRCheckpoint
    auth_mode: ghc.GitHubAuthMode


def is_ignored_activity_actor(
    source: rm.GitHubPRSourceSpec,
    *,
    actor: str | None,
    actor_type: str | None = None,
) -> bool:
    """Return whether an activity actor should be ignored per ignore_actors / ignore_bots."""
    normalized = (actor or "").lower()
    if normalized and any(entry.lower() == normalized for entry in source.ignore_actors):
        return True
    return source.ignore_bots and (normalized.endswith("[bot]") or (actor_type or "").lower() == "bot")


def _webhook_source_covers_events(source: GitHubEventSourceConfig, events: Sequence[rm.GitHubPREventType]) -> bool:
    watched = set(events)
    allowed_events = {event_name.lower() for event_name in source.allowed_events}
    required = {"pull_request"} | (_ACTIVITY_WEBHOOK_EVENTS if "activity" in watched else set())
    if not required <= allowed_events:
        return False
    return "ci" not in watched or bool(_CI_WEBHOOK_EVENTS & allowed_events)


def find_covering_github_webhook_source(
    owner: str,
    repo: str,
    events: Sequence[rm.GitHubPREventType],
    config: Config,
    paths: RuntimePaths,
) -> str | None:
    """Return the first authorized GitHub webhook source covering ``repo`` and ``events``, or None."""
    target_repo = repo.lower()
    for source_id, source in sorted(config.external_trigger_policy.event_sources.items()):
        if not isinstance(source, GitHubEventSourceConfig):
            continue
        if not any(allowed_repo.lower() == target_repo for allowed_repo in source.allowed_repos):
            continue
        if _webhook_source_covers_events(source, events) and is_source_subscriber_allowed(source, owner, config, paths):
            return source_id
    return None


def check_github_pr_source_access(
    *,
    owner_user_id: str,
    source_spec: rm.GitHubPRSourceSpec,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Verify current policy and webhook source grants for a github_pr rule."""
    policy = config.external_trigger_policy
    if source_spec.webhook_source_id is None:
        return None if policy.github_pr_polling_enabled else "github_pr_polling_disabled"
    github_source = policy.event_sources.get(source_spec.webhook_source_id)
    if not isinstance(github_source, GitHubEventSourceConfig):
        return "source_removed"
    if not is_source_subscriber_allowed(github_source, owner_user_id, config, runtime_paths):
        return "source_subscriber_revoked"
    repo_allowed = any(repo.lower() == source_spec.repo.lower() for repo in github_source.allowed_repos)
    if not repo_allowed or not _webhook_source_covers_events(github_source, source_spec.events):
        return "source_grant_revoked"
    return None


def nudge_github_pr_watches_for_webhook(
    *,
    source_id: str,
    source: GitHubEventSourceConfig,
    repo: str,
    store: ReactiveTriggerStore,
    config: Config,
    runtime_paths: RuntimePaths,
    now: float,
) -> int:
    """Nudge enabled, unexpired, authorized webhook-backed github_pr watches for ``repo`` to poll now."""
    trigger_ids = [
        rule.trigger_id
        for rule in store.list_triggers(include_disabled=False, source_type="github_pr", source_id=repo.lower())
        if (rule.expires_at is None or rule.expires_at > now)
        and isinstance(rule.source, rm.GitHubPRSourceSpec)
        and rule.source.webhook_source_id == source_id
        and _webhook_source_covers_events(source, rule.source.events)
        and is_source_subscriber_allowed(source, rule.owner_user_id, config, runtime_paths)
    ]
    return store.nudge_github_pr_checkpoints(trigger_ids, now=now)


async def seed_github_pr_watch(
    repo: str,
    number: int,
    events: Sequence[rm.GitHubPREventType],
    *,
    owner_user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    poll_interval_seconds: int,
    webhook_source_id: str | None,
    now: float,
    client: httpx.AsyncClient | None = None,
) -> _SeededPRState:
    """Validate an open PR and seed initial checkpoint state so baseline history does not replay."""
    try:
        token, auth_mode = await ghc.resolve_owner_github_token(owner_user_id, config, runtime_paths)
    except (CredentialsUnreadableError, OAuthProviderError) as exc:
        _fail(f"Unable to resolve GitHub credentials for PR watch: {exc}", exc)
    async with AsyncExitStack() as stack:
        http_client = await _resolve_http_client(stack, client)

        async def _fetch(path: str, etag: str | None) -> ghc.GitHubGetResult:
            response = await ghc.github_get_json(path, token=token, etag=etag, client=http_client)
            if response.status_code in {200, 304}:
                return response
            is_private = auth_mode == "anonymous" and response.status_code in {401, 403, 404}
            hint = " Connect GitHub or save a GitHub token for private repos." if is_private else ""
            _fail(f"Failed to fetch GitHub PR {repo}#{number} (HTTP {response.status_code}).{hint}")

        source_spec = rm.GitHubPRSourceSpec(
            repo=repo,
            number=number,
            events=list(events),
            webhook_source_id=webhook_source_id,
        )
        try:
            pr_state, head_sha, state, _ = await _sync_pr(source_spec, None, _fetch, now=now, require_open=True)
        except _API_ERRORS as exc:
            _fail(f"Failed to reach GitHub API for {repo}#{number}: {exc}", exc)
    next_poll_at = None if webhook_source_id is not None else now + float(poll_interval_seconds)
    checkpoint = rm.GitHubPRCheckpoint(
        head_sha=head_sha,
        pr_state=pr_state,
        seeded=True,
        next_poll_at=next_poll_at,
        state=state,
        updated_at=now,
    )
    return _SeededPRState(checkpoint=checkpoint, auth_mode=auth_mode)


class GitHubPRPoller:
    """Unified bounded-concurrency ETag REST poller for poll-backed and webhook-nudged github_pr watches."""

    def __init__(
        self,
        *,
        store: ReactiveTriggerStore,
        config_provider: Callable[[], Config],
        runtime_paths: RuntimePaths,
        on_enqueued: Callable[[], None] | None = None,
        client: httpx.AsyncClient | None = None,
        max_concurrency: int = 4,
    ) -> None:
        self._store = store
        self._config_provider = config_provider
        self._runtime_paths = runtime_paths
        self._on_enqueued = on_enqueued
        self._client = client
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._bucket_paused_until: dict[object, float] = {}

    async def poll_due_once(self, *, now: float | None = None) -> int:
        """Poll all due (scheduled or webhook-nudged) github_pr triggers once and return enqueued count."""
        config = self._config_provider()
        current_time = time.time() if now is None else now
        policy = config.external_trigger_policy
        if not (policy.enabled and policy.reactive_enabled):
            return 0
        due_watches = await asyncio.to_thread(
            self._store.list_due_github_pr_triggers,
            now=current_time,
            limit=50,
            include_poll_only=policy.github_pr_polling_enabled,
        )
        if not due_watches:
            return 0
        async with AsyncExitStack() as stack:
            http_client = await _resolve_http_client(stack, self._client)
            coros = (
                self._poll_rule(rule, checkpoint, config, http_client, current_time) for rule, checkpoint in due_watches
            )
            outcomes = await asyncio.gather(*coros, return_exceptions=True)
        for (rule, _), outcome in zip(due_watches, outcomes, strict=True):
            if isinstance(outcome, Exception):
                logger.warning("Unexpected github_pr poll error", trigger_id=rule.trigger_id, error=str(outcome))
            elif isinstance(outcome, BaseException):
                raise outcome
        enqueued_count = sum(outcome is True for outcome in outcomes)
        if enqueued_count and self._on_enqueued is not None:
            self._on_enqueued()
        return enqueued_count

    async def _poll_rule(
        self,
        rule: rm.ReactiveTriggerRecord,
        checkpoint: rm.GitHubPRCheckpoint | None,
        config: Config,
        http_client: httpx.AsyncClient,
        now: float,
    ) -> bool:
        async with self._semaphore:
            assert isinstance(rule.source, rm.GitHubPRSourceSpec)
            source_spec = rule.source
            access_error = check_github_pr_source_access(
                owner_user_id=rule.owner_user_id,
                source_spec=source_spec,
                config=config,
                runtime_paths=self._runtime_paths,
            )
            if access_error is not None:
                if source_spec.webhook_source_id and checkpoint is not None:
                    await self._save_checkpoint(rule, checkpoint, now, next_poll_at=None)
                return False
            try:
                return await self._run_poll(rule, source_spec, checkpoint, config, http_client, now)
            except _RateLimitedError as exc:
                await self._save_checkpoint(rule, checkpoint, now, next_poll_at=float(exc.args[0]))
            except (CredentialsUnreadableError, OAuthProviderError, *_API_ERRORS) as exc:
                logger.warning(
                    "GitHub PR poll failed",
                    trigger_id=rule.trigger_id,
                    repo=source_spec.repo,
                    error=str(exc),
                )
                errors = (checkpoint.consecutive_errors if checkpoint is not None else 0) + 1
                backoff = min(_MAX_BACKOFF_SECONDS, float(source_spec.poll_interval_seconds) * (2 ** min(errors, 5)))
                await self._save_checkpoint(
                    rule,
                    checkpoint,
                    now,
                    consecutive_errors=errors,
                    next_poll_at=now + backoff,
                )
            return False

    async def _run_poll(
        self,
        rule: rm.ReactiveTriggerRecord,
        source_spec: rm.GitHubPRSourceSpec,
        checkpoint: rm.GitHubPRCheckpoint | None,
        config: Config,
        http_client: httpx.AsyncClient,
        now: float,
    ) -> bool:
        token, auth_mode = await ghc.resolve_owner_github_token(rule.owner_user_id, config, self._runtime_paths)
        bucket: object = "anonymous" if auth_mode == "anonymous" else (rule.owner_user_id, auth_mode)
        paused_until = self._bucket_paused_until.get(bucket, 0.0)
        if paused_until > now:
            raise _RateLimitedError(paused_until)
        remaining_limits: list[int] = []

        async def _fetch(path: str, etag: str | None) -> ghc.GitHubGetResult:
            response = await ghc.github_get_json(path, token=token, etag=etag, client=http_client)
            raw_remaining = response.headers.get("x-ratelimit-remaining", "").strip()
            if raw_remaining.isascii() and raw_remaining.isdigit():
                remaining_limits.append(int(raw_remaining))
            if response.rate_limited:
                resume_at = now + min(max(response.retry_after_seconds or 60.0, 1.0), _MAX_BACKOFF_SECONDS)
                self._bucket_paused_until[bucket] = max(self._bucket_paused_until.get(bucket, 0.0), resume_at)
                raise _RateLimitedError(resume_at)
            if response.status_code not in {200, 304}:
                msg = f"GitHub API {path} returned HTTP {response.status_code}"
                raise ghc.GitHubClientError(msg)
            return response

        is_seed = checkpoint is None or not checkpoint.seeded
        previous_pr_state = "open" if is_seed or checkpoint is None else checkpoint.pr_state
        pr_state, head_sha, next_state, event_pairs = await _sync_pr(
            source_spec,
            None if is_seed else checkpoint,
            _fetch,
            now=now,
        )
        min_poll = float(config.external_trigger_policy.min_github_pr_poll_interval_seconds)
        base_interval = max(min_poll, float(source_spec.poll_interval_seconds))
        low_quota = 10 if auth_mode == "anonymous" else 100
        slowdown = 2.0 if remaining_limits and min(remaining_limits) < low_quota else 1.0
        delay = max(min_poll, base_interval * slowdown * random.uniform(0.9, 1.1))  # noqa: S311
        next_poll_at = None if source_spec.webhook_source_id is not None else now + delay
        next_checkpoint = rm.GitHubPRCheckpoint(
            head_sha=head_sha,
            pr_state=pr_state,
            seeded=True,
            next_poll_at=next_poll_at,
            state=next_state,
            updated_at=now,
        )
        event = (
            _build_event(rule.trigger_id, source_spec, next_checkpoint, event_pairs, now)
            if (not is_seed and event_pairs)
            else None
        )
        should_disable = (
            event is None
            and pr_state != "open"
            and source_spec.auto_disable_on_close
            and (is_seed or previous_pr_state == "open")
        )
        poll_started_at = (
            checkpoint.next_poll_at if checkpoint is not None and checkpoint.next_poll_at is not None else now
        )
        return await asyncio.to_thread(
            self._store.update_github_pr_checkpoint_and_enqueue,
            rule.trigger_id,
            next_checkpoint,
            event=event,
            disable_reason="pr_closed" if should_disable else None,
            expected_rule_uid=rule.rule_uid,
            poll_started_at=poll_started_at,
        )

    async def _save_checkpoint(
        self,
        rule: rm.ReactiveTriggerRecord,
        checkpoint: rm.GitHubPRCheckpoint | None,
        now: float,
        **updates: object,
    ) -> None:
        base = checkpoint or rm.GitHubPRCheckpoint(updated_at=now)
        updated = base.model_copy(update={"updated_at": now, **updates})
        await asyncio.to_thread(
            self._store.upsert_github_pr_checkpoint,
            rule.trigger_id,
            updated,
            expected_rule_uid=rule.rule_uid,
        )


async def _sync_pr(
    source_spec: rm.GitHubPRSourceSpec,
    checkpoint: rm.GitHubPRCheckpoint | None,
    fetch: _FetchFn,
    *,
    now: float,
    require_open: bool = False,
) -> _SyncResult:
    base = checkpoint if checkpoint is not None and checkpoint.seeded else None
    is_seed = base is None
    state: _JsonDict = dict(base.state) if base is not None else {}
    head_sha = base.head_sha if base is not None else None
    previous_pr_state: Literal["open", "closed", "merged"] = base.pr_state if base is not None else "open"
    pr_state = previous_pr_state
    closed_at: str | None = None

    pr_path = f"/repos/{source_spec.repo}/pulls/{source_spec.number}"
    response = await fetch(pr_path, None if is_seed else _clean_str(state.get("pr_etag")))
    if response.status_code == 200 and isinstance(payload := response.data, dict):
        merged = payload.get("merged") is True or payload.get("merged_at") is not None
        raw_state = (_clean_str(payload.get("state")) or "").lower()
        pr_state = "merged" if merged else ("closed" if raw_state == "closed" else "open")
        head_sha = _clean_str(_as_mapping(payload.get("head")).get("sha")) or head_sha
        closed_at = _clean_str(payload.get("merged_at")) or _clean_str(payload.get("closed_at"))
        state.update(
            pr_etag=response.etag,
            pr_title=_clean_str(payload.get("title"), _SEEN_AND_TEXT_LIMIT),
            pr_url=_clean_str(payload.get("html_url")),
        )
    elif response.status_code == 304:
        state["pr_etag"] = response.etag
    else:
        msg = f"Unexpected PR payload for {source_spec.repo}#{source_spec.number}"
        raise ghc.GitHubClientError(msg)

    if require_open and pr_state != "open":
        _fail(f"GitHub PR {source_spec.repo}#{source_spec.number} is already {pr_state}.")

    pairs: _EventPairs = []
    if "activity" in source_spec.events:
        await _poll_timeline(source_spec, state, fetch, pairs, seed_only=is_seed)
        await _poll_review_comments(source_spec, state, fetch, pairs, seed_only=is_seed, now=now)
    if "ci" in source_spec.events and pr_state == "open" and head_sha:
        await _poll_check_runs(source_spec, head_sha, state, fetch, pairs, seed_only=is_seed)
    terminal_event_type = {"merged": "merge", "closed": "close"}.get(pr_state)
    if not is_seed and previous_pr_state == "open" and terminal_event_type in source_spec.events:
        summary = f"PR #{source_spec.number} {'merged' if pr_state == 'merged' else 'closed without merge'}"
        occurrence_key = closed_at or head_sha or ""
        terminal_item: _JsonDict = {"type": pr_state, "url": state.get("pr_url"), "summary": summary}
        pairs.append((terminal_item, f"pr:{pr_state}:{occurrence_key}"))
    return pr_state, head_sha, state, pairs


def _load_seen_ids(state: _JsonDict, key: str, *, seed_only: bool) -> dict[str, None]:
    if seed_only:
        return {}
    return dict.fromkeys(entry for entry in state.get(key, []) if isinstance(entry, str))


async def _poll_timeline(
    source_spec: rm.GitHubPRSourceSpec,
    state: _JsonDict,
    fetch: _FetchFn,
    out: _EventPairs,
    *,
    seed_only: bool,
) -> None:
    base_url = f"/repos/{source_spec.repo}/issues/{source_spec.number}/timeline?per_page=100&page="
    seen_ids = _load_seen_ids(state, "seen_timeline_ids", seed_only=seed_only)
    page = 1 if seed_only else max(1, int(state.get("timeline_page") or 1))
    for step in range(1 if seed_only else _MAX_PAGES):
        request_etag = None if (seed_only or step > 0) else _clean_str(state.get("timeline_etag"))
        response = await fetch(f"{base_url}{page}", request_etag)
        if seed_only and response.last_page and response.last_page > 1:
            page = response.last_page
            response = await fetch(f"{base_url}{page}", None)
        if response.status_code == 304:
            state["timeline_etag"] = response.etag
            return
        entries = (
            [item for item in response.data if isinstance(item, Mapping)] if isinstance(response.data, list) else []
        )
        for entry in entries:
            event_name = (_clean_str(entry.get("event")) or "").lower()
            user = _as_mapping(entry.get("user")) or _as_mapping(entry.get("actor"))
            timestamp = entry.get("created_at") or entry.get("submitted_at") or ""
            entry_id = entry.get("id")
            dedupe_id = (
                f"{event_name}:{entry_id}"
                if entry_id is not None
                else f"{event_name}:{timestamp}:{user.get('login') or ''}"
            )
            if dedupe_id not in seen_ids:
                seen_ids[dedupe_id] = None
                if not seed_only and (formatted := _format_timeline(source_spec, entry, event_name, user)) is not None:
                    out.append((formatted, f"tl:{dedupe_id}"))
        next_page = response.next_page or (page + 1 if len(entries) >= 100 else None)
        if seed_only or next_page is None or step + 1 >= _MAX_PAGES:
            state["timeline_page"] = page if next_page is None else next_page
            state["timeline_etag"] = response.etag if next_page is None else None
            break
        page = next_page
    state["seen_timeline_ids"] = list(seen_ids)[-_SEEN_AND_TEXT_LIMIT:]


async def _poll_review_comments(
    source_spec: rm.GitHubPRSourceSpec,
    state: _JsonDict,
    fetch: _FetchFn,
    out: _EventPairs,
    *,
    seed_only: bool,
    now: float,
) -> None:
    seen_ids = _load_seen_ids(state, "seen_review_comment_ids", seed_only=seed_only)
    since = None if seed_only else _clean_str(state.get("review_comments_since"))
    request_etag = None if seed_only else _clean_str(state.get("review_comments_etag"))
    direction = "desc" if seed_only else "asc"
    base_url = (
        f"/repos/{source_spec.repo}/pulls/{source_spec.number}/comments?sort=updated&direction={direction}&per_page=100"
    )
    since_query = f"&since={quote(since, safe=':-TZ.')}" if since else ""
    last_timestamp = since
    new_rows: list[tuple[object, str | None, _JsonDict]] = []
    page = 1
    next_etag: str | None = None

    for step in range(1 if seed_only else _MAX_PAGES):
        page_query = f"&page={page}" if page > 1 else ""
        response = await fetch(f"{base_url}{since_query}{page_query}", None if step > 0 else request_etag)
        if response.status_code == 304:
            state["review_comments_etag"] = response.etag
            return
        comments = (
            [item for item in response.data if isinstance(item, Mapping)] if isinstance(response.data, list) else []
        )
        for comment in reversed(comments) if seed_only else comments:
            comment_id = comment.get("id")
            if not isinstance(comment_id, int) or isinstance(comment_id, bool):
                continue
            comment_ts = _clean_str(comment.get("updated_at") or comment.get("created_at"))
            if comment_ts and (last_timestamp is None or comment_ts > last_timestamp):
                last_timestamp = comment_ts
            comment_key = str(comment_id)
            if comment_key not in seen_ids:
                seen_ids[comment_key] = None
                user = _as_mapping(comment.get("user"))
                actor = _clean_str(user.get("login"))
                actor_type = _clean_str(user.get("type"))
                if not seed_only and not is_ignored_activity_actor(source_spec, actor=actor, actor_type=actor_type):
                    body = _clean_str(comment.get("body"), _SEEN_AND_TEXT_LIMIT)
                    entry: _JsonDict = {
                        "id": comment_id,
                        "path": comment.get("path"),
                        "body": body,
                        "url": comment.get("html_url"),
                    }
                    new_rows.append((comment.get("pull_request_review_id"), actor, entry))
        if seed_only or response.next_page is None or step + 1 >= _MAX_PAGES:
            cacheable = (
                not seed_only
                and step == 0
                and response.next_page is None
                and len(comments) < 100
                and last_timestamp == since
            )
            next_etag = response.etag if cacheable else None
            break
        page = response.next_page

    if seed_only and last_timestamp is None:
        last_timestamp = datetime.fromtimestamp(now, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    state["review_comments_since"] = last_timestamp
    state["review_comments_etag"] = next_etag
    state["seen_review_comment_ids"] = list(seen_ids)[-_SEEN_AND_TEXT_LIMIT:]
    _group_review_comments(new_rows, out)


async def _poll_check_runs(
    source_spec: rm.GitHubPRSourceSpec,
    head_sha: str,
    state: _JsonDict,
    fetch: _FetchFn,
    out: _EventPairs,
    *,
    seed_only: bool,
) -> None:
    if seed_only or state.get("ci_head_sha") != head_sha:
        state.update(ci_head_sha=head_sha, checks_etag=None, notified_failed_checks=[], last_ci_overall="none")
    runs: dict[str, Mapping[str, Any]] = {}
    request_etag = None if seed_only else _clean_str(state.get("checks_etag"))
    single_page_etag: str | None = None
    page = 1
    truncated = False

    for step in range(_MAX_PAGES):
        check_path = f"/repos/{source_spec.repo}/commits/{head_sha}/check-runs?per_page=100&page={page}"
        response = await fetch(check_path, None if step > 0 else request_etag)
        if response.status_code == 304:
            state["checks_etag"] = response.etag
            return
        raw_runs = _as_mapping(response.data).get("check_runs")
        for run in raw_runs if isinstance(raw_runs, list) else ():
            if isinstance(run, Mapping):
                run_key = str(run["id"]) if run.get("id") is not None else (_clean_str(run.get("name")) or "check")
                runs.setdefault(run_key, run)
        if step == 0 and response.next_page is None:
            single_page_etag = response.etag
        if response.next_page is None or step + 1 >= _MAX_PAGES:
            truncated = response.next_page is not None
            break
        page = response.next_page

    failed, overall = _tally_checks(runs, truncated=truncated)
    notified_failed = (
        set(failed) if seed_only else {key for key in state.get("notified_failed_checks", []) if isinstance(key, str)}
    )
    for key in sorted(set(failed) - notified_failed):
        summary = f"CI check '{failed[key]['name']}' finished with {failed[key]['conclusion']}"
        failure_item: _JsonDict = {"type": "ci_failure", "id": key, "sha": head_sha, "summary": summary, **failed[key]}
        out.append((failure_item, f"ci:fail:{head_sha}:{key}"))
    if not seed_only and overall == "passing" and state.get("last_ci_overall") != "passing":
        run_ids_digest = hashlib.sha256(",".join(sorted(runs)).encode()).hexdigest()[:16]
        summary = f"All {len(runs)} GitHub Check Runs passed for {head_sha[:7]}"
        pass_item: _JsonDict = {"type": "ci_passing", "sha": head_sha, "total": len(runs), "summary": summary}
        out.append((pass_item, f"ci:pass:{head_sha}:{run_ids_digest}"))
    state["checks_etag"] = single_page_etag
    state["notified_failed_checks"] = sorted(notified_failed | set(failed))
    state["last_ci_overall"] = overall


def _tally_checks(runs: dict[str, Mapping[str, Any]], *, truncated: bool) -> tuple[dict[str, _JsonDict], str]:
    failed: dict[str, _JsonDict] = {}
    has_pending = False
    has_non_passing = False
    for key, run in runs.items():
        status = (_clean_str(run.get("status")) or "").lower()
        conclusion = (_clean_str(run.get("conclusion")) or "").lower()
        if status != "completed":
            has_pending = True
        elif conclusion in _CI_FAIL_CONCLUSIONS:
            url = _clean_str(run.get("html_url") or run.get("url"))
            failed[key] = {"name": _clean_str(run.get("name")) or "check", "conclusion": conclusion, "url": url}
        elif conclusion not in _CI_PASS_CONCLUSIONS:
            has_non_passing = True
    if not runs or failed:
        return failed, "failing" if failed else "none"
    if has_pending or truncated:
        return failed, "pending"
    return failed, "non_passing" if has_non_passing else "passing"


def _format_timeline(
    source_spec: rm.GitHubPRSourceSpec,
    entry: Mapping[str, Any],
    event_name: str,
    user: Mapping[str, Any],
) -> _JsonDict | None:
    actor = _clean_str(user.get("login"))
    if not event_name or event_name in _IGNORED_TIMELINE_EVENTS:
        return None
    if is_ignored_activity_actor(source_spec, actor=actor, actor_type=_clean_str(user.get("type"))):
        return None
    body = _clean_str(entry.get("body"), _SEEN_AND_TEXT_LIMIT)
    who = actor or "someone"
    base: _JsonDict = {"id": entry.get("id"), "actor": actor, "url": entry.get("html_url")}
    suffix = f": {body}" if body else ""
    if event_name == "reviewed":
        review_state = (_clean_str(entry.get("state")) or "").lower()
        if review_state == "commented" and not body:
            return None
        summary = f"Review ({review_state or 'submitted'}) by {who}{suffix}"
        return {**base, "type": "review", "state": review_state, "body": body, "summary": summary}
    if event_name == "commented":
        return {**base, "type": "comment", "body": body, "summary": f"Comment by {who}{suffix}"}
    label_name = _clean_str(_as_mapping(entry.get("label")).get("name"))
    detail = f" ({label_name})" if label_name else suffix
    return {**base, "type": event_name, "summary": f"{who} {event_name.replace('_', ' ')}{detail}"}


def _group_review_comments(rows: list[tuple[object, str | None, _JsonDict]], out: _EventPairs) -> None:
    grouped: dict[object, list[tuple[object, str | None, _JsonDict]]] = {}
    for row in rows:
        review_id, _, entry = row
        group_key = ("r", review_id) if review_id is not None else ("c", entry["id"])
        grouped.setdefault(group_key, []).append(row)
    for group in grouped.values():
        review_id = group[0][0]
        actor = group[0][1]
        entries = [entry for _, _, entry in group]
        who = actor or "someone"
        paths = list(dict.fromkeys(path for entry in entries if (path := _clean_str(entry["path"]))))
        path_summary = f" on {', '.join(paths[:3])}" if paths else ""
        dedupe_key = "|".join(sorted(f"rc:{entry['id']}" for entry in entries))
        if len(group) == 1:
            body_suffix = f": {entries[0]['body']}" if entries[0]["body"] else ""
            summary = f"Inline comment by {who}{path_summary}{body_suffix}"
            item: _JsonDict = {"type": "review_comment", **entries[0], "summary": summary}
        else:
            summary = f"{len(group)} inline comments by {who}{path_summary}"
            item = {
                "type": "review_comments",
                "count": len(group),
                "paths": paths,
                "comments": entries,
                "url": entries[0]["url"],
                "summary": summary,
            }
        out.append(({"review_id": review_id, "actor": actor, **item}, dedupe_key))


def _build_event(
    trigger_id: str,
    source_spec: rm.GitHubPRSourceSpec,
    checkpoint: rm.GitHubPRCheckpoint,
    pairs: _EventPairs,
    now: float,
) -> rm.TriggerEvent:
    items = [item for item, _ in pairs]
    keys = sorted(part for _, key in pairs for part in key.split("|"))
    item_types = {item.get("type") for item in items}
    kind = (
        "github_pr.merged"
        if "merged" in item_types
        else "github_pr.closed"
        if "closed" in item_types
        else "github_pr.update"
    )
    dedupe_key = f"ghpr:{hashlib.sha256(f'{trigger_id}:{"|".join(keys)}'.encode()).hexdigest()[:32]}"
    summaries = [str(item["summary"]) for item in items if item.get("summary")]
    data: _JsonDict = {
        "repo": source_spec.repo,
        "number": source_spec.number,
        "head_sha": checkpoint.head_sha,
        "state": checkpoint.pr_state,
        "merged": checkpoint.pr_state == "merged",
        "terminal": checkpoint.pr_state != "open",
        "items": items,
    }
    if pr_url := _clean_str(checkpoint.state.get("pr_url")):
        data["url"] = pr_url
    actor = next((login for item in items if (login := _clean_str(item.get("actor")))), None)
    title = _clean_str(checkpoint.state.get("pr_title")) or f"{source_spec.repo}#{source_spec.number}"
    return rm.TriggerEvent.model_validate(
        {
            "source_type": "github_pr",
            "source_id": source_spec.repo.lower(),
            "kind": kind,
            "event_id": dedupe_key,
            "data": data,
            "timestamp": now,
            "actor": actor,
            "title": title,
            "summary": "\n".join(summaries)[:2000] if summaries else None,
        },
    )


async def _resolve_http_client(stack: AsyncExitStack, client: httpx.AsyncClient | None) -> httpx.AsyncClient:
    if client is not None:
        return client
    return await stack.enter_async_context(httpx.AsyncClient(timeout=20.0, follow_redirects=False))


def _fail(msg: str, cause: Exception | None = None) -> NoReturn:
    raise _GitHubPRWatchError(msg) from cause


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _clean_str(value: object, limit: int | None = None) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped[:limit] if limit is not None else stripped
