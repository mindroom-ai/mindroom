"""GitHub PR REST seeding and bounded-concurrency ETag poller for poll-backed github_pr triggers."""

from __future__ import annotations

import asyncio
import copy
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, NoReturn

import httpx

from mindroom.credentials.manager import CredentialsUnreadableError
from mindroom.github_client import (
    GitHubAuthMode,
    GitHubClientError,
    GitHubGetResult,
    github_get_json,
    resolve_owner_github_token,
)
from mindroom.logging_config import get_logger
from mindroom.oauth.providers import OAuthProviderError
from mindroom.reactive_triggers import github_pr as gh
from mindroom.reactive_triggers.models import (
    GitHubPRCheckpoint,
    GitHubPREventType,
    GitHubPRSourceSpec,
    ReactiveTriggerRecord,
)

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.reactive_triggers.models import DisabledReason
    from mindroom.reactive_triggers.store import ReactiveTriggerStore

logger = get_logger(__name__)
_MAX_SEEN_IDS, _MAX_SEED_PAGES, _MAX_POLL_PAGES, _MAX_BACKOFF_SEC = 500, 10, 3, 3600.0
_BucketKey = Literal["anonymous"] | tuple[str, GitHubAuthMode]
_FetchFn = Callable[[str, str | None], Awaitable[GitHubGetResult]]
_PollResult = tuple[list[dict[str, Any]], list[str]]
_DueEntry = tuple[ReactiveTriggerRecord, GitHubPRCheckpoint | None]
_Src = GitHubPRSourceSpec
_State = dict[str, Any]
_ChecksPages = tuple[list[Mapping[str, Any]] | None, str | None, bool]


class GitHubPRWatchError(ValueError):
    """Raised when a GitHub PR watch cannot be created or seeded."""


class _RateLimitedError(RuntimeError):
    def __init__(self, resume_at: float) -> None:
        super().__init__("GitHub rate limited")
        self.resume_at = resume_at


class _PollHTTPError(RuntimeError):
    """Raised when a GitHub REST poll call fails with an unexpected HTTP status."""


@dataclass(frozen=True, slots=True)
class SeededPRState:
    """Initial checkpoint and PR metadata captured when creating a github_pr watch."""

    checkpoint: GitHubPRCheckpoint
    auth_mode: GitHubAuthMode
    pr_state: gh.PRState = "open"
    head_sha: str | None = None
    pr_title: str | None = None
    pr_url: str | None = None
    notes: list[str] = field(default_factory=list)


def _fail_watch(msg: str, cause: Exception | None = None) -> NoReturn:
    raise GitHubPRWatchError(msg) from cause


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
            _fail_watch(f"Unable to resolve GitHub credentials for PR watch: {exc}", exc)
        notes.append("Saved GitHub credentials could not be read; skipped initial head_sha lookup for webhook watch.")
        return SeededPRState(GitHubPRCheckpoint(seeded=True, updated_at=now), "anonymous", notes=notes)

    res = await _seed_get_pr(repo, number, token, auth_mode, notes, client, webhook_only=webhook_only)
    if res is None:
        return SeededPRState(GitHubPRCheckpoint(seeded=True, updated_at=now), auth_mode, notes=notes)

    pr_state, head_sha, title, url = gh.extract_pr_summary(res.data)
    if pr_state != "open":
        _fail_watch(f"GitHub PR {repo}#{number} is already {pr_state}.")
    if webhook_only:
        cp = GitHubPRCheckpoint(head_sha=head_sha, pr_state="open", seeded=True, updated_at=now)
        return SeededPRState(cp, auth_mode, pr_state, head_sha, title, url, notes)

    state: _State = {"pr_etag": res.etag, "pr_title": title, "pr_url": url}
    src = GitHubPRSourceSpec(repo=repo, number=number, events=list(events), poll_interval_seconds=poll_interval_seconds)

    async def _fetch(path: str, etag: str | None) -> GitHubGetResult:
        try:
            got = await github_get_json(path, token=token, etag=etag, client=client)
        except (httpx.HTTPError, TimeoutError, GitHubClientError) as exc:
            _fail_watch(f"Failed to seed GitHub PR {repo}#{number} state: {exc}", exc)
        if got.status_code not in {200, 304}:
            _fail_watch(f"Failed to seed GitHub PR {repo}#{number} endpoint {path} (HTTP {got.status_code}).")
        return got

    await _collect_endpoints(src, head_sha, state, _fetch, seed_only=True)
    nxt = _next_poll_at(poll_interval_seconds, now, auth_mode, _parse_remaining(res.headers))
    return SeededPRState(_cp(head_sha, pr_state, nxt, state, now), auth_mode, pr_state, head_sha, title, url, notes)


