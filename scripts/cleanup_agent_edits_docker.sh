#!/bin/bash
# Run cleanup script against dockerized PostgreSQL

set -e

# Run the cleanup script with docker postgres connection
# Use synapse-postgres as default, but allow override via environment variable
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-synapse-postgres}"
docker exec -i "$POSTGRES_CONTAINER" psql -U synapse -d synapse << 'EOF'
-- Cleanup script for excessive edit history from agent messages

-- Set variables
\set min_edits 2
\set older_than_hours 0.1

-- Find agent accounts
WITH agent_users AS (
    SELECT name AS user_id
    FROM users
    WHERE name LIKE '@mindroom_%'
       OR name LIKE '@agent_%'
),
-- Find messages with excessive edits
edit_counts AS (
    SELECT
        er.relates_to_id AS original_event_id,
        COUNT(*) AS edit_count,
        e.sender,
        e.room_id
    FROM event_relations er
    JOIN events e ON er.event_id = e.event_id
    JOIN agent_users au ON e.sender = au.user_id
    WHERE er.relation_type = 'm.replace'
      AND e.origin_server_ts < (EXTRACT(EPOCH FROM NOW() - INTERVAL '6 minutes') * 1000)
    GROUP BY er.relates_to_id, e.sender, e.room_id
    HAVING COUNT(*) >= :min_edits
),
-- Get all edits to delete (keeping only the most recent)
edits_to_delete AS (
    SELECT er.event_id
    FROM event_relations er
    JOIN events e ON er.event_id = e.event_id
    WHERE er.relates_to_id IN (SELECT original_event_id FROM edit_counts)
      AND er.relation_type = 'm.replace'
      AND er.event_id NOT IN (
          -- Keep the most recent edit for each message
          SELECT DISTINCT ON (er2.relates_to_id) er2.event_id
          FROM event_relations er2
          JOIN events e2 ON er2.event_id = e2.event_id
          WHERE er2.relates_to_id IN (SELECT original_event_id FROM edit_counts)
            AND er2.relation_type = 'm.replace'
          ORDER BY er2.relates_to_id, e2.origin_server_ts DESC
      )
)
-- Show what we found
SELECT
    'Found ' || COUNT(DISTINCT ec.original_event_id) || ' messages with excessive edits' AS status,
    'Total edits to delete: ' || COUNT(etd.event_id) AS edits_info
FROM edit_counts ec
CROSS JOIN edits_to_delete etd;

-- Actually delete the edits
WITH deleted AS (
    DELETE FROM events
    WHERE event_id IN (
        SELECT er.event_id
        FROM event_relations er
        JOIN events e ON er.event_id = e.event_id
        JOIN users u ON e.sender = u.name
        WHERE er.relation_type = 'm.replace'
          AND (u.name LIKE '@mindroom_%' OR u.name LIKE '@agent_%')
          AND e.origin_server_ts < (EXTRACT(EPOCH FROM NOW() - INTERVAL '6 minutes') * 1000)
          AND er.event_id NOT IN (
              -- Keep the most recent edit
              SELECT DISTINCT ON (er2.relates_to_id) er2.event_id
              FROM event_relations er2
              JOIN events e2 ON er2.event_id = e2.event_id
              WHERE er2.relation_type = 'm.replace'
              ORDER BY er2.relates_to_id, e2.origin_server_ts DESC
          )
    )
    RETURNING event_id
)
SELECT COUNT(*) || ' edit events deleted' AS result FROM deleted;

-- Clean up orphaned entries
DELETE FROM event_json WHERE event_id NOT IN (SELECT event_id FROM events);
DELETE FROM event_relations WHERE event_id NOT IN (SELECT event_id FROM events);

VACUUM ANALYZE;
EOF

echo "Cleanup complete!"
