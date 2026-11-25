"""Native GitHub PR watcher: webhook selection/matching, seeding, and ETag REST poller."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import random
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import httpx

from mindroom.config.external_trigger_policy import GitHubEventSourceConfig
from mindroom.credentials.manager import CredentialsUnreadableError
from mindroom.external_triggers.policy import is_source_subscriber_allowed
from mindroom.github_client import (
    GitHubAuthMode,
    GitHubClientError,
    GitHubGetResult,
    github_get_json,
    resolve_owner_github_token,
)
from mindroom.logging_config import get_logger
from mindroom.oauth.providers import OAuthProviderError
from mindroom.reactive_triggers.models import (
    DisabledReason,
    GitHubPRCheckpoint,
    GitHubPREventType,
    GitHubPRSourceSpec,
    ReactiveTriggerRecord,
    TriggerEvent,
)

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.reactive_triggers.store import ReactiveTriggerStore

logger = get_logger(__name__)
_CI_FAIL = frozenset({"failure", "timed_out", "action_required", "cancelled"})
_IGNORED_TIMELINE = frozenset(
    (
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
    ),
)
_MAX_SEEN_IDS, _MAX_SEED_PAGES, _MAX_POLL_PAGES, _MAX_TEXT = 500, 10, 3, 500
_OVERLAP_SEC, _MAX_BACKOFF_SEC = 60.0, 3600.0
PRState = Literal["open", "closed", "merged"]
_BucketKey = Literal["anonymous"] | tuple[str, GitHubAuthMode]
_FetchFn = Callable[[str, str | None], Awaitable[GitHubGetResult]]


class GitHubPRWatchError(ValueError):
    """Raised when a GitHub PR watch cannot be created or seeded."""


class _RateLimitedError(RuntimeError):
    def __init__(self, resume_at: float) -> None:
        super().__init__("GitHub rate limited")
        self.resume_at = resume_at


class _PollHTTPError(RuntimeError):
    """Raised when a GitHub REST poll call fails with an unexpected HTTP status."""


@dataclass(frozen=True, slots=True)
class WebhookPRMatch:
    """Result of matching one normalized GitHub webhook event against a github_pr rule."""

    matched: bool
    checkpoint: GitHubPRCheckpoint | None = None
    event: TriggerEvent | None = None
    dedupe_key: str | None = None
    disable_immediately: bool = False


@dataclass(frozen=True, slots=True)
class SeededPRState:
    """Initial checkpoint and PR metadata captured when creating a github_pr watch."""

    checkpoint: GitHubPRCheckpoint
    auth_mode: GitHubAuthMode
    pr_state: PRState = "open"
    head_sha: str | None = None
    pr_title: str | None = None
    pr_url: str | None = None
    notes: list[str] = field(default_factory=list)


def is_ignored_activity_actor(source: GitHubPRSourceSpec, *, actor: str | None, actor_type: str | None = None) -> bool:
    """Return whether an activity actor should be ignored per ignore_actors / ignore_bots."""
    if actor:
        low = actor.lower()
        if any(a.lower() == low for a in source.ignore_actors) or (source.ignore_bots and low.endswith("[bot]")):
            return True
    return bool(source.ignore_bots and actor_type and actor_type.lower() == "bot")


def find_covering_github_webhook_source(
    owner_user_id: str,
    repo: str,
    events: Sequence[GitHubPREventType],
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[str | None, list[str]]:
    """Find the first authorized GitHub webhook source covering ``repo`` and ``events``."""
    repo_low, req, fallback = repo.lower(), set(events), []
    for sid, src in sorted(config.external_trigger_policy.event_sources.items()):
        if not isinstance(src, GitHubEventSourceConfig):
            continue
        if not is_source_subscriber_allowed(src, owner_user_id, config, runtime_paths):
            continue
        if not any(r.lower() == repo_low for r in src.allowed_repos):
            continue
        allowed = set(src.allowed_events)
        checks = (
            ("pull_request" not in allowed, "pull_request"),
            ("activity" in req and not ({"issue_comment", "pull_request_review"} & allowed), "activity"),
            ("ci" in req and not ({"check_run", "check_suite"} & allowed), "ci"),
        )
        if missing := [label for needed, label in checks if needed]:
            fallback.append(f"Source {sid!r} allows {repo} but lacks {', '.join(missing)}; falling back to polling.")
            continue
        notes: list[str] = []
        if "activity" in req:
            act_evs = ("issue_comment", "pull_request_review", "pull_request_review_comment")
            notes.extend(
                f"Source {sid!r} lacks {e!r}; those updates will not wake." for e in act_evs if e not in allowed
            )
        if "ci" in req:
            ci_evs = ("check_run", "check_suite")
            notes.extend(f"Source {sid!r} lacks {e!r}; CI coverage is partial." for e in ci_evs if e not in allowed)
        return sid, notes
    return None, fallback


def match_github_webhook_to_pr_rule(
    rule: ReactiveTriggerRecord,
    checkpoint: GitHubPRCheckpoint | None,
    event: TriggerEvent,
    *,
    allowed_events: Collection[str],
) -> WebhookPRMatch:
    """Match a normalized GitHub webhook event against a webhook-backed github_pr rule."""
    if not isinstance(rule.source, GitHubPRSourceSpec):
        return WebhookPRMatch(matched=False)
    src, base = rule.source, event.kind.split(".", 1)[0]
    if not _webhook_targets_pr(src, checkpoint, event, base):
        return WebhookPRMatch(matched=False)

    base_cp = checkpoint or GitHubPRCheckpoint(seeded=True, updated_at=event.timestamp)
    sha = (_opt_str(event.data.get("head_sha")) if base == "pull_request" else None) or base_cp.head_sha
    url = _opt_str(event.data.get("url"))

    if event.kind in {"pull_request.merged", "pull_request.closed"}:
        merged = event.kind == "pull_request.merged" or event.data.get("merged") is True
        pr_state: PRState = "merged" if merged else "closed"
        cp = _cp(base_cp, updated_at=event.timestamp, head_sha=sha, pr_state=pr_state, seeded=True)
        if ("merge" if merged else "close") not in src.events:
            return WebhookPRMatch(matched=True, checkpoint=cp, disable_immediately=src.auto_disable_on_close)
        item = _item(pr_state, f"PR #{src.number} {pr_state}", actor=event.actor, url=url)
        ev = _pr_event(
            src,
            f"github_pr.{pr_state}",
            event.event_id,
            event.timestamp,
            cp,
            [item],
            event.actor,
            event.title,
            url,
            True,
        )
        return WebhookPRMatch(matched=True, checkpoint=cp, event=ev, dedupe_key=event.event_id)

    reopen = {"pull_request.opened", "pull_request.synchronize", "pull_request.reopened"}
    pr_state = "open" if event.kind in reopen else base_cp.pr_state
    cp = _cp(base_cp, updated_at=event.timestamp, head_sha=sha, pr_state=pr_state, seeded=True)
    classified = _classify_webhook(src, event, allowed_events)
    if classified is None:
        return WebhookPRMatch(matched=base == "pull_request", checkpoint=cp if base == "pull_request" else None)
    actor, item = classified
    ev = _pr_event(src, "github_pr.update", event.event_id, event.timestamp, cp, [item], actor, event.title, url, False)
    return WebhookPRMatch(matched=True, checkpoint=cp, event=ev, dedupe_key=event.event_id)


def _webhook_targets_pr(src: GitHubPRSourceSpec, cp: GitHubPRCheckpoint | None, ev: TriggerEvent, base: str) -> bool:
    repo = ev.data.get("repo")
    if not isinstance(repo, str) or repo.lower() != src.repo.lower():
        return False
    if base == "issue_comment" and ev.data.get("is_pull_request") is not True:
        return False
    raw = ev.data.get("pr_numbers")
    nums = [n for n in raw if isinstance(n, int) and not isinstance(n, bool)] if isinstance(raw, list) else []
    num = ev.data.get("number")
    if (isinstance(num, int) and not isinstance(num, bool) and num == src.number) or src.number in nums:
        return True
    ci_ev = base in {"check_run", "check_suite"} and not nums
    return bool(ci_ev and cp and cp.head_sha and ev.data.get("sha") == cp.head_sha)


def _classify_webhook(
    src: GitHubPRSourceSpec,
    ev: TriggerEvent,
    allowed: Collection[str],
) -> tuple[str | None, dict[str, Any]] | None:
    kind, d, url = ev.kind, ev.data, ev.data.get("url")
    if kind in {"pull_request.labeled", "pull_request.reopened", "pull_request.ready_for_review"}:
        if "activity" not in src.events or is_ignored_activity_actor(
            src,
            actor=ev.actor,
            actor_type=_opt_str(d.get("actor_type")),
        ):
            return None
        act = kind.split(".", 1)[1]
        return ev.actor, _item(act, f"{ev.actor or 'someone'} {act.replace('_', ' ')}", actor=ev.actor, url=url)
    if kind in {"issue_comment.created", "pull_request_review.submitted", "pull_request_review_comment.created"}:
        return _classify_activity_webhook(src, ev, allowed)
    if kind == "check_run.completed" and "ci" in src.events and (c := _opt_str(d.get("conclusion"))) in _CI_FAIL:
        name = _opt_str(d.get("name")) or "check"
        return ev.actor, _item("ci_failure", f"CI '{name}' {c}", name=name, conclusion=c, sha=d.get("sha"), url=url)
    if (
        kind == "check_suite.completed"
        and "ci" in src.events
        and ((c := _opt_str(d.get("conclusion"))) in _CI_FAIL or c == "success")
    ):
        t = "ci_passing" if c == "success" else "ci_failure"
        return ev.actor, _item(t, f"CI check suite {c}", conclusion=c, sha=d.get("sha"), url=url)
    return None


def _classify_activity_webhook(
    src: GitHubPRSourceSpec,
    ev: TriggerEvent,
    allowed: Collection[str],
) -> tuple[str | None, dict[str, Any]] | None:
    d, actor = ev.data, _opt_str(ev.data.get("author")) or ev.actor
    atype = _opt_str(d.get("author_type")) or _opt_str(d.get("actor_type"))
    if "activity" not in src.events or is_ignored_activity_actor(src, actor=actor, actor_type=atype):
        return None
    body, url, who = _clip(ev.summary), d.get("url"), actor or "someone"
    sfx = f": {body}" if body else ""
    if ev.kind == "issue_comment.created":
        return actor, _item(
            "comment",
            f"Comment by {who}{sfx}",
            actor=actor,
            comment_id=d.get("comment_id"),
            body=body,
            url=url,
        )
    if ev.kind == "pull_request_review.submitted":
        st = (_opt_str(d.get("state")) or "").lower()
        if st == "commented" and not body and "pull_request_review_comment" in allowed:
            return None
        msg = f"Review ({st or 'submitted'}) by {who}{sfx}"
        return actor, _item("review", msg, actor=actor, state=st, review_id=d.get("review_id"), body=body, url=url)
    path = _opt_str(d.get("path"))
    msg = f"Inline comment by {who}{f' on {path}' if path else ''}{sfx}"
    return actor, _item(
        "review_comment",
        msg,
        actor=actor,
        comment_id=d.get("comment_id"),
        review_id=d.get("review_id"),
        path=path,
        body=body,
        url=url,
    )


async def seed_github_pr_watch(
    repo: str,
    number: int,
    events: Sequence[GitHubPREventType],
    *,
    owner_user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    poll_interval_seconds: int,
    now: float,
    webhook_only: bool = False,
    client: httpx.AsyncClient | None = None,
) -> SeededPRState:
    """Validate an open PR and seed initial checkpoint state so baseline history does not replay."""
    notes: list[str] = []
    try:
        token, auth_mode = await resolve_owner_github_token(owner_user_id, config, runtime_paths)
    except (CredentialsUnreadableError, OAuthProviderError) as exc:
        if not webhook_only:
            msg = f"Unable to resolve GitHub credentials for PR watch: {exc}"
            raise GitHubPRWatchError(msg) from exc
        token, auth_mode = None, "anonymous"
        notes.append("Could not read saved GitHub credentials for initial head_sha; webhook delivery still works.")

    res = await _seed_get_pr(repo, number, token, auth_mode, webhook_only, notes, client)
    if res is None:
        return SeededPRState(GitHubPRCheckpoint(seeded=True, updated_at=now), auth_mode, notes=notes)

    pr_state, head_sha, title, url = _extract_pr_summary(res.data)
    if pr_state != "open":
        msg = f"GitHub PR {repo}#{number} is already {pr_state}."
        raise GitHubPRWatchError(msg)
    if webhook_only:
        cp = GitHubPRCheckpoint(head_sha=head_sha, pr_state="open", seeded=True, updated_at=now)
        return SeededPRState(cp, auth_mode, pr_state, head_sha, title, url, notes)

    state: dict[str, Any] = {"pr_etag": res.etag, "pr_title": title, "pr_url": url}
    src = GitHubPRSourceSpec(repo=repo, number=number, events=list(events), poll_interval_seconds=poll_interval_seconds)

    async def _fetch(path: str, etag: str | None) -> GitHubGetResult:
        try:
            got = await github_get_json(path, token=token, etag=etag, client=client)
        except (httpx.HTTPError, TimeoutError, GitHubClientError) as exc:
            msg = f"Failed to seed GitHub PR {repo}#{number} state: {exc}"
            raise GitHubPRWatchError(msg) from exc
        if got.status_code not in {200, 304}:
            msg = f"Failed to seed GitHub PR {repo}#{number} endpoint {path} (HTTP {got.status_code})."
            raise GitHubPRWatchError(msg)
        return got

    await _collect_endpoints(src, head_sha, state, now, _fetch, seed_only=True)
    nxt = _next_poll_at(poll_interval_seconds, now, auth_mode, _parse_remaining(res.headers))
    cp = GitHubPRCheckpoint(
        head_sha=head_sha,
        pr_state=pr_state,
        seeded=True,
        next_poll_at=nxt,
        state=state,
        updated_at=now,
    )
    return SeededPRState(cp, auth_mode, pr_state, head_sha, title, url, notes)


async def _seed_get_pr(
    repo: str,
    number: int,
    token: str | None,
    auth_mode: GitHubAuthMode,
    webhook_only: bool,
    notes: list[str],
    client: httpx.AsyncClient | None,
) -> GitHubGetResult | None:
    try:
        res = await github_get_json(f"/repos/{repo}/pulls/{number}", token=token, client=client)
    except (httpx.HTTPError, TimeoutError, GitHubClientError) as exc:
        if webhook_only:
            notes.append(f"Initial PR lookup failed for {repo}#{number}; fork CI will match after next PR update.")
            return None
        msg = f"Failed to reach GitHub API for {repo}#{number}: {exc}"
        raise GitHubPRWatchError(msg) from exc
    if res.status_code == 200 and isinstance(res.data, dict):
        return res
    if webhook_only:
        notes.append(f"Initial PR lookup returned HTTP {res.status_code}; fork CI will match after next PR update.")
        return None
    hint = (
        " Connect GitHub for private repos." if auth_mode == "anonymous" and res.status_code in {401, 403, 404} else ""
    )
    msg = f"Failed to fetch GitHub PR {repo}#{number} (HTTP {res.status_code}).{hint}"
    raise GitHubPRWatchError(msg)


class GitHubPRPoller:
    """Bounded-concurrency ETag REST poller for poll-backed github_pr reactive triggers."""

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
        self._store, self._config_provider, self._runtime_paths = store, config_provider, runtime_paths
        self._on_enqueued, self._client = on_enqueued, client
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._bucket_paused_until: dict[_BucketKey, float] = {}

    async def poll_due_once(self, *, now: float | None = None) -> int:
        """Poll all due poll-backed github_pr triggers once and return newly enqueued firing count."""
        cfg = self._config_provider()
        pol = cfg.external_trigger_policy
        if not (pol.enabled and pol.reactive_enabled and pol.github_pr_polling_enabled):
            return 0
        now_ts = time.time() if now is None else now
        due = await asyncio.to_thread(self._store.list_due_github_pr_triggers, now=now_ts, limit=50)
        if not due:
            return 0

        async def _run(active: httpx.AsyncClient) -> list[bool]:
            async def _one(rule: ReactiveTriggerRecord, cp: GitHubPRCheckpoint | None) -> bool:
                async with self._semaphore:
                    return await self._poll_rule(rule, cp, cfg, active, now_ts)

            return list(await asyncio.gather(*(_one(r, c) for r, c in due)))

        if self._client is not None:
            results = await _run(self._client)
        else:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as owned:
                results = await _run(owned)
        enqueued = sum(1 for ok in results if ok)
        if enqueued > 0 and self._on_enqueued is not None:
            self._on_enqueued()
        return enqueued

    async def _poll_rule(
        self,
        rule: ReactiveTriggerRecord,
        cp: GitHubPRCheckpoint | None,
        cfg: Config,
        client: httpx.AsyncClient,
        now: float,
    ) -> bool:
        if not isinstance(rule.source, GitHubPRSourceSpec) or rule.source.webhook_source_id is not None:
            return False
        try:
            token, auth_mode = await resolve_owner_github_token(rule.owner_user_id, cfg, self._runtime_paths)
        except (CredentialsUnreadableError, OAuthProviderError) as exc:
            logger.warning(
                "GitHub credentials unavailable for github_pr poll",
                trigger_id=rule.trigger_id,
                error=str(exc),
            )
            await self._record_error(rule, cp, now)
            return False

        bucket: _BucketKey = "anonymous" if auth_mode == "anonymous" else (rule.owner_user_id, auth_mode)
        if (paused := self._bucket_paused_until.get(bucket, 0.0)) > now:
            await self._upsert_cp(rule, _cp(cp, updated_at=now, next_poll_at=paused))
            return False

        min_rem: list[int | None] = [None]

        async def _fetch(path: str, etag: str | None) -> GitHubGetResult:
            res = await github_get_json(path, token=token, etag=etag, client=client)
            if (rem := _parse_remaining(res.headers)) is not None:
                min_rem[0] = rem if min_rem[0] is None else min(min_rem[0], rem)
            if res.rate_limited:
                wait = 60.0 if res.retry_after_seconds is None else res.retry_after_seconds
                resume = now + min(max(wait, 1.0), _MAX_BACKOFF_SEC)
                self._bucket_paused_until[bucket] = max(self._bucket_paused_until.get(bucket, 0.0), resume)
                raise _RateLimitedError(resume)
            if res.status_code not in {200, 304}:
                msg = f"GitHub API {path} returned HTTP {res.status_code}"
                raise _PollHTTPError(msg)
            return res

        try:
            return await self._poll_body(rule, cp, auth_mode, now, _fetch, min_rem)
        except _RateLimitedError as exc:
            await self._upsert_cp(rule, _cp(cp, updated_at=now, next_poll_at=exc.resume_at))
        except (_PollHTTPError, httpx.HTTPError, TimeoutError, GitHubClientError) as exc:
            logger.warning("GitHub PR poll failed", trigger_id=rule.trigger_id, repo=rule.source.repo, error=str(exc))
            await self._record_error(rule, cp, now)
        return False

    async def _poll_body(
        self,
        rule: ReactiveTriggerRecord,
        cp: GitHubPRCheckpoint | None,
        auth_mode: GitHubAuthMode,
        now: float,
        fetch: _FetchFn,
        min_rem: list[int | None],
    ) -> bool:
        assert isinstance(rule.source, GitHubPRSourceSpec)
        src = rule.source
        state: dict[str, Any] = copy.deepcopy(cp.state) if cp is not None else {}
        head_sha, pr_state = (cp.head_sha, cp.pr_state) if cp is not None else (None, "open")

        pr_etag = _opt_str(state.get("pr_etag")) if (cp is not None and cp.seeded) else None
        pr_res = await fetch(f"/repos/{src.repo}/pulls/{src.number}", pr_etag)
        if pr_res.status_code == 200 and isinstance(pr_res.data, dict):
            pr_state, new_sha, title, url = _extract_pr_summary(pr_res.data)
            head_sha = new_sha or head_sha
            state.update(pr_etag=pr_res.etag, pr_title=title, pr_url=url)
        elif pr_res.status_code == 304:
            state["pr_etag"] = pr_res.etag or state.get("pr_etag")
        else:
            msg = f"Unexpected PR payload for {src.repo}#{src.number}"
            raise _PollHTTPError(msg)

        if cp is None or not cp.seeded:
            await _collect_endpoints(src, head_sha, state, now, fetch, seed_only=True)
            nxt = _next_poll_at(src.poll_interval_seconds, now, auth_mode, min_rem[0])
            new_cp = GitHubPRCheckpoint(
                head_sha=head_sha,
                pr_state=pr_state,
                seeded=True,
                next_poll_at=nxt,
                state=state,
                updated_at=now,
            )
            disable: DisabledReason | None = "pr_closed" if (pr_state != "open" and src.auto_disable_on_close) else None
            await self._commit_cp(rule, new_cp, now=now, disable=disable)
            return False

        items, keys = await _collect_endpoints(
            src,
            head_sha,
            state,
            now,
            fetch,
            seed_only=False,
            poll_ci=pr_state == "open",
        )
        merged, closed = cp.pr_state == "open" and pr_state == "merged", cp.pr_state == "open" and pr_state == "closed"
        if merged and "merge" in src.events:
            items.append(_item("merged", f"PR #{src.number} merged", url=state.get("pr_url")))
            keys.append(f"pr:merged:{head_sha or ''}")
        elif closed and "close" in src.events:
            items.append(_item("closed", f"PR #{src.number} closed without merge", url=state.get("pr_url")))
            keys.append(f"pr:closed:{head_sha or ''}")

        nxt = _next_poll_at(src.poll_interval_seconds, now, auth_mode, min_rem[0])
        new_cp = GitHubPRCheckpoint(
            head_sha=head_sha,
            pr_state=pr_state,
            seeded=True,
            next_poll_at=nxt,
            state=state,
            updated_at=now,
        )
        terminal = pr_state in {"closed", "merged"}
        if not items:
            disable = "pr_closed" if (terminal and src.auto_disable_on_close) else None
            await self._commit_cp(rule, new_cp, now=now, disable=disable)
            return False

        kind = (
            "github_pr.merged"
            if (merged and "merge" in src.events)
            else ("github_pr.closed" if (closed and "close" in src.events) else "github_pr.update")
        )
        dkey = f"ghpr:{hashlib.sha256(f'{rule.trigger_id}:{"|".join(sorted(keys))}'.encode()).hexdigest()[:32]}"
        actor = next((a for it in items if (a := _opt_str(it.get("actor")))), None)
        ev = _pr_event(
            src,
            kind,
            dkey,
            now,
            new_cp,
            items,
            actor,
            _opt_str(state.get("pr_title")),
            _opt_str(state.get("pr_url")),
            terminal,
        )
        return await self._commit_cp(rule, new_cp, now=now, dedupe_key=dkey, event=ev)

    async def _commit_cp(
        self,
        rule: ReactiveTriggerRecord,
        cp: GitHubPRCheckpoint,
        *,
        now: float,
        dedupe_key: str | None = None,
        event: TriggerEvent | None = None,
        disable: DisabledReason | None = None,
    ) -> bool:
        fn = self._store.update_github_pr_checkpoint_and_enqueue
        return await asyncio.to_thread(
            fn,
            rule.trigger_id,
            cp,
            dedupe_key=dedupe_key,
            event=event,
            disable_reason=disable,
            expected_rule_uid=rule.rule_uid,
            now=now,
        )

    async def _upsert_cp(self, rule: ReactiveTriggerRecord, cp: GitHubPRCheckpoint) -> None:
        await asyncio.to_thread(
            self._store.upsert_github_pr_checkpoint,
            rule.trigger_id,
            cp,
            expected_rule_uid=rule.rule_uid,
        )

    async def _record_error(self, rule: ReactiveTriggerRecord, cp: GitHubPRCheckpoint | None, now: float) -> None:
        assert isinstance(rule.source, GitHubPRSourceSpec)
        errs = (cp.consecutive_errors if cp is not None else 0) + 1
        backoff = min(_MAX_BACKOFF_SEC, float(rule.source.poll_interval_seconds) * (2 ** min(errs, 5)))
        await self._upsert_cp(rule, _cp(cp, updated_at=now, consecutive_errors=errs, next_poll_at=now + backoff))


async def _collect_endpoints(
    src: GitHubPRSourceSpec,
    head_sha: str | None,
    state: dict[str, Any],
    now: float,
    fetch: _FetchFn,
    *,
    seed_only: bool,
    poll_ci: bool = True,
) -> tuple[list[dict[str, Any]], list[str]]:
    items: list[dict[str, Any]] = []
    keys: list[str] = []
    if "activity" in src.events:
        tl_i, tl_k = await _poll_timeline(src, state, fetch, seed_only=seed_only)
        rc_i, rc_k = await _poll_review_comments(src, state, now, fetch, seed_only=seed_only)
        items.extend((*tl_i, *rc_i))
        keys.extend((*tl_k, *rc_k))
    if "ci" in src.events and poll_ci and head_sha:
        ci_i, ci_k = await _poll_check_runs(src, head_sha, state, fetch, seed_only=seed_only)
        items.extend(ci_i)
        keys.extend(ci_k)
    return items, keys


async def _poll_timeline(
    src: GitHubPRSourceSpec,
    state: dict[str, Any],
    fetch: _FetchFn,
    *,
    seed_only: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    page = 1 if seed_only else max(1, int(state.get("timeline_page") or 1))
    seen = [] if seed_only else [k for k in state.get("seen_timeline_ids", []) if isinstance(k, str)]
    seen_set, items, keys = set(seen), [], []
    for step in range(_MAX_SEED_PAGES if seed_only else _MAX_POLL_PAGES):
        etag = None if seed_only or step > 0 else _opt_str(state.get("timeline_etag"))
        res = await fetch(f"/repos/{src.repo}/issues/{src.number}/timeline?per_page=100&page={page}", etag)
        if res.status_code == 304:
            state["timeline_etag"] = res.etag or state.get("timeline_etag")
            break
        entries = res.data if isinstance(res.data, list) else []
        for entry in entries:
            if not isinstance(entry, Mapping) or (key := _timeline_key(entry)) in seen_set:
                continue
            seen_set.add(key)
            seen.append(key)
            if not seed_only and (fmt := _format_timeline_entry(src, entry)) is not None:
                items.append(fmt)
                keys.append(f"tl:{key}")
        state.update(timeline_page=page, timeline_etag=res.etag)
        if (next_p := res.next_page or (page + 1 if len(entries) >= 100 else None)) is None:
            break
        page = next_p
    state["seen_timeline_ids"] = seen[-_MAX_SEEN_IDS:]
    return items, keys


async def _poll_review_comments(
    src: GitHubPRSourceSpec,
    state: dict[str, Any],
    now: float,
    fetch: _FetchFn,
    *,
    seed_only: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    default_since = _iso_utc(now - _OVERLAP_SEC)
    since = default_since if seed_only else (_opt_str(state.get("review_comments_since")) or default_since)
    prev_max = 0 if seed_only else int(state.get("max_review_comment_id") or 0)
    max_id, raw_new, page, total = prev_max, [], 1, 0
    for step in range(_MAX_SEED_PAGES if seed_only else _MAX_POLL_PAGES):
        etag = None if seed_only or step > 0 else _opt_str(state.get("review_comments_etag"))
        q = (
            f"per_page=100&page={page}"
            if seed_only
            else f"per_page=100&since={since}" + (f"&page={page}" if page > 1 else "")
        )
        res = await fetch(f"/repos/{src.repo}/pulls/{src.number}/comments?{q}", etag)
        if res.status_code == 304:
            state["review_comments_etag"] = res.etag or state.get("review_comments_etag")
            return [], []
        comments = res.data if isinstance(res.data, list) else []
        total += len(comments)
        if not seed_only and step == 0:
            state["review_comments_etag"] = res.etag if res.next_page is None else None
        for c in comments:
            if (
                isinstance(c, Mapping)
                and isinstance(cid := c.get("id"), int)
                and not isinstance(cid, bool)
                and cid > prev_max
            ):
                max_id = max(max_id, cid)
                if not seed_only:
                    raw_new.append(c)
        if res.next_page is None:
            break
        page = res.next_page
    state["max_review_comment_id"] = max_id
    state["review_comments_since"] = default_since if (seed_only or total >= 80) else since
    if seed_only or total >= 80:
        state["review_comments_etag"] = None
    return _group_review_comments(src, raw_new) if raw_new else ([], [])


def _group_review_comments(
    src: GitHubPRSourceSpec,
    comments: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    grouped: dict[object, list[Mapping[str, Any]]] = {}
    for c in comments:
        u = _mapping(c.get("user"))
        if is_ignored_activity_actor(src, actor=_opt_str(u.get("login")), actor_type=_opt_str(u.get("type"))):
            continue
        rid = c.get("pull_request_review_id")
        grouped.setdefault(("r", rid) if rid is not None else ("c", c.get("id")), []).append(c)
    items: list[dict[str, Any]] = []
    keys: list[str] = []
    for grp in grouped.values():
        first, ids = grp[0], [int(c["id"]) for c in grp]
        actor, rid, url = (
            _opt_str(_mapping(first.get("user")).get("login")),
            first.get("pull_request_review_id"),
            first.get("html_url"),
        )
        keys.extend(f"rc:{cid}" for cid in ids)
        if len(grp) == 1:
            path, body = _opt_str(first.get("path")), _clip(first.get("body"))
            msg = f"Inline comment by {actor or 'someone'}{f' on {path}' if path else ''}{f': {body}' if body else ''}"
            items.append(
                _item("review_comment", msg, id=ids[0], review_id=rid, actor=actor, path=path, body=body, url=url),
            )
        else:
            paths = list(dict.fromkeys(p for c in grp if (p := _opt_str(c.get("path")))))
            entries = [
                {"id": c.get("id"), "path": c.get("path"), "body": _clip(c.get("body")), "url": c.get("html_url")}
                for c in grp
            ]
            on_paths = f" on {', '.join(paths[:3])}" if paths else ""
            msg = f"{len(grp)} inline comments by {actor or 'someone'}{on_paths}"
            items.append(
                _item(
                    "review_comments",
                    msg,
                    review_id=rid,
                    actor=actor,
                    count=len(grp),
                    paths=paths,
                    comments=entries,
                    url=url,
                ),
            )
    return items, keys


async def _poll_check_runs(
    src: GitHubPRSourceSpec,
    head_sha: str,
    state: dict[str, Any],
    fetch: _FetchFn,
    *,
    seed_only: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    if seed_only or state.get("ci_head_sha") != head_sha:
        state.update(
            ci_head_sha=head_sha,
            checks_etag=None,
            checks_by_id={},
            notified_failed_checks=[],
            last_ci_overall="none",
        )
    etag = None if seed_only else _opt_str(state.get("checks_etag"))
    runs: list[Mapping[str, Any]] = []
    single_etag: str | None = None
    page = 1
    for step in range(_MAX_POLL_PAGES):
        res = await fetch(
            f"/repos/{src.repo}/commits/{head_sha}/check-runs?per_page=100&page={page}",
            etag if step == 0 else None,
        )
        if res.status_code == 304:
            state["checks_etag"] = res.etag or etag
            return [], []
        if isinstance(raw := res.data.get("check_runs") if isinstance(res.data, Mapping) else None, list):
            runs.extend(r for r in raw if isinstance(r, Mapping))
        if step == 0 and res.next_page is None:
            single_etag = res.etag
        if res.next_page is None:
            break
        page = res.next_page

    snap, failed, overall = _summarize_checks(runs)
    if seed_only:
        state.update(
            checks_etag=single_etag,
            checks_by_id=snap,
            notified_failed_checks=sorted(failed),
            last_ci_overall=overall,
        )
        return [], []
    notified = {k for k in state.get("notified_failed_checks", []) if isinstance(k, str)}
    items, keys = [], []
    for key in sorted(failed - notified):
        info = snap[key]
        msg = f"CI check '{info['name']}' finished with {info['conclusion']}"
        items.append(
            _item(
                "ci_failure",
                msg,
                id=key,
                name=info["name"],
                conclusion=info["conclusion"],
                sha=head_sha,
                url=info["url"],
            ),
        )
        keys.append(f"ci:fail:{head_sha}:{key}")
        notified.add(key)
    if overall == "passing" and state.get("last_ci_overall") != "passing":
        items.append(
            _item("ci_passing", f"All {len(snap)} CI checks passed for {head_sha[:7]}", sha=head_sha, total=len(snap)),
        )
        keys.append(f"ci:pass:{head_sha}")
    state.update(
        checks_etag=single_etag,
        checks_by_id=snap,
        notified_failed_checks=sorted(notified),
        last_ci_overall=overall,
    )
    return items, keys


def _summarize_checks(runs: Sequence[Mapping[str, Any]]) -> tuple[dict[str, dict[str, Any]], set[str], str]:
    snap, failed, pending = {}, set(), 0
    for run in runs:
        name = _opt_str(run.get("name")) or "check"
        key = str(run["id"]) if run.get("id") is not None else name
        st, conc = (_opt_str(run.get("status")) or "").lower(), (_opt_str(run.get("conclusion")) or "").lower() or None
        snap[key] = {
            "name": name,
            "status": st,
            "conclusion": conc,
            "url": _opt_str(run.get("html_url")) or _opt_str(run.get("url")),
        }
        if st != "completed":
            pending += 1
        elif conc in _CI_FAIL:
            failed.add(key)
    return snap, failed, ("none" if not snap else ("failing" if failed else ("pending" if pending else "passing")))


def _format_timeline_entry(src: GitHubPRSourceSpec, entry: Mapping[str, Any]) -> dict[str, Any] | None:
    ev = (_opt_str(entry.get("event")) or "").lower()
    if not ev or ev in _IGNORED_TIMELINE:
        return None
    u = _mapping(entry.get("user")) or _mapping(entry.get("actor"))
    actor = _opt_str(u.get("login"))
    if is_ignored_activity_actor(src, actor=actor, actor_type=_opt_str(u.get("type"))):
        return None
    body, url, eid, who = _clip(entry.get("body")), entry.get("html_url"), entry.get("id"), actor or "someone"
    sfx = f": {body}" if body else ""
    if ev == "reviewed":
        st = (_opt_str(entry.get("state")) or "").lower()
        if st == "commented" and not body:
            return None
        return _item(
            "review",
            f"Review ({st or 'submitted'}) by {who}{sfx}",
            id=eid,
            actor=actor,
            state=st,
            body=body,
            url=url,
        )
    if ev == "commented":
        return _item("comment", f"Comment by {who}{sfx}", id=eid, actor=actor, body=body, url=url)
    label = _opt_str(_mapping(entry.get("label")).get("name"))
    return _item(ev, f"{who} {ev.replace('_', ' ')}{f' ({label})' if label else sfx}", id=eid, actor=actor, url=url)


def _item(item_type: str, summary: str, **extra: object) -> dict[str, Any]:
    return {"type": item_type, **extra, "summary": summary}


def _cp(existing: GitHubPRCheckpoint | None, *, updated_at: float, **updates: object) -> GitHubPRCheckpoint:
    return (existing or GitHubPRCheckpoint(updated_at=updated_at)).model_copy(
        update={"updated_at": updated_at, **updates},
    )


def _pr_event(
    src: GitHubPRSourceSpec,
    kind: str,
    event_id: str,
    ts: float,
    cp: GitHubPRCheckpoint,
    items: list[dict[str, Any]],
    actor: str | None,
    title: str | None,
    url: str | None,
    terminal: bool,
) -> TriggerEvent:
    summaries = [str(it["summary"]) for it in items if it.get("summary")]
    data: dict[str, Any] = {
        "repo": src.repo,
        "number": src.number,
        "head_sha": cp.head_sha,
        "state": cp.pr_state,
        "merged": cp.pr_state == "merged",
        "terminal": terminal,
        "items": items,
    }
    if url:
        data["url"] = url
    return TriggerEvent(
        source_type="github_pr",
        source_id=src.repo.lower(),
        kind=kind,
        event_id=event_id,
        timestamp=ts,
        actor=actor,
        title=title or f"{src.repo}#{src.number}",
        summary="\n".join(summaries)[:2000] if summaries else None,
        data=data,
    )


def _extract_pr_summary(payload: Mapping[str, Any]) -> tuple[PRState, str | None, str | None, str | None]:
    raw = (_opt_str(payload.get("state")) or "open").lower()
    merged = payload.get("merged") is True or payload.get("merged_at") is not None
    state: PRState = "merged" if merged else ("closed" if raw == "closed" else "open")
    return (
        state,
        _opt_str(_mapping(payload.get("head")).get("sha")),
        _clip(payload.get("title")),
        _opt_str(payload.get("html_url")),
    )


def _timeline_key(entry: Mapping[str, Any]) -> str:
    ev = _opt_str(entry.get("event")) or "event"
    if entry.get("id") is not None:
        return f"{ev}:{entry['id']}"
    u = _mapping(entry.get("user")) or _mapping(entry.get("actor"))
    return f"{ev}:{entry.get('created_at') or entry.get('submitted_at') or ''}:{u.get('login') or ''}"


def _next_poll_at(poll_sec: int, now: float, auth_mode: GitHubAuthMode, remaining: int | None) -> float:
    interval = float(poll_sec) * (
        2.0 if remaining is not None and remaining < (10 if auth_mode == "anonymous" else 100) else 1.0
    )
    return now + max(1.0, interval * (1.0 + random.uniform(-0.1, 0.1)))  # noqa: S311


def _parse_remaining(headers: Mapping[str, str]) -> int | None:
    raw = headers.get("x-ratelimit-remaining")
    return int(raw) if raw is not None and raw.strip().isascii() and raw.strip().isdigit() else None


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _opt_str(value: object) -> str | None:
    return s if isinstance(value, str) and (s := value.strip()) else None


def _clip(value: object) -> str | None:
    return t[:_MAX_TEXT] if (t := _opt_str(value)) else None
