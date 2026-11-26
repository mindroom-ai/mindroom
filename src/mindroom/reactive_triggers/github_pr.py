"""Unified GitHub PR watcher: webhook-nudge routing, access checks, seeding, and ETag REST poller."""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, NoReturn

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
CI_FAIL_CONCLUSIONS = frozenset({"failure", "timed_out", "action_required"})
CI_PASS_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
_IGN_A = "subscribed unsubscribed mentioned deployed head_ref_deleted head_ref_force_pushed committed"
_IGNORED_TL = frozenset(f"{_IGN_A} base_ref_changed automatic_base_change_succeeded closed merged".split())
_LIMIT, _MAX_PAGES, _MAX_BACKOFF = 500, 3, 3600.0
_API_ERRS = (httpx.HTTPError, TimeoutError, ghc.GitHubClientError)
type _Src = rm.GitHubPRSourceSpec
type _Cp = rm.GitHubPRCheckpoint
type _Rule = rm.ReactiveTriggerRecord
type _Obj = dict[str, Any]
type _Fetch = Callable[[str, str | None], Awaitable[ghc.GitHubGetResult]]
type _Pairs = list[tuple[_Obj, str]]
type _Sync = tuple[Literal["open", "closed", "merged"], str | None, _Obj, _Pairs]
type _Http = httpx.AsyncClient


class GitHubPRWatchError(ValueError):
    """Raised when a GitHub PR watch cannot be created or seeded."""


class _RateLimitedError(RuntimeError): ...


class SeededPRState(NamedTuple):
    """Initial checkpoint and PR metadata captured when creating a github_pr watch."""

    checkpoint: _Cp
    auth_mode: ghc.GitHubAuthMode
    pr_state: Literal["open", "closed", "merged"] = "open"
    head_sha: str | None = None


def is_ignored_activity_actor(source: _Src, *, actor: str | None, actor_type: str | None = None) -> bool:
    """Return whether an activity actor should be ignored per ignore_actors / ignore_bots."""
    low = (actor or "").lower()
    listed = bool(low) and any(a.lower() == low for a in source.ignore_actors)
    return listed or (source.ignore_bots and (low.endswith("[bot]") or (actor_type or "").lower() == "bot"))


def find_covering_github_webhook_source(owner: str, repo: str, config: Config, paths: RuntimePaths) -> str | None:
    """Return the first authorized GitHub webhook source covering ``repo``, or None."""
    low = repo.lower()
    for sid, src in sorted(config.external_trigger_policy.event_sources.items()):
        ok = isinstance(src, GitHubEventSourceConfig) and any(r.lower() == low for r in src.allowed_repos)
        if ok and is_source_subscriber_allowed(src, owner, config, paths):
            return sid
    return None