async def _seed_get_pr(
    repo: str,
    number: int,
    token: str | None,
    auth_mode: GitHubAuthMode,
    notes: list[str],
    client: httpx.AsyncClient | None,
    *,
    webhook_only: bool,
) -> GitHubGetResult | None:
    try:
        res = await github_get_json(f"/repos/{repo}/pulls/{number}", token=token, client=client)
    except (httpx.HTTPError, TimeoutError, GitHubClientError) as exc:
        if webhook_only:
            notes.append(f"Initial PR lookup failed for {repo}#{number}; fork CI will match after next PR update.")
            return None
        _fail_watch(f"Failed to reach GitHub API for {repo}#{number}: {exc}", exc)
    if res.status_code == 200 and isinstance(res.data, dict):
        return res
    if webhook_only:
        notes.append(f"Initial PR lookup returned HTTP {res.status_code}; fork CI will match after next PR update.")
        return None
    hint = (
        " Connect GitHub for private repos." if auth_mode == "anonymous" and res.status_code in {401, 403, 404} else ""
    )
    _fail_watch(f"Failed to fetch GitHub PR {repo}#{number} (HTTP {res.status_code}).{hint}")


class GitHubPRPoller:
    """Bounded-concurrency ETag REST poller for poll-backed github_pr reactive triggers.

    Unlike webhook CI (fail-fast on failures only), polling computes a full all-green CI rollup
    across all check runs for ``head_sha`` whenever pagination is not truncated.
    """

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
        async with AsyncExitStack() as stack:
            client = (
                self._client
                if self._client is not None
                else await stack.enter_async_context(httpx.AsyncClient(timeout=20.0, follow_redirects=False))
            )

            async def _one(rule: ReactiveTriggerRecord, cp: GitHubPRCheckpoint | None) -> bool:
                async with self._semaphore:
                    return await self._poll_rule(rule, cp, cfg, client, now_ts)

            results = await asyncio.gather(*(_one(r, c) for r, c in due), return_exceptions=True)
        enqueued = _count_enqueued(due, results)
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
            logger.warning("GitHub credentials unavailable for poll", trigger_id=rule.trigger_id, error=str(exc))
            await self._record_error(rule, cp, now)
            return False

        bucket: _BucketKey = "anonymous" if auth_mode == "anonymous" else (rule.owner_user_id, auth_mode)
        if (paused := self._bucket_paused_until.get(bucket, 0.0)) > now:
            await self._upsert_cp(rule, gh.update_pr_checkpoint(cp, updated_at=now, next_poll_at=paused))
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
            await self._upsert_cp(rule, gh.update_pr_checkpoint(cp, updated_at=now, next_poll_at=exc.resume_at))
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
        src, state = rule.source, copy.deepcopy(cp.state) if cp is not None else {}
        prev_state: gh.PRState = cp.pr_state if cp is not None else "open"
        head_sha, pr_state = (cp.head_sha, prev_state) if cp is not None else (None, "open")
        seed_only = cp is None or not cp.seeded

        pr_etag = None if seed_only else gh.opt_str(state.get("pr_etag"))
        pr_res = await fetch(f"/repos/{src.repo}/pulls/{src.number}", pr_etag)
        if pr_res.status_code == 200 and isinstance(pr_res.data, dict):
            pr_state, new_sha, title, url = gh.extract_pr_summary(pr_res.data)
            head_sha = new_sha or head_sha
            state.update(pr_etag=pr_res.etag, pr_title=title, pr_url=url)
        elif pr_res.status_code == 304:
            state["pr_etag"] = pr_res.etag or state.get("pr_etag")
        else:
            msg = f"Unexpected PR payload for {src.repo}#{src.number}"
            raise _PollHTTPError(msg)

        ci_sha = head_sha if pr_state == "open" else None
        items, keys = await _collect_endpoints(src, ci_sha, state, fetch, seed_only=seed_only)
        was_open = not seed_only and prev_state == "open"
        merged_hit = was_open and pr_state == "merged" and "merge" in src.events
        closed_hit = was_open and pr_state == "closed" and "close" in src.events
        if merged_hit or closed_hit:
            term_type = "merged" if merged_hit else "closed"
            term_msg = f"PR #{src.number} merged" if merged_hit else f"PR #{src.number} closed without merge"
            items.append(gh.make_pr_item(term_type, term_msg, url=state.get("pr_url")))
            keys.append(f"pr:{term_type}:{head_sha or ''}")

        nxt = _next_poll_at(src.poll_interval_seconds, now, auth_mode, min_rem[0])
        new_cp, terminal = _cp(head_sha, pr_state, nxt, state, now), pr_state in {"closed", "merged"}
        dkey, ev = None, None
        disable: DisabledReason | None = None
        if seed_only or not items:
            if terminal and src.auto_disable_on_close and (seed_only or prev_state == "open"):
                disable = "pr_closed"
        else:
            kind = "github_pr.merged" if merged_hit else ("github_pr.closed" if closed_hit else "github_pr.update")
            dkey, ev = gh.build_poll_pr_event(rule, kind, new_cp, state, items, keys, now)
        commit = self._store.update_github_pr_checkpoint_and_enqueue
        return await asyncio.to_thread(
            commit,
            rule.trigger_id,
            new_cp,
            dedupe_key=dkey,
            event=ev,
            disable_reason=disable,
            expected_rule_uid=rule.rule_uid,
            now=now,
        )

    async def _upsert_cp(self, rule: ReactiveTriggerRecord, cp: GitHubPRCheckpoint) -> None:
        upsert = self._store.upsert_github_pr_checkpoint
        await asyncio.to_thread(upsert, rule.trigger_id, cp, expected_rule_uid=rule.rule_uid)

    async def _record_error(self, rule: ReactiveTriggerRecord, cp: GitHubPRCheckpoint | None, now: float) -> None:
        assert isinstance(rule.source, GitHubPRSourceSpec)
        errs = (cp.consecutive_errors if cp is not None else 0) + 1
        backoff = min(_MAX_BACKOFF_SEC, float(rule.source.poll_interval_seconds) * (2 ** min(errs, 5)))
        next_cp = gh.update_pr_checkpoint(cp, updated_at=now, consecutive_errors=errs, next_poll_at=now + backoff)
        await self._upsert_cp(rule, next_cp)


