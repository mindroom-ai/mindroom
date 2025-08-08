#!/usr/bin/env -S uv run
"""Cleanup script for removing excessive edit history from agent messages in Synapse database.

This script:
1. Identifies all mindroom agent accounts
2. Finds messages with excessive edit history (from streaming)
3. Keeps only the final version of each message
4. Cleans up related database entries
5. Provides statistics on cleanup

Usage:
    uv run scripts/cleanup_agent_edits.py [--dry-run] [--keep-last N] [--older-than HOURS]
"""
# /// script
# dependencies = [
#   "psycopg2-binary",
# ]
# ///

import argparse
import os
import sys
from datetime import datetime, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor


def get_db_connection(config: dict) -> psycopg2.extensions.connection:
    """Create database connection to Synapse PostgreSQL."""
    return psycopg2.connect(
        host=config.get("host", "localhost"),
        port=config.get("port", 5432),
        database=config.get("database", "synapse"),
        user=config.get("user", "synapse"),
        password=config.get("password", "synapse_password"),
    )


def get_agent_user_ids(conn: psycopg2.extensions.connection) -> list[str]:
    """Get all mindroom agent user IDs from the database."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Find all users that match the mindroom agent pattern
        cur.execute("""
            SELECT name AS user_id
            FROM users
            WHERE name LIKE '@mindroom_%'
               OR name LIKE '@agent_%'
            ORDER BY name
        """)
        return [row["user_id"] for row in cur.fetchall()]


def find_messages_with_edits(
    conn: psycopg2.extensions.connection, agent_user_ids: list[str], older_than_hours: int = 1, min_edits: int = 5
) -> dict:
    """Find messages from agents that have excessive edit history."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Get current timestamp in milliseconds (Matrix uses ms since epoch)
        cutoff_time = int((datetime.now() - timedelta(hours=older_than_hours)).timestamp() * 1000)

        # Find original messages with many edits
        agent_ids_str = ",".join(f"'{uid}'" for uid in agent_user_ids)

        query = f"""
            WITH edit_counts AS (
                -- Count edits per original message
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
                r.room_alias_or_id
            FROM edit_counts ec
            LEFT JOIN (
                SELECT room_id,
                       COALESCE(room_alias, room_id) AS room_alias_or_id
                FROM rooms
                LEFT JOIN room_aliases ON rooms.room_id = room_aliases.room_id
            ) r ON ec.room_id = r.room_id
            ORDER BY edit_count DESC
        """

        cur.execute(query)
        return {row["original_event_id"]: row for row in cur.fetchall()}


def get_edits_for_message(
    conn: psycopg2.extensions.connection, original_event_id: str, keep_last: int = 1
) -> tuple[list[str], str]:
    """Get all edit event IDs for a message, returning those to delete and the one to keep."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Get all edits ordered by timestamp
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

        # Keep the most recent edit(s)
        to_keep = edits[:keep_last]
        to_delete = edits[keep_last:]

        return [edit["event_id"] for edit in to_delete], to_keep[0]["event_id"] if to_keep else None


def cleanup_edit_events(
    conn: psycopg2.extensions.connection, event_ids_to_delete: list[str], dry_run: bool = False
) -> int:
    """Delete edit events and related data from the database."""
    if not event_ids_to_delete:
        return 0

    deleted_count = 0

    with conn.cursor() as cur:
        # Delete from all related tables
        tables_to_clean = [
            "event_relations",
            "event_edges",
            "event_forward_extremities",
            "event_backward_extremities",
            "event_json",
            "events",  # This should be last
        ]

        for table in tables_to_clean:
            if dry_run:
                # Just count what would be deleted
                cur.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {table}
                    WHERE event_id = ANY(%s)
                """,
                    (event_ids_to_delete,),
                )
                count = cur.fetchone()[0]
                if count > 0:
                    print(f"  Would delete {count} rows from {table}")
            else:
                # Actually delete
                cur.execute(
                    f"""
                    DELETE FROM {table}
                    WHERE event_id = ANY(%s)
                """,
                    (event_ids_to_delete,),
                )
                if table == "events":
                    deleted_count = cur.rowcount

    if not dry_run:
        conn.commit()

    return deleted_count