def check_github_pr_source_access(
    *,
    owner_user_id: str,
    source_spec: _Src,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Verify current policy and webhook source grants for a github_pr rule."""
    pol = config.external_trigger_policy
    if source_spec.webhook_source_id is None:
        return None if pol.github_pr_polling_enabled else "github_pr_polling_disabled"
    if not isinstance(gh := pol.event_sources.get(source_spec.webhook_source_id), GitHubEventSourceConfig):
        return "source_removed"
    if not is_source_subscriber_allowed(gh, owner_user_id, config, runtime_paths):
        return "source_subscriber_revoked"
    return None if any(r.lower() == source_spec.repo.lower() for r in gh.allowed_repos) else "source_grant_revoked"


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
    ids = [
        r.trigger_id
        for r in store.list_triggers(include_disabled=False, source_type="github_pr", source_id=repo.lower())
        if (r.expires_at is None or r.expires_at > now)
        and isinstance(r.source, rm.GitHubPRSourceSpec)
        and r.source.webhook_source_id == source_id
        and is_source_subscriber_allowed(source, r.owner_user_id, config, runtime_paths)
    ]
    return store.nudge_github_pr_checkpoints(ids, now=now)


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
    client: _Http | None = None,
) -> SeededPRState:
    """Validate an open PR and seed initial checkpoint state so baseline history does not replay."""
    try:
        token, mode = await ghc.resolve_owner_github_token(owner_user_id, config, runtime_paths)
    except (CredentialsUnreadableError, OAuthProviderError) as exc:
        _fail(f"Unable to resolve GitHub credentials for PR watch: {exc}", exc)
    async with AsyncExitStack() as stack:
        cm = httpx.AsyncClient(timeout=20.0, follow_redirects=False)
        cli = client if client is not None else await stack.enter_async_context(cm)

        async def _fetch(path: str, etag: str | None) -> ghc.GitHubGetResult:
            got = await ghc.github_get_json(path, token=token, etag=etag, client=cli)
            if got.status_code in {200, 304}:
                return got
            priv = mode == "anonymous" and got.status_code in {401, 403, 404}
            hint = " Connect GitHub or save a GitHub token for private repos." if priv else ""
            _fail(f"Failed to fetch GitHub PR {repo}#{number} (HTTP {got.status_code}).{hint}")

        src = rm.GitHubPRSourceSpec(repo=repo, number=number, events=list(events), webhook_source_id=webhook_source_id)
        try:
            pr_st, sha, st, _ = await _sync_pr(src, None, _fetch, require_open=True)
        except _API_ERRS as exc:
            _fail(f"Failed to reach GitHub API for {repo}#{number}: {exc}", exc)
    nxt = None if webhook_source_id is not None else now + float(poll_interval_seconds)
    cp = rm.GitHubPRCheckpoint(head_sha=sha, pr_state=pr_st, seeded=True, next_poll_at=nxt, state=st, updated_at=now)
    return SeededPRState(cp, mode, pr_st, sha)


class GitHubPRPoller:
    """Unified bounded-concurrency ETag REST poller for poll-backed and webhook-nudged github_pr watches."""

    def __init__(
        self,
        *,
        store: ReactiveTriggerStore,
        config_provider: Callable[[], Config],
        runtime_paths: RuntimePaths,
        on_enqueued: Callable[[], None] | None = None,
        client: _Http | None = None,
        max_concurrency: int = 4,
    ) -> None:
        self._store, self._cfg, self._paths, self._on_enqueued = store, config_provider, runtime_paths, on_enqueued
        self._client, self._sem = client, asyncio.Semaphore(max(1, max_concurrency))
        self._paused: dict[object, float] = {}

    async def poll_due_once(self, *, now: float | None = None) -> int:
        """Poll all due (scheduled or webhook-nudged) github_pr triggers once and return enqueued count."""
        cfg, ts = self._cfg(), time.time() if now is None else now
        if not (cfg.external_trigger_policy.enabled and cfg.external_trigger_policy.reactive_enabled):
            return 0
        if not (due := await asyncio.to_thread(self._store.list_due_github_pr_triggers, now=ts, limit=50)):
            return 0
        async with AsyncExitStack() as stack:
            cm = httpx.AsyncClient(timeout=20.0, follow_redirects=False)
            cli = self._client if self._client is not None else await stack.enter_async_context(cm)
            res = await asyncio.gather(*(self._poll_rule(r, c, cfg, cli, ts) for r, c in due), return_exceptions=True)
        for (rule, _), out in zip(due, res, strict=True):
            if isinstance(out, Exception):
                logger.warning("Unexpected github_pr poll error", trigger_id=rule.trigger_id, error=str(out))
            elif isinstance(out, BaseException):
                raise out
        if (enq := sum(out is True for out in res)) and self._on_enqueued:
            self._on_enqueued()
        return enq

    async def _poll_rule(self, rule: _Rule, cp: _Cp | None, cfg: Config, http: _Http, now: float) -> bool:
        async with self._sem:
            assert isinstance(rule.source, rm.GitHubPRSourceSpec)
            src, chk = rule.source, check_github_pr_source_access
            if chk(owner_user_id=rule.owner_user_id, source_spec=src, config=cfg, runtime_paths=self._paths):
                if src.webhook_source_id and cp:
                    await self._save(rule, cp, now, next_poll_at=None)
                return False
            try:
                return await self._run_poll(rule, src, cp, cfg, http, now)
            except _RateLimitedError as exc:
                await self._save(rule, cp, now, next_poll_at=float(exc.args[0]))
            except (CredentialsUnreadableError, OAuthProviderError, *_API_ERRS) as exc:
                logger.warning("GitHub PR poll failed", trigger_id=rule.trigger_id, repo=src.repo, error=str(exc))
                errs = (cp.consecutive_errors if cp else 0) + 1
                nxt = now + min(_MAX_BACKOFF, float(src.poll_interval_seconds) * (2 ** min(errs, 5)))
                await self._save(rule, cp, now, consecutive_errors=errs, next_poll_at=nxt)
            return False

    async def _run_poll(self, rule: _Rule, src: _Src, cp: _Cp | None, cfg: Config, http: _Http, now: float) -> bool:
        tok, mode = await ghc.resolve_owner_github_token(rule.owner_user_id, cfg, self._paths)
        bkt: object = "anonymous" if mode == "anonymous" else (rule.owner_user_id, mode)
        if (paused := self._paused.get(bkt, 0.0)) > now:
            raise _RateLimitedError(paused)
        rem: list[int] = []

        async def _fetch(path: str, etag: str | None) -> ghc.GitHubGetResult:
            res = await ghc.github_get_json(path, token=tok, etag=etag, client=http)
            if (raw := res.headers.get("x-ratelimit-remaining", "").strip()).isascii() and raw.isdigit():
                rem.append(int(raw))
            if res.rate_limited:
                resume = now + min(max(res.retry_after_seconds or 60.0, 1.0), _MAX_BACKOFF)
                self._paused[bkt] = max(self._paused.get(bkt, 0.0), resume)
                raise _RateLimitedError(resume)
            if res.status_code not in {200, 304}:
                msg = f"GitHub API {path} returned HTTP {res.status_code}"
                raise ghc.GitHubClientError(msg)
            return res

        seed, prev_st = cp is None or not cp.seeded, "open" if cp is None or not cp.seeded else cp.pr_state
        pr_st, sha, s, pairs = await _sync_pr(src, None if seed else cp, _fetch)
        mult = 2.0 if rem and min(rem) < (10 if mode == "anonymous" else 100) else 1.0
        delay = max(1.0, src.poll_interval_seconds * mult * random.uniform(0.9, 1.1))  # noqa: S311
        nxt = None if src.webhook_source_id is not None else now + delay
        nc = rm.GitHubPRCheckpoint(head_sha=sha, pr_state=pr_st, seeded=True, next_poll_at=nxt, state=s, updated_at=now)
        ev = _build_event(rule.trigger_id, src, nc, pairs, now) if (not seed and pairs) else None
        close = not ev and pr_st != "open" and src.auto_disable_on_close and (seed or prev_st == "open")
        return await asyncio.to_thread(
            self._store.update_github_pr_checkpoint_and_enqueue,
            rule.trigger_id,
            nc,
            event=ev,
            disable_reason="pr_closed" if close else None,
            expected_rule_uid=rule.rule_uid,
            poll_started_at=cp.next_poll_at if cp is not None and cp.next_poll_at is not None else now,
        )

    async def _save(self, rule: _Rule, cp: _Cp | None, now: float, **upd: object) -> None:
        nxt = (cp or rm.GitHubPRCheckpoint(updated_at=now)).model_copy(update={"updated_at": now, **upd})
        fn = self._store.upsert_github_pr_checkpoint
        await asyncio.to_thread(fn, rule.trigger_id, nxt, expected_rule_uid=rule.rule_uid)


async def _sync_pr(src: _Src, cp: _Cp | None, fetch: _Fetch, *, require_open: bool = False) -> _Sync:
    base = cp if cp is not None and cp.seeded else None
    st, sha, prev_st = (dict(base.state), base.head_sha, base.pr_state) if base is not None else ({}, None, "open")
    seed, pr_st = base is None, prev_st
    res = await fetch(f"/repos/{src.repo}/pulls/{src.number}", None if seed else _s(st.get("pr_etag")))
    if res.status_code == 200 and isinstance(d := res.data, dict):
        merged = d.get("merged") is True or d.get("merged_at") is not None
        pr_st = "merged" if merged else ("closed" if (_s(d.get("state")) or "").lower() == "closed" else "open")
        sha = _s(_d(d.get("head")).get("sha")) or sha
        st.update(pr_etag=res.etag, pr_title=_s(d.get("title"), _LIMIT), pr_url=_s(d.get("html_url")))
    elif res.status_code == 304:
        st["pr_etag"] = res.etag
    else:
        msg = f"Unexpected PR payload for {src.repo}#{src.number}"
        raise ghc.GitHubClientError(msg)
    if require_open and pr_st != "open":
        _fail(f"GitHub PR {src.repo}#{src.number} is already {pr_st}.")
    pairs: _Pairs = []
    for fn in (_poll_timeline, _poll_review_comments) if "activity" in src.events else ():
        await fn(src, st, fetch, pairs, seed_only=seed)
    if "ci" in src.events and pr_st == "open" and sha:
        await _poll_check_runs(src, sha, st, fetch, pairs, seed_only=seed)
    if not seed and prev_st == "open" and {"merged": "merge", "closed": "close"}.get(pr_st) in src.events:
        msg = f"PR #{src.number} {'merged' if pr_st == 'merged' else 'closed without merge'}"
        pairs.append(({"type": pr_st, "url": st.get("pr_url"), "summary": msg}, f"pr:{pr_st}:{sha or ''}"))
    return pr_st, sha, st, pairs


async def _poll_timeline(src: _Src, st: _Obj, fetch: _Fetch, out: _Pairs, *, seed_only: bool) -> None:
    base = f"/repos/{src.repo}/issues/{src.number}/timeline?per_page=100&page="
    page = 1 if seed_only else max(1, int(st.get("timeline_page") or 1))
    seen = {} if seed_only else dict.fromkeys(k for k in st.get("seen_timeline_ids", []) if isinstance(k, str))
    for step in range(1 if seed_only else _MAX_PAGES):
        res = await fetch(f"{base}{page}", None if (seed_only or step) else _s(st.get("timeline_etag")))
        if seed_only and res.last_page and res.last_page > 1:
            page, res = res.last_page, await fetch(f"{base}{res.last_page}", None)
        if res.status_code == 304:
            st["timeline_etag"] = res.etag
            return
        entries = [e for e in res.data if isinstance(e, Mapping)] if isinstance(res.data, list) else []
        for e in entries:
            ev, u = (_s(e.get("event")) or "").lower(), _d(e.get("user")) or _d(e.get("actor"))
            ts = e.get("created_at") or e.get("submitted_at") or ""
            key = f"{ev}:{e['id']}" if e.get("id") is not None else f"{ev}:{ts}:{u.get('login') or ''}"
            if key not in seen:
                seen[key] = None
                if not seed_only and (fmt := _format_timeline(src, e, ev, u)) is not None:
                    out.append((fmt, f"tl:{key}"))
        nxt = None if seed_only else (res.next_page or (page + 1 if len(entries) >= 100 else None))
        if nxt is None or step + 1 >= _MAX_PAGES:
            st.update(timeline_page=page if nxt is None else nxt, timeline_etag=res.etag if nxt is None else None)
            break
        page = nxt
    st["seen_timeline_ids"] = list(seen)[-_LIMIT:]


async def _poll_review_comments(src: _Src, st: _Obj, fetch: _Fetch, out: _Pairs, *, seed_only: bool) -> None:
    prev = 0 if seed_only else int(st.get("max_review_comment_id") or 0)
    raw_new: dict[int, tuple[object, str | None, _Obj]] = {}
    max_id, etag, page = prev, None if seed_only else _s(st.get("review_comments_etag")), 1
    for step in range(1 if seed_only else _MAX_PAGES):
        q = f"sort=created&direction=desc&per_page=100{f'&page={page}' if page > 1 else ''}"
        res = await fetch(f"/repos/{src.repo}/pulls/{src.number}/comments?{q}", None if step else etag)
        if res.status_code == 304:
            st["review_comments_etag"] = res.etag
            return
        comments = [c for c in res.data if isinstance(c, Mapping)] if isinstance(res.data, list) else []
        etag, hit_old = res.etag if step == 0 else etag, False
        for c in comments:
            if not isinstance(cid := c.get("id"), int) or isinstance(cid, bool):
                continue
            if cid <= prev:
                hit_old = True
            else:
                max_id, u = max(max_id, cid), _d(c.get("user"))
                actor = _s(u.get("login"))
                if not seed_only and not is_ignored_activity_actor(src, actor=actor, actor_type=_s(u.get("type"))):
                    e = {"id": cid, "path": c.get("path"), "body": _s(c.get("body"), _LIMIT), "url": c.get("html_url")}
                    raw_new.setdefault(cid, (c.get("pull_request_review_id"), actor, e))
        if seed_only or hit_old or not comments or res.next_page is None:
            break
        page = res.next_page
    st.update(max_review_comment_id=max_id, review_comments_etag=etag)
    _group_review_comments(list(reversed(raw_new.values())), out)


async def _poll_check_runs(s: _Src, sha: str, st: _Obj, fetch: _Fetch, out: _Pairs, *, seed_only: bool) -> None:
    if seed_only or st.get("ci_head_sha") != sha:
        st.update(ci_head_sha=sha, checks_etag=None, notified_failed_checks=[], last_ci_overall="none")
    runs: dict[str, Mapping[str, Any]] = {}
    etag, single_etag, page, trunc = None if seed_only else _s(st.get("checks_etag")), None, 1, False
    for step in range(_MAX_PAGES):
        res = await fetch(f"/repos/{s.repo}/commits/{sha}/check-runs?per_page=100&page={page}", None if step else etag)
        if res.status_code == 304:
            st["checks_etag"] = res.etag
            return
        for r in raw if isinstance(raw := _d(res.data).get("check_runs"), list) else ():
            if isinstance(r, Mapping):
                runs.setdefault(str(r["id"]) if r.get("id") is not None else (_s(r.get("name")) or "check"), r)
        single_etag = res.etag if step == 0 and res.next_page is None else single_etag
        if res.next_page is None or step + 1 >= _MAX_PAGES:
            trunc = res.next_page is not None
            break
        page = res.next_page
    failed, overall = _tally_checks(runs, truncated=trunc)
    notified = set(failed) if seed_only else {k for k in st.get("notified_failed_checks", []) if isinstance(k, str)}
    for k in sorted(set(failed) - notified):
        msg = f"CI check '{failed[k]['name']}' finished with {failed[k]['conclusion']}"
        out.append(({"type": "ci_failure", "id": k, "sha": sha, "summary": msg, **failed[k]}, f"ci:fail:{sha}:{k}"))
    if not seed_only and overall == "passing" and st.get("last_ci_overall") != "passing":
        msg = f"All {len(runs)} CI checks passed for {sha[:7]}"
        out.append(({"type": "ci_passing", "sha": sha, "total": len(runs), "summary": msg}, f"ci:pass:{sha}"))
    st.update(checks_etag=single_etag, notified_failed_checks=sorted(notified | set(failed)), last_ci_overall=overall)


def _tally_checks(runs: dict[str, Mapping[str, Any]], *, truncated: bool) -> tuple[dict[str, _Obj], str]:
    failed, pending, other = dict[str, _Obj](), False, False
    for k, r in runs.items():
        st, c = (_s(r.get("status")) or "").lower(), (_s(r.get("conclusion")) or "").lower()
        if st != "completed":
            pending = True
        elif c in CI_FAIL_CONCLUSIONS:
            u = _s(r.get("html_url") or r.get("url"))
            failed[k] = {"name": _s(r.get("name")) or "check", "conclusion": c, "url": u}
        elif c not in CI_PASS_CONCLUSIONS:
            other = True
    if not runs or failed:
        return failed, "failing" if failed else "none"
    return failed, "pending" if (pending or truncated) else ("non_passing" if other else "passing")


def _format_timeline(src: _Src, e: Mapping[str, Any], ev: str, u: Mapping[str, Any]) -> _Obj | None:
    actor = _s(u.get("login"))
    if not ev or ev in _IGNORED_TL or is_ignored_activity_actor(src, actor=actor, actor_type=_s(u.get("type"))):
        return None
    body, who = _s(e.get("body"), _LIMIT), actor or "someone"
    base, sfx = {"id": e.get("id"), "actor": actor, "url": e.get("html_url")}, f": {body}" if body else ""
    if ev == "reviewed":
        if (st := (_s(e.get("state")) or "").lower()) == "commented" and not body:
            return None
        msg = f"Review ({st or 'submitted'}) by {who}{sfx}"
        return {**base, "type": "review", "state": st, "body": body, "summary": msg}
    if ev == "commented":
        return {**base, "type": "comment", "body": body, "summary": f"Comment by {who}{sfx}"}
    lbl = _s(_d(e.get("label")).get("name"))
    return {**base, "type": ev, "summary": f"{who} {ev.replace('_', ' ')}{f' ({lbl})' if lbl else sfx}"}


def _group_review_comments(rows: list[tuple[object, str | None, _Obj]], out: _Pairs) -> None:
    grouped: dict[object, list[tuple[object, str | None, _Obj]]] = {}
    for row in rows:
        grouped.setdefault(("r", row[0]) if row[0] is not None else ("c", row[2]["id"]), []).append(row)
    for grp in grouped.values():
        rid, actor, es = grp[0][0], grp[0][1], [e for _, _, e in grp]
        who, paths = actor or "someone", list(dict.fromkeys(p for e in es if (p := _s(e["path"]))))
        p_txt, key = f" on {', '.join(paths[:3])}" if paths else "", "|".join(sorted(f"rc:{e['id']}" for e in es))
        if len(grp) == 1:
            msg = f"Inline comment by {who}{p_txt}{f': {es[0]["body"]}' if es[0]['body'] else ''}"
            item: _Obj = {"type": "review_comment", **es[0], "summary": msg}
        else:
            item = {"type": "review_comments", "count": len(grp), "paths": paths, "comments": es, "url": es[0]["url"]}
            item["summary"] = f"{len(grp)} inline comments by {who}{p_txt}"
        out.append(({"review_id": rid, "actor": actor, **item}, key))


def _build_event(tid: str, src: _Src, cp: _Cp, pairs: _Pairs, now: float) -> rm.TriggerEvent:
    items, keys = [it for it, _ in pairs], sorted(p for _, k in pairs for p in k.split("|"))
    types = {it.get("type") for it in items}
    kind = "github_pr.merged" if "merged" in types else "github_pr.closed" if "closed" in types else "github_pr.update"
    dkey = f"ghpr:{hashlib.sha256(f'{tid}:{"|".join(keys)}'.encode()).hexdigest()[:32]}"
    sums = [str(it["summary"]) for it in items if it.get("summary")]
    data: _Obj = {"repo": src.repo, "number": src.number, "head_sha": cp.head_sha, "state": cp.pr_state}
    data |= {"merged": cp.pr_state == "merged", "terminal": cp.pr_state != "open", "items": items}
    data |= {"url": u} if (u := _s(cp.state.get("pr_url"))) else {}
    ev: _Obj = {"source_type": "github_pr", "source_id": src.repo.lower(), "kind": kind, "event_id": dkey, "data": data}
    actor = next((a for it in items if (a := _s(it.get("actor")))), None)
    title = _s(cp.state.get("pr_title")) or f"{src.repo}#{src.number}"
    ev |= {"timestamp": now, "actor": actor, "title": title, "summary": "\n".join(sums)[:2000] if sums else None}
    return rm.TriggerEvent.model_validate(ev)


def _fail(msg: str, cause: Exception | None = None) -> NoReturn:
    raise GitHubPRWatchError(msg) from cause


def _d(v: object) -> Mapping[str, Any]:
    return v if isinstance(v, Mapping) else {}


def _s(v: object, limit: int | None = None) -> str | None:
    return (t[:limit] if limit else t) if isinstance(v, str) and (t := v.strip()) else None
