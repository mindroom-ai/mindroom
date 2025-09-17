#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["requests", "python-dotenv"]
# ///
"""Run ad-hoc SQL against Supabase using the service role key.

Loads credentials from `saas-platform/.env` (or overrides via env vars) and
executes the provided SQL by calling the `exec_sql` RPC function defined in the
consolidated migration. This avoids needing direct Postgres connectivity.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / "saas-platform/.env"


def load_supabase_credentials() -> tuple[str, str]:
    """Return (url, service_key) from env/overrides."""
    env = {}
    if ENV_PATH.exists():
        env.update(dotenv_values(ENV_PATH))

    url = env.get("SUPABASE_URL") or os.getenv("SUPABASE_URL")
    key = env.get("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")

    if not url or not key:
        msg = "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set (env or saas-platform/.env)"
        raise SystemExit(msg)

    return url, key


def run_sql(query: str) -> None:
    """Execute `query` against Supabase via the exec_sql RPC."""
    url, service_key = load_supabase_credentials()
    endpoint = f"{url}/rest/v1/rpc/exec_sql"
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }
    payload = {"query": query}

    response = requests.post(endpoint, headers=headers, data=json.dumps(payload), timeout=30)
    if response.status_code == 404:
        msg = (
            "exec_sql function not found.\n"
            "Run the following once in the Supabase SQL editor (or manually via psql) "
            "to bootstrap it, then retry: \n\n"
            "    CREATE OR REPLACE FUNCTION exec_sql(query TEXT)\n"
            "    RETURNS VOID\n"
            "    LANGUAGE plpgsql\n"
            "    SECURITY DEFINER\n"
            "    SET search_path = public\n"
            "    AS $$ BEGIN EXECUTE query; END; $$;\n\n"
            "    REVOKE ALL ON FUNCTION exec_sql(TEXT) FROM PUBLIC;\n"
            "    GRANT EXECUTE ON FUNCTION exec_sql(TEXT) TO service_role;\n"
        )
        raise SystemExit(msg)

    if response.status_code >= 400:
        message = f"Supabase exec_sql failed ({response.status_code}): {response.text}"
        raise SystemExit(message)

    if response.text and response.text.strip() not in {"null", ""}:
        print(response.text)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Execute SQL via Supabase service role")
    parser.add_argument("sql", nargs="?", help="Inline SQL to execute (wrap in quotes)")
    parser.add_argument("-f", "--file", type=Path, help="Path to SQL file to execute")
    args = parser.parse_args()

    if args.file:
        query = args.file.read_text()
    elif args.sql:
        query = args.sql
    else:
        query = sys.stdin.read()

    if not query.strip():
        msg = "No SQL provided"
        raise SystemExit(msg)

    run_sql(query)


if __name__ == "__main__":
    main()
