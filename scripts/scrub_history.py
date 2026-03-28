#!/usr/bin/env python3
"""
ISSUE-034c: Scrub from_history messages from all sessions.

⚠️  Run ONLY while mindroom-chat.service is STOPPED:
    sudo systemctl stop mindroom-chat.service
    cd /srv/mindroom && uv run python scripts/scrub_history.py
    sudo systemctl start mindroom-chat.service

This removes O(N²) bloat from pre-fix sessions (~981 MB → ~15 MB).
"""
import sys
sys.path.insert(0, "src")

from agno.db.sqlite import SqliteDb
from mindroom.compaction import scrub_history_messages_from_sessions

DB_PATH = "/home/basnijholt/.mindroom-chat/mindroom_data/agents/openclaw/sessions/openclaw.db"

def main():
    import os
    db_size_before = os.path.getsize(DB_PATH)
    print(f"DB file size before: {db_size_before / 1048576:.1f} MB")

    storage = SqliteDb(db_file=DB_PATH, session_table="openclaw_sessions")
    stats = scrub_history_messages_from_sessions(storage)

    print(f"\nScrub complete:")
    print(f"  Sessions scanned: {stats.sessions_scanned}")
    print(f"  Sessions changed: {stats.sessions_changed}")
    print(f"  Messages removed: {stats.messages_removed:,}")
    print(f"  Data before: {stats.size_before_bytes / 1048576:.1f} MB")
    print(f"  Data after:  {stats.size_after_bytes / 1048576:.1f} MB")
    reduction = (1 - stats.size_after_bytes / max(stats.size_before_bytes, 1)) * 100
    print(f"  Reduction:   {reduction:.1f}%")

    db_size_after = os.path.getsize(DB_PATH)
    print(f"\nDB file size after: {db_size_after / 1048576:.1f} MB")
    print(f"(Run 'sqlite3 {DB_PATH} VACUUM' to reclaim file space)")

if __name__ == "__main__":
    main()