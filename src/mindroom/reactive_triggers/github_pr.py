"""Native GitHub PR watcher: webhook selection/matching, access checks, and pure payload formatters."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from mindroom.config.external_trigger_policy import GitHubEventSourceConfig
from mindroom.external_triggers.policy import is_source_subscriber_allowed
from mindroom.reactive_triggers.models import (
    GitHubPRCheckpoint,
    GitHubPREventType,
    GitHubPRSourceSpec,
    ReactiveTriggerRecord,
    TriggerEvent,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

# Excludes "cancelled" so cancel-in-progress workflows on new pushes do not wake as CI failures or all-green passes.
CI_FAIL_CONCLUSIONS = frozenset({"failure", "timed_out", "action_required"})
CI_PASS_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
_DRY_RUN_CATEGORY_BY_KIND: Mapping[str, GitHubPREventType] = {
    "activity": "activity",
    "ci": "ci",
    "merge": "merge",
    "close": "close",
    "github_pr.merged": "merge",
    "github_pr.closed": "close",
}
_DEFAULT_GITHUB_PR_WEBHOOK_EVENTS: frozenset[str] = frozenset(
    {"pull_request", "issue_comment", "pull_request_review", "pull_request_review_comment", "check_run", "check_suite"},
)
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
_MAX_TEXT = 500
PRState = Literal["open", "closed", "merged"]


@dataclass(frozen=True, slots=True)
class WebhookPRMatch:
    """Result of matching one normalized GitHub webhook event against a github_pr rule."""

    matched: bool
    checkpoint: GitHubPRCheckpoint | None = None
    event: TriggerEvent | None = None
    dedupe_key: str | None = None
    disable_immediately: bool = False


def is_ignored_activity_actor(source: GitHubPRSourceSpec, *, actor: str | None, actor_type: str | None = None) -> bool:
    """Return whether an activity actor should be ignored per ignore_actors / ignore_bots."""
    if actor:
        low = actor.lower()
        if any(a.lower() == low for a in source.ignore_actors) or (source.ignore_bots and low.endswith("[bot]")):
            return True
    return bool(source.ignore_bots and actor_type and actor_type.lower() == "bot")


def check_github_pr_source_access(
    *,
    owner_user_id: str,
    source_spec: GitHubPRSourceSpec,
    event: TriggerEvent | None,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Verify current policy and webhook source grants for a github_pr rule (and optional event)."""
    if source_spec.webhook_source_id is None:
        return None if config.external_trigger_policy.github_pr_polling_enabled else "github_pr_polling_disabled"
    gh_source = config.external_trigger_policy.event_sources.get(source_spec.webhook_source_id)
    if not isinstance(gh_source, GitHubEventSourceConfig):
        return "source_removed"
    if not is_source_subscriber_allowed(gh_source, owner_user_id, config, runtime_paths):
        return "source_subscriber_revoked"
    repo_lower = source_spec.repo.lower()
    github_event = event.data.get("github_event") if event is not None else None
    repo_allowed = any(allowed.lower() == repo_lower for allowed in gh_source.allowed_repos)
    event_allowed = not isinstance(github_event, str) or github_event in gh_source.allowed_events
    return None if (repo_allowed and event_allowed) else "source_grant_revoked"


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
            notes.append("Webhook CI fires fail-fast on failures only; all-green rollups require polling.")
            if "check_run" not in allowed:
                notes.append(f"Source {sid!r} lacks 'check_run'; CI failures are reported at check-suite level only.")
        return sid, notes
    return None, fallback


