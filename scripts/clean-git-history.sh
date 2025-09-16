#!/bin/bash
# Clean sensitive data from git history using BFG

set -euo pipefail

read -p "Continue? (yes/no): " confirm
if [ "$confirm" != "yes" ]; then
    exit 0
fi

# Require BFG to be installed
if ! command -v bfg &> /dev/null; then
    echo "BFG not found. Install: brew install bfg" >&2
    exit 1
fi

# Create backup
cp -r .git .git.backup.$(date +%Y%m%d_%H%M%S)

# Create patterns file for sensitive data
cat > /tmp/sensitive-patterns.txt << 'EOF'
# API Keys
sk-proj-*==>REMOVED-OPENAI-KEY
sk-ant-*==>REMOVED-ANTHROPIC-KEY
sk-or-v1-*==>REMOVED-OPENROUTER-KEY
sk_live_*==>REMOVED-STRIPE-KEY
sk_test_*==>REMOVED-STRIPE-TEST-KEY
whsec_*==>REMOVED-WEBHOOK-SECRET
pk_live_*==>REMOVED-STRIPE-PUB-KEY
pk_test_*==>REMOVED-STRIPE-TEST-PUB-KEY

# Generic patterns
password=changeme==>password=REMOVED
password: changeme==>password: REMOVED
password = "changeme"==>password = "REMOVED"
api_key=*==>api_key=REMOVED
api-key=*==>api-key=REMOVED
secret=*==>secret=REMOVED

# Supabase
eyJ*==>REMOVED-SUPABASE-KEY

# Common test keys
test-api-key==>REMOVED
demo-api-key==>REMOVED
example-api-key==>REMOVED
EOF

# Remove .env files from history
bfg --delete-files .env --no-blob-protection .
bfg --delete-files .env.local --no-blob-protection .
bfg --delete-files .env.production --no-blob-protection .

# Replace sensitive strings
bfg --replace-text /tmp/sensitive-patterns.txt --no-blob-protection .

# Clean up
git reflog expire --expire=now --all
git gc --prune=now --aggressive

# Verify changes
if git log --all --full-history --grep="sk-proj-\|sk-ant-\|sk_live_\|whsec_" > /tmp/secrets-check.log 2>&1; then
    if [ -s /tmp/secrets-check.log ]; then
        echo "Warning: Some commits may still reference secrets" >&2
    fi
fi

# Cleanup temp files
rm -f /tmp/sensitive-patterns.txt /tmp/secrets-check.log
