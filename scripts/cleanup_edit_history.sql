-- Cleanup script for Synapse database to remove old message edit history
-- WARNING: This modifies the Synapse database directly. Use with caution!
-- Always backup your database before running this script.

-- This query identifies and removes old edit events (m.replace relations)
-- while keeping the most recent version of each message

-- First, identify edit events older than 1 day
WITH old_edits AS (
    SELECT e.event_id, e.room_id, e.origin_server_ts
    FROM events e
    JOIN event_relations er ON e.event_id = er.event_id
    WHERE er.relation_type = 'm.replace'
    AND e.origin_server_ts < (EXTRACT(EPOCH FROM NOW() - INTERVAL '1 day') * 1000)
),
-- Keep the latest edit for each original message
latest_edits AS (
    SELECT er.relates_to_id, MAX(e.origin_server_ts) as max_ts
    FROM events e
    JOIN event_relations er ON e.event_id = er.event_id
    WHERE er.relation_type = 'm.replace'
    GROUP BY er.relates_to_id
)
-- Delete old edits except the latest one for each message
DELETE FROM events
WHERE event_id IN (
    SELECT oe.event_id
    FROM old_edits oe
    JOIN event_relations er ON oe.event_id = er.event_id
    LEFT JOIN latest_edits le ON er.relates_to_id = le.relates_to_id
    WHERE oe.origin_server_ts < le.max_ts OR le.max_ts IS NULL
);

-- Also clean up orphaned entries in related tables
DELETE FROM event_json WHERE event_id NOT IN (SELECT event_id FROM events);
DELETE FROM event_relations WHERE event_id NOT IN (SELECT event_id FROM events);
DELETE FROM event_edges WHERE event_id NOT IN (SELECT event_id FROM events);
DELETE FROM event_forward_extremities WHERE event_id NOT IN (SELECT event_id FROM events);

-- Vacuum to reclaim space
VACUUM ANALYZE;