def _cp(sha: str | None, state: gh.PRState, nxt: float, st: _State, now: float) -> GitHubPRCheckpoint:
    return GitHubPRCheckpoint(head_sha=sha, pr_state=state, seeded=True, next_poll_at=nxt, state=st, updated_at=now)


def _count_enqueued(due: Sequence[_DueEntry], results: Sequence[bool | BaseException]) -> int:
    enqueued = 0
    for (rule, _), outcome in zip(due, results, strict=True):
        if isinstance(outcome, Exception):
            logger.warning("Unexpected github_pr poll error", trigger_id=rule.trigger_id, error=str(outcome))
        elif isinstance(outcome, BaseException):
            raise outcome
        elif outcome:
            enqueued += 1
    return enqueued


async def _collect_endpoints(
    src: _Src,
    sha: str | None,
    state: _State,
    fetch: _FetchFn,
    *,
    seed_only: bool,
) -> _PollResult:
    items, keys = [], []
    if "activity" in src.events:
        for fn in (_poll_timeline, _poll_review_comments):
            sub_items, sub_keys = await fn(src, state, fetch, seed_only=seed_only)
            items.extend(sub_items)
            keys.extend(sub_keys)
    if "ci" in src.events and sha:
        ci_items, ci_keys = await _poll_check_runs(src, sha, state, fetch, seed_only=seed_only)
        items.extend(ci_items)
        keys.extend(ci_keys)
    return items, keys


async def _poll_timeline(src: GitHubPRSourceSpec, state: _State, fetch: _FetchFn, *, seed_only: bool) -> _PollResult:
    suppress = seed_only or bool(state.get("seed_tail_pending"))
    page = 1 if seed_only else max(1, int(state.get("timeline_page") or 1))
    seen = [] if seed_only else [k for k in state.get("seen_timeline_ids", []) if isinstance(k, str)]
    seen_set, items, keys, max_pages = set(seen), [], [], _MAX_SEED_PAGES if seed_only else _MAX_POLL_PAGES
    for step in range(max_pages):
        etag = None if seed_only or step > 0 else gh.opt_str(state.get("timeline_etag"))
        res = await fetch(f"/repos/{src.repo}/issues/{src.number}/timeline?per_page=100&page={page}", etag)
        if res.status_code == 304:
            state["timeline_etag"] = res.etag or state.get("timeline_etag")
            break
        entries = res.data if isinstance(res.data, list) else []
        for entry in entries:
            if not isinstance(entry, Mapping) or (key := gh.timeline_key(entry)) in seen_set:
                continue
            seen_set.add(key)
            seen.append(key)
            if not suppress and (fmt := gh.format_timeline_entry(src, entry)) is not None:
                items.append(fmt)
                keys.append(f"tl:{key}")
        next_p = res.next_page or (page + 1 if len(entries) >= 100 else None)
        if next_p is None:
            state.update(timeline_page=page, timeline_etag=res.etag, seed_tail_pending=False)
            break
        if step + 1 >= max_pages:
            state.update(timeline_page=next_p, timeline_etag=None, seed_tail_pending=suppress)
            break
        page = next_p
    state["seen_timeline_ids"] = seen[-_MAX_SEEN_IDS:]
    return items, keys


