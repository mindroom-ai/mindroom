#!/bin/bash
# Script to set up automated cleanup cron job on the server

# Create the cleanup script in the user's home directory
cat > ~/cleanup_agent_edits.sh << 'SCRIPT'
#!/bin/bash
# Cleanup script for Synapse edit history

POSTGRES_CONTAINER="synapse-postgres"
LOG_FILE="$HOME/cleanup_agent_edits.log"

echo "[$(date)] Starting cleanup..." >> "$LOG_FILE"

docker exec -i "$POSTGRES_CONTAINER" psql -U synapse -d synapse << 'EOF' >> "$LOG_FILE" 2>&1
-- Cleanup script for excessive edit history from agent messages
WITH agent_users AS (
    SELECT name AS user_id FROM users
    WHERE name LIKE '@mindroom_%' OR name LIKE '@agent_%'
),
edits_to_delete AS (
    SELECT er.event_id
    FROM event_relations er
    JOIN events e ON er.event_id = e.event_id
    JOIN agent_users au ON e.sender = au.user_id
    WHERE er.relation_type = 'm.replace'
      AND e.origin_server_ts < (EXTRACT(EPOCH FROM NOW() - INTERVAL '1 hour') * 1000)
      AND er.event_id NOT IN (
          SELECT DISTINCT ON (er2.relates_to_id) er2.event_id
          FROM event_relations er2
          JOIN events e2 ON er2.event_id = e2.event_id
          WHERE er2.relation_type = 'm.replace'
          ORDER BY er2.relates_to_id, e2.origin_server_ts DESC
      )
),
deleted AS (
    DELETE FROM events WHERE event_id IN (SELECT event_id FROM edits_to_delete) RETURNING event_id
)
SELECT COUNT(*) || ' edit events deleted' AS result FROM deleted;

DELETE FROM event_json WHERE event_id NOT IN (SELECT event_id FROM events);
DELETE FROM event_relations WHERE event_id NOT IN (SELECT event_id FROM events);
VACUUM ANALYZE;
EOF

echo "[$(date)] Cleanup complete" >> "$LOG_FILE"
SCRIPT

chmod +x ~/cleanup_agent_edits.sh

# Add to crontab (runs every 6 hours)
(crontab -l 2>/dev/null; echo "0 */6 * * * $HOME/cleanup_agent_edits.sh") | crontab -

echo "Cron job installed. Will run every 6 hours."
echo "Check ~/cleanup_agent_edits.log for execution logs."
