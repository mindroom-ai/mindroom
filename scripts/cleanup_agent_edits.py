#!/usr/bin/env -S uv run
"""Cleanup script for removing excessive edit history from agent messages in Synapse database.

This script:
1. Identifies all mindroom agent accounts
2. Finds messages with excessive edit history (from streaming)
3. Keeps only the final version of each message
4. Cleans up related database entries
5. Provides statistics on cleanup

Usage:
    uv run scripts/cleanup_agent_edits.py [OPTIONS]
"""
# /// script
# dependencies = [
#   "psycopg2-binary",
#   "typer",
#   "rich",
# ]
# ///

import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg2
import typer
from psycopg2.extras import RealDictCursor
from rich.console import Console
from rich.table import Table

console = Console()
app = typer.Typer()


@dataclass
class DBConfig:
    """Database configuration."""

    host: str = "localhost"
    port: int = 5432
    database: str = "synapse"
    user: str = "synapse"
    password: str = "synapse_password"  # noqa: S105


def get_db_connection(config: DBConfig) -> psycopg2.extensions.connection:
    """Create database connection to Synapse PostgreSQL."""
    return psycopg2.connect(
        host=config.host,
        port=config.port,
        database=config.database,
        user=config.user,
        password=config.password,
    )


def get_agent_user_ids(conn: psycopg2.extensions.connection) -> list[str]:
    """Get all mindroom agent user IDs from the database."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT name AS user_id
            FROM users
            WHERE name LIKE '@mindroom_%'
               OR name LIKE '@agent_%'
            ORDER BY name
        """)
        return [row["user_id"] for row in cur.fetchall()]