async def _poll_review_comments(src: _Src, state: _State, fetch: _FetchFn, *, seed_only: bool) -> _PollResult:
    prev_max = 0 if seed_only else int(state.get("max_review_comment_id") or 0)
    page1_etag = None if seed_only else gh.opt_str(state.get("review_comments_etag"))
    max_id, raw_new, seen_ids, page = prev_max, [], set(), 1
    for step in range(1 if seed_only else _MAX_POLL_PAGES):
        q = f"sort=created&direction=desc&per_page=100{f'&page={page}' if page > 1 else ''}"
        res = await fetch(f"/repos/{src.repo}/pulls/{src.number}/comments?{q}", page1_etag if step == 0 else None)
        if res.status_code == 304:
            state["review_comments_etag"] = res.etag or page1_etag
            return [], []
        if step == 0:
            page1_etag = res.etag
        comments = res.data if isinstance(res.data, list) else []
        hit_old = False
        for c in comments:
            cid = c.get("id") if isinstance(c, Mapping) else None
            if not isinstance(cid, int) or isinstance(cid, bool):
                continue
            if cid <= prev_max:
                hit_old = True
                continue
            max_id = max(max_id, cid)
            if not seed_only and cid not in seen_ids:
                seen_ids.add(cid)
                raw_new.append(c)
        if seed_only or hit_old or not comments or res.next_page is None:
            break
        page = res.next_page
    state.update(max_review_comment_id=max_id, review_comments_etag=page1_etag)
    if not raw_new:
        return [], []
    raw_new.reverse()
    return gh.group_review_comments(src, raw_new)


async def _fetch_check_runs(repo: str, sha: str, etag: str | None, fetch: _FetchFn) -> _ChecksPages:
    runs: list[Mapping[str, Any]] = []
    single_etag, page, truncated = None, 1, False
    for step in range(_MAX_POLL_PAGES):
        path = f"/repos/{repo}/commits/{sha}/check-runs?per_page=100&page={page}"
        res = await fetch(path, etag if step == 0 else None)
        if res.status_code == 304:
            return None, res.etag or etag, False
        if isinstance(raw := res.data.get("check_runs") if isinstance(res.data, Mapping) else None, list):
            runs.extend(r for r in raw if isinstance(r, Mapping))
        if step == 0 and res.next_page is None:
            single_etag = res.etag
        if res.next_page is None:
            break
        if step + 1 >= _MAX_POLL_PAGES:
            truncated = True
            break
        page = res.next_page
    return runs, single_etag, truncated


async def _poll_check_runs(src: _Src, sha: str, state: _State, fetch: _FetchFn, *, seed_only: bool) -> _PollResult:
    if seed_only or state.get("ci_head_sha") != sha:
        state.update(ci_head_sha=sha, checks_etag=None, notified_failed_checks=[], last_ci_overall="none")
    etag = None if seed_only else gh.opt_str(state.get("checks_etag"))
    runs, single_etag, truncated = await _fetch_check_runs(src.repo, sha, etag, fetch)
    if runs is None:
        state["checks_etag"] = single_etag
        return [], []

    s = gh.summarize_check_runs(runs)
    overall = s.overall(truncated=truncated)
    if seed_only:
        state.update(checks_etag=single_etag, notified_failed_checks=sorted(s.failed), last_ci_overall=overall)
        return [], []
    notified = {k for k in state.get("notified_failed_checks", []) if isinstance(k, str)}
    items, keys = [], []
    for key in sorted(set(s.failed) - notified):
        info = s.failed[key]
        msg = f"CI check '{info['name']}' finished with {info['conclusion']}"
        extra = {"id": key, "name": info["name"], "conclusion": info["conclusion"], "sha": sha, "url": info["url"]}
        items.append(gh.make_pr_item("ci_failure", msg, **extra))
        keys.append(f"ci:fail:{sha}:{key}")
        notified.add(key)
    if overall == "passing" and state.get("last_ci_overall") != "passing":
        msg = f"All {s.total} CI checks passed for {sha[:7]}"
        items.append(gh.make_pr_item("ci_passing", msg, sha=sha, total=s.total))
        keys.append(f"ci:pass:{sha}")
    state.update(checks_etag=single_etag, notified_failed_checks=sorted(notified), last_ci_overall=overall)
    return items, keys


def _next_poll_at(poll_sec: int, now: float, auth_mode: GitHubAuthMode, remaining: int | None) -> float:
    low_budget = remaining is not None and remaining < (10 if auth_mode == "anonymous" else 100)
    return now + max(1.0, float(poll_sec) * (2.0 if low_budget else 1.0) * (1.0 + random.uniform(-0.1, 0.1)))  # noqa: S311


def _parse_remaining(headers: Mapping[str, str]) -> int | None:
    raw = headers.get("x-ratelimit-remaining")
    return int(raw) if raw is not None and raw.strip().isascii() and raw.strip().isdigit() else None