def match_github_webhook_to_pr_rule(
    rule: ReactiveTriggerRecord,
    checkpoint: GitHubPRCheckpoint | None,
    event: TriggerEvent,
    *,
    allowed_events: Collection[str],
) -> WebhookPRMatch:
    """Match a normalized GitHub webhook event against a webhook-backed github_pr rule.

    Webhook CI fires fail-fast on failures only (``check_run.completed``, or ``check_suite.completed``
    only when ``check_run`` is not enabled on the source). Full all-green CI rollups across all check
    runs are computed only in poll mode, because GitHub emits one ``check_suite`` per workflow.
    """
    if not isinstance(rule.source, GitHubPRSourceSpec):
        return WebhookPRMatch(matched=False)
    src, base = rule.source, event.kind.split(".", 1)[0]
    if not _webhook_targets_pr(src, checkpoint, event, base):
        return WebhookPRMatch(matched=False)

    base_cp = checkpoint or GitHubPRCheckpoint(seeded=True, updated_at=event.timestamp)
    sha = (opt_str(event.data.get("head_sha")) if base == "pull_request" else None) or base_cp.head_sha
    url = opt_str(event.data.get("url"))

    if event.kind in {"pull_request.merged", "pull_request.closed"}:
        merged = event.kind == "pull_request.merged" or event.data.get("merged") is True
        pr_state: PRState = "merged" if merged else "closed"
        cp = update_pr_checkpoint(base_cp, updated_at=event.timestamp, head_sha=sha, pr_state=pr_state, seeded=True)
        if ("merge" if merged else "close") not in src.events:
            return WebhookPRMatch(matched=True, checkpoint=cp, disable_immediately=src.auto_disable_on_close)
        item = make_pr_item(pr_state, f"PR #{src.number} {pr_state}", actor=event.actor, url=url)
        kind, actor, terminal = f"github_pr.{pr_state}", event.actor, True
    else:
        reopen = {"pull_request.opened", "pull_request.synchronize", "pull_request.reopened"}
        pr_state = "open" if event.kind in reopen else base_cp.pr_state
        cp = update_pr_checkpoint(base_cp, updated_at=event.timestamp, head_sha=sha, pr_state=pr_state, seeded=True)
        classified = _classify_webhook(src, event, allowed_events)
        if classified is None:
            return WebhookPRMatch(matched=base == "pull_request", checkpoint=cp if base == "pull_request" else None)
        actor, item = classified
        kind, terminal = "github_pr.update", False

    ev = build_pr_event(
        src,
        kind,
        event.event_id,
        event.timestamp,
        cp,
        [item],
        actor=actor,
        title=event.title,
        url=url,
        terminal=terminal,
        github_event=base,
    )
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
    event_number = num if isinstance(num, int) and not isinstance(num, bool) else None
    number_matches = event_number == src.number or src.number in nums
    if base in {"check_run", "check_suite"}:
        event_sha = opt_str(ev.data.get("sha"))
        if cp is not None and cp.head_sha and event_sha is not None and event_sha != cp.head_sha:
            return False
        if number_matches:
            return True
        return bool(not nums and event_number is None and cp is not None and cp.head_sha and event_sha == cp.head_sha)
    return number_matches


def _classify_webhook(
    src: GitHubPRSourceSpec,
    ev: TriggerEvent,
    allowed: Collection[str],
) -> tuple[str | None, dict[str, Any]] | None:
    """Classify one matched webhook event into a wake item, or None if it should not wake.

    Webhook CI emits fail-fast ``ci_failure`` alerts only (never per-suite ``ci_passing``).
    ``check_suite.completed`` fires only when ``check_run`` is absent from ``allowed``.
    """
    kind, d, url = ev.kind, ev.data, ev.data.get("url")
    if kind in {"pull_request.labeled", "pull_request.reopened", "pull_request.ready_for_review"}:
        atype = opt_str(d.get("actor_type"))
        if "activity" not in src.events or is_ignored_activity_actor(src, actor=ev.actor, actor_type=atype):
            return None
        act = kind.split(".", 1)[1]
        return ev.actor, make_pr_item(act, f"{ev.actor or 'someone'} {act.replace('_', ' ')}", actor=ev.actor, url=url)
    if kind in {"issue_comment.created", "pull_request_review.submitted", "pull_request_review_comment.created"}:
        return _classify_activity_webhook(src, ev, allowed)
    conc = (opt_str(d.get("conclusion")) or "").lower()
    if kind == "check_run.completed" and "ci" in src.events and conc in CI_FAIL_CONCLUSIONS:
        name = opt_str(d.get("name")) or "check"
        extra = {"name": name, "conclusion": conc, "sha": d.get("sha"), "url": url}
        return ev.actor, make_pr_item("ci_failure", f"CI '{name}' {conc}", **extra)
    if (
        kind == "check_suite.completed"
        and "ci" in src.events
        and "check_run" not in allowed
        and conc in CI_FAIL_CONCLUSIONS
    ):
        extra = {"conclusion": conc, "sha": d.get("sha"), "url": url}
        return ev.actor, make_pr_item("ci_failure", f"CI check suite {conc}", **extra)
    return None


