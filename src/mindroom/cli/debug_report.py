"""`mindroom debug-report`: collect the backend side of a MindRoom Chat bug report.

The config is only read: it is parsed with its includes, never migrated or written, and only the settings this
command uses are validated.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from mindroom.cli.config import activate_cli_runtime
from mindroom.constants import resolve_session_state_root
from mindroom.debug_report import DebugReportSources, build_debug_report, collect_ids

if TYPE_CHECKING:
    from typing import NoReturn

    from mindroom.constants import RuntimePaths

_BUG_REPORT_TYPE = "io.mindroom.bug_report"


def _fail(message: str) -> NoReturn:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(1)


def _read_report(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    # UnicodeDecodeError, JSONDecodeError, and the integer digit limit are ValueErrors; deep nesting is a RecursionError.
    except (OSError, ValueError, RecursionError) as exc:
        _fail(f"cannot read {path}: {exc}")
    if not isinstance(data, dict) or data.get("type") != _BUG_REPORT_TYPE:
        _fail(f"{path} is not a MindRoom Chat bug report.")
    return data


def _read_config_source(runtime_paths: RuntimePaths) -> dict[str, Any]:
    """Parse the config file with its includes, without validating, migrating, or writing it.

    Loading the full config can persist an access migration, and inspecting an install must not rewrite its config.
    Only `event_journal` and `debug.llm_request_log_dir` are used, so a config the runtime would reject still works.
    """
    import yaml  # noqa: PLC0415

    from mindroom.config.yaml_includes import load_yaml_config_source_with_digests  # noqa: PLC0415

    path = runtime_paths.config_path
    if not path.exists():
        typer.echo(f"Note: no config at {path}; using default storage locations.", err=True)
        return {}
    try:
        data, _source_digests, _uses_includes = load_yaml_config_source_with_digests(path)
    except (yaml.YAMLError, OSError, UnicodeError) as exc:
        typer.echo(f"Warning: config not read ({exc}); using default storage locations.", err=True)
        return {}
    if not isinstance(data, dict):
        typer.echo(f"Warning: {path} is not a mapping; using default storage locations.", err=True)
        return {}
    return data


def _llm_request_log_dir(config_source: dict[str, Any], storage_root: Path) -> Path:
    """Return the directory the runtime writes LLM request logs to (`llm_request_logging._daily_log_path`).

    The configured value is used as given, so a relative directory is relative to the working directory, like the
    runtime's; the default is the one `model_loading` passes.
    """
    debug = config_source.get("debug")
    configured = debug.get("llm_request_log_dir") if isinstance(debug, dict) else None
    if isinstance(configured, str) and configured:
        return Path(configured)
    return storage_root / "logs" / "llm_requests"


def _resolve_sources(runtime_paths: RuntimePaths) -> DebugReportSources:
    from mindroom.config.matrix import EventJournalConfig  # noqa: PLC0415

    storage_root = runtime_paths.storage_root
    config_source = _read_config_source(runtime_paths)
    journal_postgres_url: str | None = None
    journal_error: str | None = None
    try:
        journal = EventJournalConfig.model_validate(config_source.get("event_journal") or {})
        if journal.backend == "postgres":
            journal_postgres_url = journal.resolve_postgres_database_url(runtime_paths)
    except ValueError as exc:
        # An invalid section (pydantic's ValidationError is a ValueError), or a URL that lives in the service's
        # environment rather than this one.
        # Reading the SQLite file instead could show the wrong database, so the journal sources report the error.
        journal_error = str(exc)
    return DebugReportSources(
        storage_root=storage_root,
        session_root=resolve_session_state_root(storage_root, runtime_paths),
        journal_postgres_url=journal_postgres_url,
        llm_request_log_dir=_llm_request_log_dir(config_source, storage_root),
        journal_error=journal_error,
    )


def _summarize(name: str, source: dict[str, Any]) -> str:
    line = f"{name}: {source['status']}, {len(source['items'])} items"
    if source["error"]:
        line += f" ({source['error']})"
    return line


def debug_report(
    report: Path | None = typer.Argument(  # noqa: B008
        None,
        help="MindRoom Chat bug report JSON, as attached to a Report a bug message.",
    ),
    event: list[str] | None = typer.Option(None, "--event", "-e", help="Matrix event ID; repeatable."),  # noqa: B008
    room: str | None = typer.Option(None, "--room", "-r", help="Matrix room ID."),
    thread: str | None = typer.Option(None, "--thread", "-t", help="Thread root event ID."),
    config_path: Path | None = typer.Option(None, "--config", "-c", help="Use this config file path."),  # noqa: B008
    storage_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--storage-path",
        "-s",
        help="Base directory for persistent MindRoom data.",
    ),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write the JSON here instead of stdout."),  # noqa: B008
) -> None:
    """Collect everything the backend stored about a reported conversation, as JSON."""
    ids = collect_ids(
        _read_report(report) if report is not None else None,
        event_ids=event or (),
        room_id=room,
        thread_id=thread,
    )
    if ids.is_empty():
        _fail("pass a bug report file or at least one of --event, --room, --thread.")
    if config_path is not None and not config_path.expanduser().exists():
        _fail(f"config file not found: {config_path}")
    runtime_paths = activate_cli_runtime(path=config_path, storage_path=storage_path)
    sources = _resolve_sources(runtime_paths)
    document = build_debug_report(sources, ids, generated_at=datetime.now(UTC).isoformat())
    # ASCII escaping is lossless: a stored lone surrogate stays its exact \udXXX escape instead of failing to encode.
    text = json.dumps(document, indent=2, default=str)
    if output is None:
        typer.echo(text)
    else:
        output.write_text(text + "\n", encoding="utf-8")
    for name, source in document["sources"].items():
        typer.echo(_summarize(name, source), err=True)
    typer.echo(f"storage root: {sources.storage_root}", err=True)
