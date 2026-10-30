"""The storage step that keeps, as one usage ledger row, the usage a session's stored totals hold beyond its ledger."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from mindroom.llm.usage import TokenCounts
from mindroom.session_storage.legacy_agno2_runs import decode_session_json
from mindroom.session_storage.schema import quote_identifier
from mindroom.session_storage.store import database_owner
from mindroom.usage.storage import stored_token_counts

if TYPE_CHECKING:
    import sqlite3

    from mindroom.session_storage.schema import TableNames

type _Model = tuple[str, str]
_UNKNOWN: _Model = ("unknown", "unknown")


# LEGACY_COMPAT: Session usage totals stored beside the usage ledger.
# Legacy format: A session row's ``session_data["session_metrics"]`` holds the token counters, and per-model
# ``details``, that every run save added up, beside the ledger's run snapshots; usage reports read the session's totals
# there, so they also counted history compacted away before the ledger began (v2026.9.191), which no snapshot holds.
# Last legacy release: v2026.10.696; replacement: the next release, whose reports total a session's ledger rows.
# Handling: ``add_pre_ledger_baselines`` runs in the step to layout version 7, on both routes a database reaches it by
# (``upgrade_layout``, ``convert_conversation_database``). A session whose readable totals exceed what its run rows
# count by the reporter's rules (helper usage was never part of the totals; a member run counts in a team's database
# only) gets one undated ``pre_ledger`` row holding each counter's positive residual, by model when the stored details
# reconcile and no model's residual is negative, else under model ``unknown``; it keeps the report's totals. A counter
# stored below the runs' sum then reports the runs' count, and the runs' attribution wins over contradictory stored
# attribution. ``session_data`` stays as stored, which an older release's preflight requires; a row the step wrote is
# never replaced.
# Coverage: tests/session_storage/test_legacy_usage_totals.py::test_a_baseline_keeps_the_report_of_a_compacted_session,
# tests/session_storage/test_legacy_usage_totals.py::test_a_baseline_holds_each_counter_above_the_run_rows,
# tests/session_storage/test_legacy_usage_totals.py::test_contradictory_stored_attribution_leaves_an_unknown_residual,
# tests/session_storage/test_legacy_usage_totals.py::test_unusable_totals_or_none_beyond_the_runs_add_no_row,
# tests/session_storage/test_legacy_usage_totals.py::test_member_runs_count_only_in_a_team_database,
# tests/session_storage/test_legacy_usage_totals.py::test_upgrading_adds_baselines_once_and_rolls_back_with_its_step,
# tests/session_storage/test_legacy_usage_totals.py::test_converting_an_agno_database_adds_its_baselines,
# tests/test_usage_stats.py::test_ledger_attribution_wins_over_contradictory_stored_attribution.
def add_pre_ledger_baselines(connection: sqlite3.Connection, names: TableNames) -> None:
    """Add each session's ``pre_ledger`` usage row, in the caller's write transaction."""
    include_members = database_owner(connection, names).kind == "team"
    usage = quote_identifier(names.usage)
    for session_id, raw_session_data in connection.execute(
        f"SELECT session_id, session_data FROM {quote_identifier(names.sessions)}",  # noqa: S608
    ).fetchall():
        stored = _stored_totals(raw_session_data)
        if stored is None:
            continue
        rows = connection.execute(f"SELECT usage_data FROM {usage} WHERE session_id = ?", (session_id,))  # noqa: S608
        runs = [run for (payload,) in rows if (run := _counted_run(payload, include_members=include_members))]
        baseline = _baseline(*stored, runs)
        if baseline is not None:
            connection.execute(
                f"INSERT OR IGNORE INTO {usage} (session_id, run_id, usage_data) VALUES (?, 'pre_ledger', ?)",  # noqa: S608
                (session_id, json.dumps(baseline)),
            )


def _stored_totals(raw_session_data: object) -> tuple[TokenCounts, dict[_Model, TokenCounts] | None] | None:
    """Return a session's readable stored totals and, when its details reconcile, the same by model."""
    try:
        session_data = _mapping(decode_session_json(raw_session_data))
        totals = _mapping(session_data.get("session_metrics")) if session_data is not None else None
        counted = stored_token_counts(totals) if totals is not None else None
    except (RecursionError, TypeError, ValueError):
        return None
    if totals is None or counted is None:
        return None
    details = totals.get("details")
    return counted, _by_model(details, counted, named=True) if details is not None else None