def main():
    parser = argparse.ArgumentParser(description="Clean up excessive edit history from agent messages")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without actually deleting")
    parser.add_argument("--keep-last", type=int, default=1, help="Number of recent edits to keep (default: 1)")
    parser.add_argument("--older-than", type=int, default=1, help="Only clean edits older than N hours (default: 1)")
    parser.add_argument(
        "--min-edits", type=int, default=5, help="Only clean messages with at least N edits (default: 5)"
    )
    parser.add_argument("--host", default=os.getenv("SYNAPSE_DB_HOST", "localhost"), help="PostgreSQL host")
    parser.add_argument("--port", type=int, default=int(os.getenv("SYNAPSE_DB_PORT", "5432")), help="PostgreSQL port")
    parser.add_argument("--database", default=os.getenv("SYNAPSE_DB_NAME", "synapse"), help="Database name")
    parser.add_argument("--user", default=os.getenv("SYNAPSE_DB_USER", "synapse"), help="Database user")
    parser.add_argument(
        "--password", default=os.getenv("SYNAPSE_DB_PASSWORD", "synapse_password"), help="Database password"
    )

    args = parser.parse_args()

    # Database configuration
    db_config = {
        "host": args.host,
        "port": args.port,
        "database": args.database,
        "user": args.user,
        "password": args.password,
    }

    print(f"Connecting to database {db_config['database']} at {db_config['host']}:{db_config['port']}...")

    try:
        conn = get_db_connection(db_config)
    except psycopg2.Error as e:
        print(f"Failed to connect to database: {e}")
        sys.exit(1)

    try:
        # Find agent accounts
        print("\nFinding agent accounts...")
        agent_user_ids = get_agent_user_ids(conn)
        print(f"Found {len(agent_user_ids)} agent accounts:")
        for uid in agent_user_ids[:10]:  # Show first 10
            print(f"  - {uid}")
        if len(agent_user_ids) > 10:
            print(f"  ... and {len(agent_user_ids) - 10} more")

        if not agent_user_ids:
            print("No agent accounts found. Nothing to clean.")
            return

        # Find messages with excessive edits
        print(f"\nFinding messages with {args.min_edits}+ edits older than {args.older_than} hour(s)...")
        messages_with_edits = find_messages_with_edits(conn, agent_user_ids, args.older_than, args.min_edits)

        if not messages_with_edits:
            print("No messages found with excessive edits. Nothing to clean.")
            return

        print(f"Found {len(messages_with_edits)} messages with excessive edits:")

        # Show statistics
        total_edits = sum(msg["edit_count"] for msg in messages_with_edits.values())
        print(f"  Total edits across all messages: {total_edits}")

        # Show top messages by edit count
        sorted_messages = sorted(messages_with_edits.items(), key=lambda x: x[1]["edit_count"], reverse=True)
        for _event_id, info in sorted_messages[:5]:
            print(f"  - Message in {info['room_alias_or_id']}: {info['edit_count']} edits")

        if len(sorted_messages) > 5:
            print(f"  ... and {len(sorted_messages) - 5} more messages")

        # Process each message
        print(f"\n{'DRY RUN: ' if args.dry_run else ''}Processing cleanup...")
        total_deleted = 0
        total_to_delete = 0

        for original_event_id, info in messages_with_edits.items():
            edits_to_delete, kept_edit = get_edits_for_message(conn, original_event_id, args.keep_last)

            if edits_to_delete:
                total_to_delete += len(edits_to_delete)

                if args.dry_run:
                    print(f"\nWould delete {len(edits_to_delete)} edits for message in {info['room_alias_or_id']}")
                else:
                    deleted = cleanup_edit_events(conn, edits_to_delete, args.dry_run)
                    total_deleted += deleted
                    if deleted > 0:
                        print(f"  Deleted {deleted} edits for message in {info['room_alias_or_id']}")

        # Summary
        print(f"\n{'=' * 50}")
        if args.dry_run:
            print(f"DRY RUN SUMMARY: Would delete {total_to_delete} edit events")
            print("Run without --dry-run to actually perform cleanup")
        else:
            print(f"CLEANUP COMPLETE: Deleted {total_deleted} edit events")

            # Vacuum to reclaim space
            print("\nRunning VACUUM ANALYZE to reclaim space...")
            with conn.cursor() as cur:
                conn.set_isolation_level(0)  # VACUUM requires autocommit mode
                cur.execute("VACUUM ANALYZE")
            print("Database optimization complete!")

    except Exception as e:
        print(f"Error during cleanup: {e}")
        if not args.dry_run:
            conn.rollback()
        sys.exit(1)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
