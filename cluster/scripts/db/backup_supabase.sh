#!/usr/bin/env bash
# Backup full Supabase Postgres database using pg_dump.
# Loads env vars from saas-platform/.env via python-dotenv (uvx),
# then resolves the database URL from env or constructs it from Supabase vars.

set -euo pipefail

# 1) Load environment variables from saas-platform/.env (preferred)
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || true)
if [ -z "${REPO_ROOT:-}" ]; then
  REPO_ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
fi
ENV_FILE="${ENV_FILE:-$REPO_ROOT/saas-platform/.env}"

if [ -f "$ENV_FILE" ]; then
  if command -v uvx >/dev/null 2>&1; then
    set -a
    eval "$(uvx --from 'python-dotenv[cli]' dotenv -f "$ENV_FILE" list --format shell)"
    set +a
  else
    # Fallback: source the file directly (best-effort)
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
  fi
else
  # Fallback: try loading .env in CWD via python-dotenv if available
  if command -v uvx >/dev/null 2>&1; then
    set -a
    eval "$(uvx --from 'python-dotenv[cli]' dotenv list --format shell)"
    set +a
  fi
fi

# 2) Resolve database URL
# Prefer DATABASE_URL or SUPABASE_DB_URL if set explicitly in env.
DB_URL=${DATABASE_URL:-${SUPABASE_DB_URL:-}}

if [[ -z "${DB_URL}" ]]; then
  # Try to construct a Supabase DB URL from SUPABASE_URL and SUPABASE_DB_PASSWORD
  SUPA_URL_HOST=""
  if [[ -n "${SUPABASE_URL:-}" ]]; then
    SUPA_URL_HOST=$(printf "%s" "$SUPABASE_URL" | sed -E 's~^https?://([^/]+)/?.*$~\1~')
  fi

  if [[ -z "${SUPA_URL_HOST}" ]] || [[ -z "${SUPABASE_DB_PASSWORD:-}" ]]; then
    echo "[ERROR] Cannot determine database URL." >&2
    echo "- Set DATABASE_URL or SUPABASE_DB_URL in saas-platform/.env, OR" >&2
    echo "- Provide SUPABASE_URL and SUPABASE_DB_PASSWORD in saas-platform/.env to auto-construct." >&2
    echo "  (Tip: Find DB password in Supabase → Project → Settings → Database)" >&2
    exit 1
  fi

  DB_USER=${SUPABASE_DB_USER:-postgres}
  DB_NAME=${SUPABASE_DB_NAME:-postgres}
  DB_HOST="db.${SUPA_URL_HOST}"

  # Enhanced IPv4 resolution with retry logic
  echo "Resolving database host to IPv4 address..."
  DB_HOSTADDR=""
  for attempt in 1 2 3; do
    DB_HOSTADDR=$(python - <<PY
import socket
import sys
host = "${DB_HOST}"
try:
    # Force IPv4 resolution only
    infos = socket.getaddrinfo(host, 5432, family=socket.AF_INET, type=socket.SOCK_STREAM)
    if infos:
        ip = infos[0][4][0]
        print(ip, file=sys.stderr)  # Log to stderr for visibility
        print(ip)  # Output to stdout for capture
        sys.exit(0)
except socket.gaierror as e:
    print(f"DNS resolution failed: {e}", file=sys.stderr)
except Exception as e:
    print(f"Error resolving {host}: {e}", file=sys.stderr)
sys.exit(1)
PY
) && break || {
      echo "Attempt $attempt failed, retrying..." >&2
      sleep 2
    }
  done

  if [ -z "${DB_HOSTADDR}" ]; then
    echo "[WARNING] Could not resolve IPv4 address, trying with hostname directly" >&2
    # Try alternative resolution methods
    if command -v dig >/dev/null 2>&1; then
      DB_HOSTADDR=$(dig +short A "${DB_HOST}" | head -n1)
      echo "Using dig result: ${DB_HOSTADDR:-none}" >&2
    elif command -v host >/dev/null 2>&1; then
      DB_HOSTADDR=$(host -t A "${DB_HOST}" | grep "has address" | head -n1 | awk '{print $4}')
      echo "Using host result: ${DB_HOSTADDR:-none}" >&2
    fi
  fi

  # URL-encode the password to handle special characters like @, *, !, etc.
  ENC_PASS=$(python - <<'PY'
from urllib.parse import quote
import os
print(quote(os.environ.get('SUPABASE_DB_PASSWORD',''), safe=''))
PY
)

  # Build connection URL with IPv4 preference
  QUERY_PARAMS="?sslmode=require"
  if [ -n "${DB_HOSTADDR}" ]; then
    echo "Using IPv4 address: ${DB_HOSTADDR}" >&2
    # Use hostaddr to force IPv4 connection
    QUERY_PARAMS="${QUERY_PARAMS}&hostaddr=${DB_HOSTADDR}"
    # Also keep hostname for SSL certificate validation
    DB_URL="postgresql://${DB_USER}:${ENC_PASS}@${DB_HOST}:5432/${DB_NAME}${QUERY_PARAMS}"
  else
    echo "Using hostname directly: ${DB_HOST}" >&2
    DB_URL="postgresql://${DB_USER}:${ENC_PASS}@${DB_HOST}:5432/${DB_NAME}${QUERY_PARAMS}"
  fi
fi

# 3) Choose output path
STAMP=$(date +%Y%m%d_%H%M%S)
OUT_DIR=${DB_BACKUP_DIR:-backups}
mkdir -p "$OUT_DIR"
OUT_FILE="$OUT_DIR/supabase_full_${STAMP}.dump"

echo "Backing up database to: $OUT_FILE"

# 4) Function to run pg_dump with retry logic
backup_with_retry() {
  local attempt=1
  local max_attempts=3
  local success=false

  while [ $attempt -le $max_attempts ] && [ "$success" = false ]; do
    echo "Backup attempt $attempt of $max_attempts..."

    if command -v pg_dump >/dev/null 2>&1; then
      # Use local pg_dump with timeout
      if timeout 300 pg_dump \
        --no-owner \
        --no-privileges \
        --format=custom \
        --file="$OUT_FILE" \
        --verbose \
        "$DB_URL" 2>&1 | tee /tmp/pg_dump.log; then
        success=true
      else
        echo "pg_dump failed (exit code: $?)" >&2
        cat /tmp/pg_dump.log >&2
      fi
    else
      echo "pg_dump not found locally; attempting Docker-based pg_dump (postgres:16-alpine)" >&2
      if docker run --rm \
        -v "$(pwd)":/work -w /work \
        -e PGCLIENTENCODING=UTF8 \
        postgres:16-alpine \
        sh -c "timeout 300 pg_dump --no-owner --no-privileges --format=custom --file='$OUT_FILE' --verbose '$DB_URL'" 2>&1 | tee /tmp/pg_dump.log; then
        success=true
      else
        echo "Docker pg_dump failed (exit code: $?)" >&2
        cat /tmp/pg_dump.log >&2
      fi
    fi

    if [ "$success" = false ]; then
      echo "Attempt $attempt failed" >&2
      if [ $attempt -lt $max_attempts ]; then
        echo "Waiting 10 seconds before retry..." >&2
        sleep 10
      fi
      ((attempt++))
    fi
  done

  if [ "$success" = true ]; then
    echo "✅ Backup complete: $OUT_FILE"
    echo "File size: $(du -h "$OUT_FILE" | cut -f1)"
    return 0
  else
    echo "❌ All backup attempts failed" >&2
    return 1
  fi
}

# Run the backup
backup_with_retry
