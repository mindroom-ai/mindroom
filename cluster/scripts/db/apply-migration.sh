#!/usr/bin/env bash
# Apply one SQL migration to the hosted Supabase project through the Management API.
#
# Usage: SUPABASE_ACCESS_TOKEN=sbp_... SUPABASE_PROJECT_REF=<ref> cluster/scripts/db/apply-migration.sh <file.sql>
#
# Operators have no database password, so this uses a Supabase personal access token instead.
# Every migration from 002 on in saas-platform/supabase/migrations is written to be re-runnable; apply them in numeric
# order. 000 is for fresh installs only. 007 fails without changing anything while a subscription has several instances.
# Prints the API response and exits non-zero when the query fails.

set -euo pipefail

FILE="${1:?Usage: $0 <file.sql>}"
: "${SUPABASE_ACCESS_TOKEN:?Set SUPABASE_ACCESS_TOKEN to a Supabase personal access token}"
: "${SUPABASE_PROJECT_REF:?Set SUPABASE_PROJECT_REF to the Supabase project ref}"
[ -f "$FILE" ] || { echo "No such file: $FILE" >&2; exit 1; }

echo "Applying $FILE to project $SUPABASE_PROJECT_REF"
# The token goes through a file descriptor so it never appears in the process list or output.
response=$(
  jq -Rs '{query: .}' "$FILE" |
    curl -sS --max-time 300 -X POST \
      -H @<(printf 'Authorization: Bearer %s\n' "$SUPABASE_ACCESS_TOKEN") \
      -H 'Content-Type: application/json' \
      --data-binary @- -w '\n%{http_code}' \
      "https://api.supabase.com/v1/projects/$SUPABASE_PROJECT_REF/database/query"
)
code=$(tail -n1 <<<"$response")
body=$(sed '$d' <<<"$response")
echo "HTTP $code"
jq . <<<"$body" 2>/dev/null || echo "$body"
[[ "$code" == 2* ]] || { echo "Migration failed" >&2; exit 1; }
echo "Migration applied"
