# Mindroom Scripts

## cleanup_agent_edits.py

A Python script to clean up excessive edit history from agent messages in the Synapse database.

### Background

When agents stream their responses (updating every 0.1 seconds), each edit creates a new event in the Matrix/Synapse database. This can lead to significant database bloat - a single 30-second streamed response could create 300 edit events!

### Usage

```bash
# Dry run - see what would be deleted
uv run scripts/cleanup_agent_edits.py --dry-run

# Clean up edits older than 1 hour, keeping only the final version
uv run scripts/cleanup_agent_edits.py --older-than 1 --keep-last 1

# Clean up messages with 10+ edits (about 1 second of streaming)
uv run scripts/cleanup_agent_edits.py --min-edits 10

# Use custom database connection
uv run scripts/cleanup_agent_edits.py \
    --host localhost \
    --port 5432 \
    --database synapse \
    --user synapse \
    --password synapse_password
```

### Options

- `--dry-run`: Show what would be deleted without actually deleting
- `--keep-last N`: Number of recent edits to keep per message (default: 1)
- `--older-than N`: Only clean edits older than N hours (default: 1)
- `--min-edits N`: Only clean messages with at least N edits (default: 5)
- `--host`, `--port`, `--database`, `--user`, `--password`: Database connection settings

### Running via Cron

Add to your crontab to run every 6 hours:

```bash
# Run cleanup every 6 hours
0 */6 * * * /path/to/mindroom/scripts/cleanup_agent_edits.sh
```

### What it does

1. Identifies all mindroom agent accounts (users starting with `@mindroom_` or `@agent_`)
2. Finds messages from these agents with excessive edit history
3. Keeps only the final version (or last N versions) of each message
4. Cleans up all related database entries
5. Runs VACUUM to reclaim disk space

### Safety

- Only affects messages from agent accounts
- Preserves the final version of each message
- Won't affect regular user messages or edits
- Supports dry-run mode to preview changes

## cleanup_agent_edits.sh

Simple bash wrapper for the Python cleanup script. Sets up environment variables and provides sensible defaults for cron usage.

## cleanup_edit_history.sql

SQL script for manual database cleanup (more aggressive, use with caution).
