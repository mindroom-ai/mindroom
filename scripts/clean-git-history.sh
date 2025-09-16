#!/bin/bash
# Clean sensitive data from git history
# Uses BFG Repo-Cleaner for safety and simplicity

set -euo pipefail

echo "🧹 Git History Cleanup Tool"
echo "==========================="
echo ""
echo "⚠️  WARNING: This will rewrite git history!"
echo "Make sure all team members are aware before proceeding."
echo ""
read -p "Continue? (yes/no): " confirm
if [ "$confirm" != "yes" ]; then
    echo "Aborted."
    exit 0
fi

# Check for BFG
if ! command -v bfg &> /dev/null && ! command -v java &> /dev/null; then
    echo "❌ BFG Repo-Cleaner not found. Installing instructions:"
    echo ""
    echo "Option 1 (macOS): brew install bfg"
    echo "Option 2 (Linux): sudo apt install bfg-repo-cleaner"
    echo "Option 3 (Manual): Download from https://rtyley.github.io/bfg-repo-cleaner/"
    echo ""
    exit 1
fi

# Download BFG if not installed but Java is available
if ! command -v bfg &> /dev/null && command -v java &> /dev/null; then
    echo "📥 Downloading BFG Repo-Cleaner..."
    curl -L https://repo1.maven.org/maven2/com/madgag/bfg/1.14.0/bfg-1.14.0.jar -o /tmp/bfg.jar
    BFG="java -jar /tmp/bfg.jar"
else
    BFG="bfg"
fi

# Create backup
echo "📦 Creating backup..."
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

echo "🔍 Scanning for sensitive files..."

# Remove .env files from history
$BFG --delete-files .env --no-blob-protection .
$BFG --delete-files .env.local --no-blob-protection .
$BFG --delete-files .env.production --no-blob-protection .

# Replace sensitive strings
echo "🔄 Replacing sensitive patterns..."
$BFG --replace-text /tmp/sensitive-patterns.txt --no-blob-protection .

# Clean up
echo "🧹 Cleaning up..."
git reflog expire --expire=now --all
git gc --prune=now --aggressive

# Verify changes
echo ""
echo "📊 Verification:"
echo "Checking for remaining secrets..."
if git log --all --full-history --grep="sk-proj-\|sk-ant-\|sk_live_\|whsec_" > /tmp/secrets-check.log 2>&1; then
    if [ -s /tmp/secrets-check.log ]; then
        echo "⚠️  Warning: Some commits may still reference secrets in commit messages"
        echo "Review: /tmp/secrets-check.log"
    else
        echo "✅ No obvious secrets found in commit messages"
    fi
fi

# Final instructions
echo ""
echo "🎯 Final Steps:"
echo "1. Review the changes: git log --oneline -20"
echo "2. Force push to remote: git push --force-with-lease --all"
echo "3. Force push tags: git push --force-with-lease --tags"
echo "4. Notify all team members to re-clone or reset their repos"
echo "5. Delete backup if satisfied: rm -rf .git.backup.*"
echo ""
echo "⚠️  IMPORTANT: All team members must run:"
echo "   git fetch --all"
echo "   git reset --hard origin/main"
echo ""
echo "✅ Git history cleaned!"

# Cleanup temp files
rm -f /tmp/sensitive-patterns.txt /tmp/secrets-check.log
[ -f /tmp/bfg.jar ] && rm -f /tmp/bfg.jar
