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

  # Force IPv4 resolution for Supabase connectivity
  DB_HOSTADDR=$(python -c "import socket; print(socket.gethostbyname('${DB_HOST}'))")

  # URL-encode the password to handle special characters like @, *, !, etc.
  ENC_PASS=$(python - <<'PY'
from urllib.parse import quote
import os
print(quote(os.environ.get('SUPABASE_DB_PASSWORD',''), safe=''))
PY
)

  # Build connection URL with IPv4 address
  DB_URL="postgresql://${DB_USER}:${ENC_PASS}@${DB_HOST}:5432/${DB_NAME}?sslmode=require&hostaddr=${DB_HOSTADDR}"
fi

# 3) Choose output path
STAMP=$(date +%Y%m%d_%H%M%S)
OUT_DIR=${DB_BACKUP_DIR:-backups}
mkdir -p "$OUT_DIR"
OUT_FILE="$OUT_DIR/supabase_full_${STAMP}.dump"

# 4) Run pg_dump (single attempt, fail fast)
if command -v pg_dump >/dev/null 2>&1; then
  pg_dump \
    --no-owner \
    --no-privileges \
    --format=custom \
    --file="$OUT_FILE" \
    "$DB_URL"
else
  docker run --rm \
    -v "$(pwd)":/work -w /work \
    postgres:16-alpine \
    pg_dump --no-owner --no-privileges --format=custom --file="$OUT_FILE" "$DB_URL"
fi
