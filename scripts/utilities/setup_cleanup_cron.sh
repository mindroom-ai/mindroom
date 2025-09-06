#!/bin/bash
# Script to set up automated cleanup cron job on the server

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check if the Python cleanup script exists
if [ ! -f "$SCRIPT_DIR/cleanup_agent_edits.py" ]; then
    echo "Error: cleanup_agent_edits.py not found in $SCRIPT_DIR"
    echo "Please ensure all cleanup scripts are in the same directory"
    exit 1
fi

# Create the cleanup wrapper script in the user's home directory
cat > ~/cleanup_agent_edits_cron.sh << SCRIPT
#!/bin/bash
# Automated cleanup script for Synapse edit history

SCRIPT_DIR="$SCRIPT_DIR"
LOG_FILE="\$HOME/cleanup_agent_edits.log"

echo "[\$(date)] Starting cleanup..." >> "\$LOG_FILE"

# Run the docker cleanup script
cd "\$SCRIPT_DIR" && bash cleanup_agent_edits_docker.sh --min-edits 10 --older-than 1 >> "\$LOG_FILE" 2>&1

echo "[\$(date)] Cleanup complete" >> "\$LOG_FILE"
SCRIPT

chmod +x ~/cleanup_agent_edits_cron.sh

# Add to crontab (runs every 6 hours)
(crontab -l 2>/dev/null | grep -v cleanup_agent_edits_cron.sh; echo "0 */6 * * * $HOME/cleanup_agent_edits_cron.sh") | crontab -

echo "Cron job installed. Will run every 6 hours."
echo "Check ~/cleanup_agent_edits.log for execution logs."
echo ""
echo "The cron job will use: $SCRIPT_DIR/cleanup_agent_edits_docker.sh"
echo "Which calls: $SCRIPT_DIR/cleanup_agent_edits.py"