def find_messages_with_edits(
    conn: psycopg2.extensions.connection,
    agent_user_ids: list[str],
    older_than_hours: int = 1,
    min_edits: int = 5,
) -> dict:
    """Find messages from agents that have excessive edit history."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cutoff_time = int((datetime.now(UTC) - timedelta(hours=older_than_hours)).timestamp() * 1000)
        agent_ids_str = ",".join(f"'{uid}'" for uid in agent_user_ids)

        query = f"""  # noqa: S608
            WITH edit_counts AS (
                SELECT
                    er.relates_to_id AS original_event_id,
                    COUNT(*) AS edit_count,
                    MAX(e.origin_server_ts) AS latest_edit_ts,
                    MIN(e.origin_server_ts) AS earliest_edit_ts,
                    e.sender,
                    e.room_id
                FROM event_relations er
                JOIN events e ON er.event_id = e.event_id
                WHERE er.relation_type = 'm.replace'
                  AND e.sender IN ({agent_ids_str})
                  AND e.origin_server_ts < {cutoff_time}
                GROUP BY er.relates_to_id, e.sender, e.room_id
                HAVING COUNT(*) >= {min_edits}
            )
            SELECT
                ec.*,
                COALESCE(ra.room_alias, ec.room_id) AS room_alias_or_id
            FROM edit_counts ec
            LEFT JOIN room_aliases ra ON ec.room_id = ra.room_id
            ORDER BY edit_count DESC
        """  # noqa: S608

        cur.execute(query)
        return {row["original_event_id"]: row for row in cur.fetchall()}


def get_edits_for_message(
    conn: psycopg2.extensions.connection,
    original_event_id: str,
    keep_last: int = 1,
) -> tuple[list[str], str | None]:
    """Get all edit event IDs for a message, returning those to delete and the one to keep."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                er.event_id,
                e.origin_server_ts
            FROM event_relations er
            JOIN events e ON er.event_id = e.event_id
            WHERE er.relates_to_id = %s
              AND er.relation_type = 'm.replace'
            ORDER BY e.origin_server_ts DESC
        """,
            (original_event_id,),
        )

        edits = cur.fetchall()
        if not edits:
            return [], None

        to_keep = edits[:keep_last]
        to_delete = edits[keep_last:]

        return [edit["event_id"] for edit in to_delete], to_keep[0]["event_id"] if to_keep else None


def cleanup_edit_events(
    conn: psycopg2.extensions.connection,
    event_ids_to_delete: list[str],
    dry_run: bool = False,
) -> int:
    """Delete edit events and related data from the database."""
    if not event_ids_to_delete:
        return 0

    deleted_count = 0
    tables_to_clean = [
        "event_relations",
        "event_edges",
        "event_forward_extremities",
        "event_backward_extremities",
        "event_json",
        "events",  # This should be last
    ]

    with conn.cursor() as cur:
        for table in tables_to_clean:
            if dry_run:
                cur.execute(f"SELECT COUNT(*) FROM {table} WHERE event_id = ANY(%s)", (event_ids_to_delete,))  # noqa: S608
                count = cur.fetchone()[0]
                if count > 0:
                    console.print(f"  [yellow]Would delete {count} rows from {table}[/yellow]")
            else:
                cur.execute(f"DELETE FROM {table} WHERE event_id = ANY(%s)", (event_ids_to_delete,))  # noqa: S608
                if table == "events":
                    deleted_count = cur.rowcount

    if not dry_run:
        conn.commit()

    return deleted_count


def perform_cleanup(  # noqa: C901, PLR0912
    conn: psycopg2.extensions.connection,
    dry_run: bool,
    keep_last: int,
    older_than: int,
    min_edits: int,
) -> None:
    """Perform the actual cleanup operation."""
    # Find agent accounts
    with console.status("[bold green]Finding agent accounts..."):
        agent_user_ids = get_agent_user_ids(conn)

    console.print(f"\n[green]Found {len(agent_user_ids)} agent accounts[/green]")
    for uid in agent_user_ids[:5]:
        console.print(f"  • {uid}")
    if len(agent_user_ids) > 5:
        console.print(f"  [dim]... and {len(agent_user_ids) - 5} more[/dim]")

    if not agent_user_ids:
        console.print("[yellow]No agent accounts found. Nothing to clean.[/yellow]")
        return

    # Find messages with excessive edits
    with console.status(f"[bold green]Finding messages with {min_edits}+ edits older than {older_than} hour(s)..."):
        messages_with_edits = find_messages_with_edits(conn, agent_user_ids, older_than, min_edits)

    if not messages_with_edits:
        console.print("[yellow]No messages found with excessive edits. Nothing to clean.[/yellow]")
        return

    console.print(f"\n[green]Found {len(messages_with_edits)} messages with excessive edits[/green]")

    # Show statistics
    total_edits = sum(msg["edit_count"] for msg in messages_with_edits.values())
    console.print(f"[cyan]Total edits across all messages: {total_edits}[/cyan]")

    # Show top messages in a table
    table = Table(title="Top Messages by Edit Count")
    table.add_column("Room", style="cyan")
    table.add_column("Edit Count", style="magenta", justify="right")

    sorted_messages = sorted(messages_with_edits.items(), key=lambda x: x[1]["edit_count"], reverse=True)
    for _, info in sorted_messages[:5]:
        table.add_row(info["room_alias_or_id"], str(info["edit_count"]))

    console.print(table)

    if len(sorted_messages) > 5:
        console.print(f"[dim]... and {len(sorted_messages) - 5} more messages[/dim]\n")

    # Process each message
    if dry_run:
        console.print("\n[yellow]DRY RUN MODE - No changes will be made[/yellow]")

    total_deleted = 0
    total_to_delete = 0

    with console.status("[bold green]Processing cleanup..."):
        for original_event_id, info in messages_with_edits.items():
            edits_to_delete, _ = get_edits_for_message(conn, original_event_id, keep_last)

            if edits_to_delete:
                total_to_delete += len(edits_to_delete)

                if dry_run:
                    console.print(
                        f"[yellow]Would delete {len(edits_to_delete)} edits for message in {info['room_alias_or_id']}[/yellow]",
                    )
                else:
                    deleted = cleanup_edit_events(conn, edits_to_delete, dry_run)
                    total_deleted += deleted
                    if deleted > 0:
                        console.print(
                            f"[green]Deleted {deleted} edits for message in {info['room_alias_or_id']}[/green]",
                        )

    # Summary
    console.rule()
    if dry_run:
        console.print(f"[yellow]DRY RUN SUMMARY: Would delete {total_to_delete} edit events[/yellow]")
        console.print("[dim]Run without --dry-run to actually perform cleanup[/dim]")
    else:
        console.print(f"[green]CLEANUP COMPLETE: Deleted {total_deleted} edit events[/green]")

        # Vacuum to reclaim space
        with console.status("[bold green]Running VACUUM ANALYZE to reclaim space..."), conn.cursor() as cur:
            conn.set_isolation_level(0)  # VACUUM requires autocommit mode
            cur.execute("VACUUM ANALYZE")
        console.print("[green]Database optimization complete![/green]")


@app.command()
def main(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be deleted without actually deleting"),
    keep_last: int = typer.Option(1, "--keep-last", help="Number of recent edits to keep"),
    older_than: int = typer.Option(1, "--older-than", help="Only clean edits older than N hours"),
    min_edits: int = typer.Option(5, "--min-edits", help="Only clean messages with at least N edits"),
    host: str = typer.Option(os.getenv("SYNAPSE_DB_HOST", "localhost"), "--host", help="PostgreSQL host"),
    port: int = typer.Option(int(os.getenv("SYNAPSE_DB_PORT", "5432")), "--port", help="PostgreSQL port"),
    database: str = typer.Option(os.getenv("SYNAPSE_DB_NAME", "synapse"), "--database", help="Database name"),
    user: str = typer.Option(os.getenv("SYNAPSE_DB_USER", "synapse"), "--user", help="Database user"),
    password: str = typer.Option(
        os.getenv("SYNAPSE_DB_PASSWORD", "synapse_password"),
        "--password",
        help="Database password",
    ),
) -> None:
    """Clean up excessive edit history from agent messages in Synapse database."""
    db_config = DBConfig(host=host, port=port, database=database, user=user, password=password)

    console.print(f"[cyan]Connecting to database {db_config.database} at {db_config.host}:{db_config.port}...[/cyan]")

    try:
        conn = get_db_connection(db_config)
    except psycopg2.Error as e:
        console.print(f"[red]Failed to connect to database: {e}[/red]")
        sys.exit(1)

    try:
        perform_cleanup(conn, dry_run, keep_last, older_than, min_edits)
    except Exception as e:
        console.print(f"[red]Error during cleanup: {e}[/red]")
        if not dry_run:
            conn.rollback()
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    app()