def _classify_activity_webhook(
    src: GitHubPRSourceSpec,
    ev: TriggerEvent,
    allowed: Collection[str],
) -> tuple[str | None, dict[str, Any]] | None:
    d, actor = ev.data, opt_str(ev.data.get("author")) or ev.actor
    atype = opt_str(d.get("author_type")) or opt_str(d.get("actor_type"))
    if "activity" not in src.events or is_ignored_activity_actor(src, actor=actor, actor_type=atype):
        return None
    body, url, who = clip_text(ev.summary), d.get("url"), actor or "someone"
    sfx = f": {body}" if body else ""
    if ev.kind == "issue_comment.created":
        extra = {"actor": actor, "comment_id": d.get("comment_id"), "body": body, "url": url}
        return actor, make_pr_item("comment", f"Comment by {who}{sfx}", **extra)
    if ev.kind == "pull_request_review.submitted":
        st = (opt_str(d.get("state")) or "").lower()
        if st == "commented" and not body and "pull_request_review_comment" in allowed:
            return None
        extra = {"actor": actor, "state": st, "review_id": d.get("review_id"), "body": body, "url": url}
        return actor, make_pr_item("review", f"Review ({st or 'submitted'}) by {who}{sfx}", **extra)
    path = opt_str(d.get("path"))
    msg = f"Inline comment by {who}{f' on {path}' if path else ''}{sfx}"
    extra = {
        "actor": actor,
        "comment_id": d.get("comment_id"),
        "review_id": d.get("review_id"),
        "path": path,
        "body": body,
        "url": url,
    }
    return actor, make_pr_item("review_comment", msg, **extra)


