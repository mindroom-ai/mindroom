#!/usr/bin/env bash
# Backup full Supabase Postgres database using pg_dump.
# Loads env vars from .env via python-dotenv (using uvx), then resolves a DB URL.

set -euo pipefail

<<<<<<< HEAD:cluster/scripts/db/backup_supabase.sh
<<<<<<< HEAD:cluster/scripts/db/backup_supabase.sh
# 1) Load environment variables from saas-platform/.env (preferred)
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
ENV_FILE="${ENV_FILE:-$REPO_ROOT/saas-platform/.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
else
  # Fallback: try loading .env in CWD via python-dotenv if available
  if command -v uvx >/dev/null 2>&1; then
    set -a
    eval "$(uvx --from python-dotenv[cli] dotenv list --format shell)"
    set +a
  fi
fi
=======
# 1) Load environment variables from .env file
=======
# 1) Load environment variables from saas-platform/.env file
# Resolve path to saas-platform directory (two levels up from this script dir)
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
PLATFORM_DIR=$(cd -- "$SCRIPT_DIR/../.." && pwd)
ENV_FILE="$PLATFORM_DIR/.env"

>>>>>>> c2dc252d (db: backup script now explicitly loads saas-platform/.env via python-dotenv):saas-platform/scripts/db/backup_supabase.sh
set -a
if [ -f "$ENV_FILE" ]; then
  eval "$(uvx --from 'python-dotenv[cli]' dotenv -f "$ENV_FILE" list --format shell)"
else
  # Fallback to current directory .env if saas-platform/.env is missing
  eval "$(uvx --from 'python-dotenv[cli]' dotenv list --format shell)"
fi
set +a
>>>>>>> d0e206f7 (scripts: move backup_supabase.sh to saas-platform/scripts/db and update Makefile target):saas-platform/scripts/db/backup_supabase.sh

# 2) Resolve database URL
# Prefer DATABASE_URL or SUPABASE_DB_URL if set explicitly in .env.
DB_URL=${DATABASE_URL:-${SUPABASE_DB_URL:-}}

if [[ -z "${DB_URL}" ]]; then
  # Try to construct a Supabase DB URL from SUPABASE_URL and SUPABASE_DB_PASSWORD
  SUPA_URL_HOST=""
  if [[ -n "${SUPABASE_URL:-}" ]]; then
    SUPA_URL_HOST=$(printf "%s" "$SUPABASE_URL" | sed -E 's~^https?://([^/]+)/?.*$~\1~')
  fi

  if [[ -z "${SUPA_URL_HOST}" ]] || [[ -z "${SUPABASE_DB_PASSWORD:-}" ]]; then
<<<<<<< HEAD:cluster/scripts/db/backup_supabase.sh
  echo "[ERROR] Cannot determine database URL." >&2
  echo "- Set DATABASE_URL or SUPABASE_DB_URL in .env, OR" >&2
  echo "- Provide SUPABASE_URL and SUPABASE_DB_PASSWORD in saas-platform/.env to auto-construct." >&2
  echo "  (Tip: Find DB password in Supabase → Project → Settings → Database)" >&2
  exit 1
=======
    echo "[ERROR] Cannot determine database URL." >&2
    echo "- Set DATABASE_URL or SUPABASE_DB_URL in .env, OR" >&2
    echo "- Provide SUPABASE_URL and SUPABASE_DB_PASSWORD in .env to auto-construct." >&2
    echo "  (Tip: Find DB password in Supabase → Project → Settings → Database)" >&2
    exit 1
>>>>>>> d0e206f7 (scripts: move backup_supabase.sh to saas-platform/scripts/db and update Makefile target):saas-platform/scripts/db/backup_supabase.sh
  fi

  DB_USER=${SUPABASE_DB_USER:-postgres}
  DB_NAME=${SUPABASE_DB_NAME:-postgres}
  DB_HOST="db.${SUPA_URL_HOST}"
  DB_URL="postgresql://${DB_USER}:${SUPABASE_DB_PASSWORD}@${DB_HOST}:5432/${DB_NAME}?sslmode=require"
fi

# 3) Choose output path
STAMP=$(date +%Y%m%d_%H%M%S)
OUT_DIR=${DB_BACKUP_DIR:-backups}
mkdir -p "$OUT_DIR"
OUT_FILE="$OUT_DIR/supabase_full_${STAMP}.dump"

echo "Backing up database to: $OUT_FILE"

# 4) Run pg_dump (prefer local binary; fallback to Docker postgres if not available)
if command -v pg_dump >/dev/null 2>&1; then
  pg_dump --no-owner --no-privileges --format=custom --file="$OUT_FILE" "$DB_URL"
else
  echo "pg_dump not found locally; attempting Docker-based pg_dump (postgres:16-alpine)" >&2
  docker run --rm \
    -v "$(pwd)":/work -w /work \
    -e PGCLIENTENCODING=UTF8 \
    postgres:16-alpine \
    sh -c "pg_dump --no-owner --no-privileges --format=custom --file='$OUT_FILE' '$DB_URL'"
fi

echo "✅ Backup complete: $OUT_FILE"