def _counted_run(payload: object, *, include_members: bool) -> tuple[TokenCounts, dict[_Model, TokenCounts]] | None:
    """Return a run row's usage, in total and by model, as the reporter counts it, or None when it counts none.

    The reporter counts a member run in a team's database only.
    """
    run = _decoded_run(payload)
    parent_run_id = run.get("parent_run_id") if run is not None else None
    if run is None or not (
        parent_run_id is None or (include_members and isinstance(parent_run_id, str) and parent_run_id)
    ):
        return None
    metrics = _mapping(run.get("metrics", {}))
    try:
        counted = stored_token_counts(metrics) if metrics is not None else None
    except ValueError:
        return None
    if metrics is None or counted is None:
        return None
    details = metrics.get("details")
    if details is None:
        return counted, {(_name(run.get("model_provider")), _name(run.get("model"))): counted}
    return counted, _by_model(details, counted, named=False) or {_UNKNOWN: counted}


def _decoded_run(payload: object) -> dict[str, object] | None:
    """Return a usage row's snapshot when it is a run's, or None."""
    try:
        run = json.loads(payload) if isinstance(payload, (str, bytes)) else None
    except (RecursionError, ValueError):
        return None
    run = _mapping(run)
    return run if run is not None and run.get("kind", "run") == "run" else None


def _mapping(value: object) -> dict[str, object] | None:
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


def _baseline(
    stored: TokenCounts,
    stored_models: dict[_Model, TokenCounts] | None,
    runs: list[tuple[TokenCounts, dict[_Model, TokenCounts]]],
) -> dict[str, object] | None:
    """Return the ``pre_ledger`` row of what ``stored`` holds beyond ``runs``, or None when it holds nothing more."""
    counted = TokenCounts()
    counted_models: dict[_Model, TokenCounts] = {}
    for totals, models in runs:
        counted += totals
        for model, model_totals in models.items():
            counted_models[model] = counted_models.get(model, TokenCounts()) + model_totals
    residual = _clamped(stored - counted)
    if residual == TokenCounts():
        return None
    metrics: dict[str, object] = _positive(residual)
    if stored_models is not None:
        by_model = {
            model: stored_models.get(model, TokenCounts()) - counted_models.get(model, TokenCounts())
            for model in stored_models.keys() | counted_models.keys()
        }
        # Both sides reconcile with their totals, so nonnegative model residuals sum to ``residual``.
        if all(_clamped(model_residual) == model_residual for model_residual in by_model.values()):
            metrics["details"] = {
                "model": [
                    {"id": model, "provider": provider, **_positive(model_residual)}
                    for (provider, model), model_residual in sorted(by_model.items())
                    if model_residual != TokenCounts()
                ],
            }
    return {"run_id": "pre_ledger", "kind": "pre_ledger", "metrics": metrics}


def _by_model(details: object, totals: TokenCounts, *, named: bool) -> dict[_Model, TokenCounts] | None:
    """Return ``totals`` by model as stored ``details`` record them, or None when the reporter cannot use them.

    That is when they are malformed, account for other totals, or, ``named``, leave a counted model unnamed, as the
    reporter required of stored session details.
    """
    if not isinstance(details, dict):
        return None
    entries = [entry for group in details.values() for entry in (group if isinstance(group, list) else [None])]
    if not entries or not all(isinstance(entry, dict) for entry in entries):
        return None
    models: dict[_Model, TokenCounts] = {}
    try:
        for entry in cast("list[dict[str, object]]", entries):
            model = (_name(entry.get("provider")), _name(entry.get("id")))
            model_totals = stored_token_counts(entry)
            if model_totals is None:
                # A model whose requests reported no usage adds nothing.
                continue
            if named and _UNKNOWN[0] in model:
                return None
            models[model] = models.get(model, TokenCounts()) + model_totals
    except ValueError:
        return None
    combined = TokenCounts()
    for model_totals in models.values():
        combined += model_totals
    return models if models and combined == totals else None


def _name(value: object) -> str:
    return value if isinstance(value, str) and value else _UNKNOWN[0]


def _clamped(counts: TokenCounts) -> TokenCounts:
    """Return ``counts`` with each negative counter zero."""
    return TokenCounts(**{name: max(0, count) for name, count in counts.to_dict().items()})


def _positive(counts: TokenCounts) -> dict[str, object]:
    return {name: count for name, count in counts.to_dict().items() if count}


__all__ = ["add_pre_ledger_baselines"]