def format_timeline_entry(src: GitHubPRSourceSpec, entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """Format one GitHub issue timeline entry into a wake item, or None if ignored."""
    ev = (opt_str(entry.get("event")) or "").lower()
    if not ev or ev in _IGNORED_TIMELINE:
        return None
    u = mapping_or_empty(entry.get("user")) or mapping_or_empty(entry.get("actor"))
    actor = opt_str(u.get("login"))
    if is_ignored_activity_actor(src, actor=actor, actor_type=opt_str(u.get("type"))):
        return None
    body, url, eid, who = clip_text(entry.get("body")), entry.get("html_url"), entry.get("id"), actor or "someone"
    sfx = f": {body}" if body else ""
    if ev == "reviewed":
        st = (opt_str(entry.get("state")) or "").lower()
        if st == "commented" and not body:
            return None
        msg = f"Review ({st or 'submitted'}) by {who}{sfx}"
        return make_pr_item("review", msg, id=eid, actor=actor, state=st, body=body, url=url)
    if ev == "commented":
        return make_pr_item("comment", f"Comment by {who}{sfx}", id=eid, actor=actor, body=body, url=url)
    label = opt_str(mapping_or_empty(entry.get("label")).get("name"))
    msg = f"{who} {ev.replace('_', ' ')}{f' ({label})' if label else sfx}"
    return make_pr_item(ev, msg, id=eid, actor=actor, url=url)


def timeline_key(entry: Mapping[str, Any]) -> str:
    """Return a stable deduplication key for one GitHub issue timeline entry."""
    ev = opt_str(entry.get("event")) or "event"
    if entry.get("id") is not None:
        return f"{ev}:{entry['id']}"
    u = mapping_or_empty(entry.get("user")) or mapping_or_empty(entry.get("actor"))
    return f"{ev}:{entry.get('created_at') or entry.get('submitted_at') or ''}:{u.get('login') or ''}"


def group_review_comments(
    src: GitHubPRSourceSpec,
    comments: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Filter and group new inline PR review comments by review ID."""
    grouped: dict[object, list[Mapping[str, Any]]] = {}
    for c in comments:
        u = mapping_or_empty(c.get("user"))
        if is_ignored_activity_actor(src, actor=opt_str(u.get("login")), actor_type=opt_str(u.get("type"))):
            continue
        rid = c.get("pull_request_review_id")
        grouped.setdefault(("r", rid) if rid is not None else ("c", c.get("id")), []).append(c)
    items: list[dict[str, Any]] = []
    keys: list[str] = []
    for grp in grouped.values():
        first, ids = grp[0], [int(c["id"]) for c in grp]
        actor = opt_str(mapping_or_empty(first.get("user")).get("login"))
        rid, url = first.get("pull_request_review_id"), first.get("html_url")
        keys.extend(f"rc:{cid}" for cid in ids)
        if len(grp) == 1:
            path, body = opt_str(first.get("path")), clip_text(first.get("body"))
            msg = f"Inline comment by {actor or 'someone'}{f' on {path}' if path else ''}{f': {body}' if body else ''}"
            extra = {"id": ids[0], "review_id": rid, "actor": actor, "path": path, "body": body, "url": url}
            items.append(make_pr_item("review_comment", msg, **extra))
            continue
        paths = list(dict.fromkeys(p for c in grp if (p := opt_str(c.get("path")))))
        entries = [
            {"id": c.get("id"), "path": c.get("path"), "body": clip_text(c.get("body")), "url": c.get("html_url")}
            for c in grp
        ]
        msg = f"{len(grp)} inline comments by {actor or 'someone'}{f' on {", ".join(paths[:3])}' if paths else ''}"
        extra = {"review_id": rid, "actor": actor, "count": len(grp), "paths": paths, "comments": entries, "url": url}
        items.append(make_pr_item("review_comments", msg, **extra))
    return items, keys


@dataclass(frozen=True, slots=True)
class CheckRunsSummary:
    """Aggregated check-run counts for one PR head commit."""

    total: int
    failed: dict[str, dict[str, Any]]
    pending: int
    passed: int
    non_passing: int

    def overall(self, *, truncated: bool) -> str:
        """Return overall CI rollup state (only 'passing' when all runs passed and not truncated)."""
        if self.total == 0:
            return "none"
        if self.failed:
            return "failing"
        if self.pending or truncated:
            return "pending"
        if self.non_passing or self.passed == 0:
            return "non_passing"
        return "passing"


def summarize_check_runs(runs: Sequence[Mapping[str, Any]]) -> CheckRunsSummary:
    """Summarize check-run payloads into unique total, failed map, pending, passed, and non-passing counts."""
    seen_keys: set[str] = set()
    failed: dict[str, dict[str, Any]] = {}
    pending = passed = non_passing = 0
    for run in runs:
        name = opt_str(run.get("name")) or "check"
        key = str(run["id"]) if run.get("id") is not None else name
        if key in seen_keys:
            continue
        seen_keys.add(key)
        st, conc = (opt_str(run.get("status")) or "").lower(), (opt_str(run.get("conclusion")) or "").lower() or None
        if st != "completed":
            pending += 1
        elif conc in CI_FAIL_CONCLUSIONS:
            url = opt_str(run.get("html_url")) or opt_str(run.get("url"))
            failed[key] = {"name": name, "conclusion": conc, "url": url}
        elif conc in CI_PASS_CONCLUSIONS:
            passed += 1
        else:
            non_passing += 1
    return CheckRunsSummary(len(seen_keys), failed, pending, passed, non_passing)


def build_github_pr_dry_run_event_and_match(
    *,
    config: Config,
    rule: ReactiveTriggerRecord,
    checkpoint: GitHubPRCheckpoint | None,
    actor: str | None,
    text: str | None,
    extra_data: Mapping[str, Any],
    raw_kind: str | None,
    now: float,
) -> tuple[TriggerEvent, bool, str | None]:
    """Build a synthetic github_pr dry-run event and evaluate deterministic rule matching."""
    assert isinstance(rule.source, GitHubPRSourceSpec)
    src = rule.source
    norm_kind = opt_str(raw_kind)
    mapped_cat = _DRY_RUN_CATEGORY_BY_KIND.get(norm_kind.lower()) if norm_kind is not None else None
    if norm_kind is not None and mapped_cat is None:
        return _dry_run_github_pr_webhook_event(
            config=config,
            rule=rule,
            checkpoint=checkpoint,
            actor=actor,
            text=text,
            extra_data=extra_data,
            normalized_kind=norm_kind,
            now=now,
        )

    raw_cat = extra_data.get("category")
    event_type: GitHubPREventType = (
        mapped_cat
        if mapped_cat is not None
        else (raw_cat if raw_cat in {"activity", "ci", "merge", "close"} else src.events[0])
    )
    event_kind = (
        "github_pr.merged"
        if event_type == "merge"
        else ("github_pr.closed" if event_type == "close" else "github_pr.update")
    )
    data: dict[str, Any] = {
        "repo": src.repo,
        "number": src.number,
        "terminal": event_type in {"merge", "close"},
        **extra_data,
    }
    event = TriggerEvent(
        source_type="github_pr",
        source_id=src.repo.lower(),
        kind=event_kind,
        event_id="dry_run",
        timestamp=now,
        actor=actor,
        title=f"{src.repo}#{src.number}",
        text=text,
        data=data,
    )
    sample_repo, sample_num = data.get("repo"), data.get("number")
    repo_ok = isinstance(sample_repo, str) and sample_repo.lower() == src.repo.lower()
    num_ok = isinstance(sample_num, int) and not isinstance(sample_num, bool) and sample_num == src.number
    if not (repo_ok and num_ok) or event_type not in src.events:
        return event, False, None
    if event_type == "activity" and is_ignored_activity_actor(
        src,
        actor=actor,
        actor_type=opt_str(extra_data.get("actor_type")),
    ):
        return (
            event,
            False,
            "Dry run only (no state changed or message sent); activity from this actor is ignored by ignore_actors/ignore_bots.",
        )
    return event, True, None


def _dry_run_github_pr_webhook_event(
    *,
    config: Config,
    rule: ReactiveTriggerRecord,
    checkpoint: GitHubPRCheckpoint | None,
    actor: str | None,
    text: str | None,
    extra_data: Mapping[str, Any],
    normalized_kind: str,
    now: float,
) -> tuple[TriggerEvent, bool, str | None]:
    assert isinstance(rule.source, GitHubPRSourceSpec)
    src, base_event = rule.source, normalized_kind.split(".", 1)[0]
    data: dict[str, Any] = dict(extra_data)
    data.setdefault("repo", src.repo)
    if "number" not in data and "pr_numbers" not in data:
        data["number"] = src.number
    if base_event == "issue_comment":
        data.setdefault("is_pull_request", True)

    webhook_event = TriggerEvent(
        source_type="external_event",
        source_id=src.webhook_source_id or src.repo.lower(),
        kind=normalized_kind,
        event_id="dry_run",
        timestamp=now,
        actor=actor,
        title=f"{src.repo}#{src.number}",
        summary=text,
        text=text,
        data=data,
    )
    allowed_events: frozenset[str] = _DEFAULT_GITHUB_PR_WEBHOOK_EVENTS
    if src.webhook_source_id is not None:
        gh_source = config.external_trigger_policy.event_sources.get(src.webhook_source_id)
        if isinstance(gh_source, GitHubEventSourceConfig):
            allowed_events = frozenset(gh_source.allowed_events)
            if base_event not in allowed_events:
                return webhook_event, False, None

    match_result = match_github_webhook_to_pr_rule(rule, checkpoint, webhook_event, allowed_events=allowed_events)
    return match_result.event or webhook_event, match_result.event is not None, None


def make_pr_item(item_type: str, summary: str, **extra: object) -> dict[str, Any]:
    """Build one structured github_pr wake item dictionary."""
    return {"type": item_type, **extra, "summary": summary}


def update_pr_checkpoint(
    existing: GitHubPRCheckpoint | None,
    *,
    updated_at: float,
    **updates: object,
) -> GitHubPRCheckpoint:
    """Return a copied or new GitHubPRCheckpoint with updated fields."""
    return (existing or GitHubPRCheckpoint(updated_at=updated_at)).model_copy(
        update={"updated_at": updated_at, **updates},
    )


def build_pr_event(
    src: GitHubPRSourceSpec,
    kind: str,
    event_id: str,
    ts: float,
    cp: GitHubPRCheckpoint,
    items: list[dict[str, Any]],
    *,
    actor: str | None,
    title: str | None,
    url: str | None,
    terminal: bool,
    github_event: str | None = None,
) -> TriggerEvent:
    """Construct a normalized github_pr TriggerEvent for webhook or poll delivery."""
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
    if github_event is not None:
        data["github_event"] = github_event
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


def build_poll_pr_event(
    rule: ReactiveTriggerRecord,
    kind: str,
    cp: GitHubPRCheckpoint,
    state: Mapping[str, Any],
    items: list[dict[str, Any]],
    keys: Sequence[str],
    now: float,
) -> tuple[str, TriggerEvent]:
    """Build deterministic dedupe key and normalized TriggerEvent for a polled github_pr wake."""
    assert isinstance(rule.source, GitHubPRSourceSpec)
    dkey = f"ghpr:{hashlib.sha256(f'{rule.trigger_id}:{"|".join(sorted(keys))}'.encode()).hexdigest()[:32]}"
    actor = next((a for it in items if (a := opt_str(it.get("actor")))), None)
    ev = build_pr_event(
        rule.source,
        kind,
        dkey,
        now,
        cp,
        items,
        actor=actor,
        title=opt_str(state.get("pr_title")),
        url=opt_str(state.get("pr_url")),
        terminal=cp.pr_state in {"closed", "merged"},
    )
    return dkey, ev


def extract_pr_summary(payload: Mapping[str, Any]) -> tuple[PRState, str | None, str | None, str | None]:
    """Extract (pr_state, head_sha, title, html_url) from a GitHub pull request REST payload."""
    raw = (opt_str(payload.get("state")) or "open").lower()
    merged = payload.get("merged") is True or payload.get("merged_at") is not None
    state: PRState = "merged" if merged else ("closed" if raw == "closed" else "open")
    sha = opt_str(mapping_or_empty(payload.get("head")).get("sha"))
    return state, sha, clip_text(payload.get("title")), opt_str(payload.get("html_url"))


def mapping_or_empty(value: object) -> Mapping[str, Any]:
    """Return ``value`` if it is a Mapping, else an empty dict."""
    return value if isinstance(value, Mapping) else {}


def opt_str(value: object) -> str | None:
    """Return stripped non-empty string or None."""
    return s if isinstance(value, str) and (s := value.strip()) else None


def clip_text(value: object) -> str | None:
    """Return stripped text clipped to the bounded summary length."""
    return t[:_MAX_TEXT] if (t := opt_str(value)) else None
