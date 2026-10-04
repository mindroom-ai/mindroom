"""`mindroom debug-report`: collect the backend side of a MindRoom Chat bug report."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from mindroom.cli.config import activate_cli_runtime, load_config_quiet

if TYPE_CHECKING:
    from typing import NoReturn

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.debug_report import DebugReportSources

_BUG_REPORT_TYPE = "io.mindroom.bug_report"


def _fail(message: str) -> NoReturn:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(1)


def _read_report(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"cannot read {path}: {exc}")
    if not isinstance(data, dict) or data.get("type") != _BUG_REPORT_TYPE:
        _fail(f"{path} is not a MindRoom Chat bug report.")
    return data


def _load_config_if_present(runtime_paths: RuntimePaths) -> Config | None:
    """Load the config for journal and log locations; defaults still find most data without it."""
    from mindroom.config.main import CONFIG_LOAD_USER_ERROR_TYPES  # noqa: PLC0415

    if not runtime_paths.config_path.exists():
        return None
    try:
        return load_config_quiet(runtime_paths, tolerate_plugin_load_errors=True)
    except CONFIG_LOAD_USER_ERROR_TYPES as exc:
        typer.echo(f"Warning: config not loaded ({exc}); using default storage locations.", err=True)
        return None


def _resolve_sources(runtime_paths: RuntimePaths) -> DebugReportSources:
    from mindroom.constants import resolve_session_state_root  # noqa: PLC0415
    from mindroom.debug_report import DebugReportSources  # noqa: PLC0415

    storage_root = runtime_paths.storage_root
    llm_request_log_dir = storage_root / "logs" / "llm_requests"
    journal_sqlite_path: Path | None = storage_root / "tracking" / "event_journal.db"
    journal_postgres_url: str | None = None
    config = _load_config_if_present(runtime_paths)
    if config is not None:
        if config.debug.llm_request_log_dir:
            configured = Path(config.debug.llm_request_log_dir).expanduser()
            llm_request_log_dir = configured if configured.is_absolute() else runtime_paths.config_dir / configured
        if config.event_journal.backend == "postgres":
            journal_postgres_url = config.event_journal.resolve_postgres_database_url(runtime_paths)
            journal_sqlite_path = None
    return DebugReportSources(
        storage_root=storage_root,
        session_root=resolve_session_state_root(storage_root, runtime_paths),
        journal_sqlite_path=journal_sqlite_path,
        journal_postgres_url=journal_postgres_url,
        llm_request_log_dir=llm_request_log_dir,
    )


def _summarize(name: str, source: dict[str, Any]) -> str:
    line = f"{name}: {source['status']}, {len(source['items'])} items"
    if source["status"] == "error":
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
    from mindroom.debug_report import (  # noqa: PLC0415 - keeps readers out of CLI import time
        build_debug_report,
        collect_ids,
    )

    ids = collect_ids(
        _read_report(report) if report is not None else None,
        event_ids=event or (),
        room_id=room,
        thread_id=thread,
    )
    if ids.is_empty():
        _fail("pass a bug report file or at least one of --event, --room, --thread.")
    runtime_paths = activate_cli_runtime(path=config_path, storage_path=storage_path)
    sources = _resolve_sources(runtime_paths)
    document = build_debug_report(sources, ids, generated_at=datetime.now(UTC).isoformat())
    text = json.dumps(document, indent=2, ensure_ascii=False, default=str)
    if output is None:
        typer.echo(text)
    else:
        output.write_text(text + "\n", encoding="utf-8")
    for name, source in document["sources"].items():
        typer.echo(_summarize(name, source), err=True)
    typer.echo(f"storage root: {sources.storage_root}", err=True)
