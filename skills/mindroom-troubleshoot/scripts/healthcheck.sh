#!/usr/bin/env bash
# MindRoom Healthcheck Script
# Checks Matrix connectivity, API health, storage, env vars, and Python version.
# Run from the project root: bash skills/mindroom-troubleshoot/scripts/healthcheck.sh
# Exit code: 0 = all checks pass, 1 = one or more checks failed.

set -euo pipefail

PASS=0
FAIL=0
WARN=0

pass() { PASS=$((PASS + 1)); echo "  [PASS] $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  [FAIL] $1"; }
warn() { WARN=$((WARN + 1)); echo "  [WARN] $1"; }
section() { echo ""; echo "=== $1 ==="; }

# Helper: get HTTP status code from curl, always returns exactly 3 digits
# Respects MATRIX_SSL_VERIFY=false by passing -k when SSL verification is disabled
http_status() {
    local url="$1"
    local curl_args=(-s -o /dev/null -w "%{http_code}" --max-time 5)
    if [ "${MATRIX_SSL_VERIFY:-true}" = "false" ]; then
        curl_args+=(-k)
    fi
    local code
    code=$(curl "${curl_args[@]}" "$url" 2>/dev/null) || true
    # Ensure we only have the last 3 characters (the HTTP code)
    echo "${code: -3}"
}

# ---------------------------------------------------------------------------
# 1. Matrix homeserver reachability
# ---------------------------------------------------------------------------
section "Matrix Homeserver"

HOMESERVER="${MATRIX_HOMESERVER:-http://localhost:8008}"
echo "  Homeserver: $HOMESERVER"

if command -v curl >/dev/null 2>&1; then
    HTTP_CODE=$(http_status "${HOMESERVER}/_matrix/client/versions")
    if [ "$HTTP_CODE" = "200" ]; then
        pass "Matrix homeserver reachable (HTTP $HTTP_CODE)"
    elif [ "$HTTP_CODE" = "000" ]; then
        fail "Matrix homeserver unreachable at $HOMESERVER (connection refused or timeout)"
    else
        warn "Matrix homeserver returned HTTP $HTTP_CODE (expected 200)"
    fi
else
    warn "curl not found -- cannot check Matrix homeserver"
fi

# Check SSL setting
SSL_VERIFY="${MATRIX_SSL_VERIFY:-true}"
if [ "$SSL_VERIFY" = "false" ]; then
    warn "MATRIX_SSL_VERIFY=false -- SSL verification disabled (OK for development)"
else
    pass "SSL verification enabled"
fi

# ---------------------------------------------------------------------------
# 2. MindRoom API health endpoint
# ---------------------------------------------------------------------------
section "MindRoom API"

API_URL="http://localhost:8765/api/health"
if command -v curl >/dev/null 2>&1; then
    API_CODE=$(http_status "$API_URL")
    if [ "$API_CODE" = "200" ]; then
        pass "MindRoom API healthy ($API_URL)"
    elif [ "$API_CODE" = "000" ]; then
        warn "MindRoom API not running at $API_URL (this is expected if only the bot process is running via 'mindroom run')"
    else
        warn "MindRoom API returned HTTP $API_CODE"
    fi
else
    warn "curl not found -- cannot check API health"
fi

# ---------------------------------------------------------------------------
# 3. Storage path and permissions
# ---------------------------------------------------------------------------
section "Storage"

STORAGE="${STORAGE_PATH:-mindroom_data}"
echo "  Storage path: $STORAGE"

if [ -d "$STORAGE" ]; then
    pass "Storage directory exists"

    if [ -w "$STORAGE" ]; then
        pass "Storage directory is writable"
    else
        fail "Storage directory is NOT writable: $STORAGE"
    fi

    # Check subdirectories
    for subdir in tracking memory credentials sessions encryption_keys; do
        if [ -d "$STORAGE/$subdir" ]; then
            if [ -w "$STORAGE/$subdir" ]; then
                pass "Subdirectory writable: $subdir/"
            else
                fail "Subdirectory NOT writable: $subdir/"
            fi
        else
            warn "Subdirectory missing: $subdir/ (will be created on first run)"
        fi
    done

    # Check matrix_state.yaml
    if [ -f "$STORAGE/matrix_state.yaml" ]; then
        pass "matrix_state.yaml exists"
    else
        warn "matrix_state.yaml not found (will be created on first run)"
    fi
else
    warn "Storage directory does not exist: $STORAGE (will be created on first run)"
fi

# ---------------------------------------------------------------------------
# 4. Configuration file
# ---------------------------------------------------------------------------
section "Configuration"

CONFIG_FILE="${MINDROOM_CONFIG_PATH:-${CONFIG_PATH:-config.yaml}}"
echo "  Config path: $CONFIG_FILE"

if [ -f "$CONFIG_FILE" ]; then
    pass "Config file exists"

    if [ -r "$CONFIG_FILE" ]; then
        pass "Config file is readable"
    else
        fail "Config file is NOT readable"
    fi
else
    fail "Config file not found: $CONFIG_FILE"
fi

# ---------------------------------------------------------------------------
# 5. Required environment variables
# ---------------------------------------------------------------------------
section "Environment Variables"

# Check for at least one provider API key or local model setup
HAS_PROVIDER=false
for var in OPENAI_API_KEY ANTHROPIC_API_KEY GOOGLE_API_KEY OPENROUTER_API_KEY DEEPSEEK_API_KEY CEREBRAS_API_KEY GROQ_API_KEY; do
    if [ -n "${!var:-}" ]; then
        pass "Provider key set: $var"
        HAS_PROVIDER=true
    fi
done

# Also check _FILE variants for all providers
for var in OPENAI_API_KEY_FILE ANTHROPIC_API_KEY_FILE GOOGLE_API_KEY_FILE OPENROUTER_API_KEY_FILE DEEPSEEK_API_KEY_FILE CEREBRAS_API_KEY_FILE GROQ_API_KEY_FILE; do
    if [ -n "${!var:-}" ] && [ -f "${!var}" ]; then
        pass "Provider key file exists: $var"
        HAS_PROVIDER=true
    fi
done

if [ "$HAS_PROVIDER" = false ]; then
    # Check credentials directory as fallback
    CRED_DIR="${STORAGE:-mindroom_data}/credentials"
    if [ -d "$CRED_DIR" ] && [ "$(ls -A "$CRED_DIR" 2>/dev/null)" ]; then
        pass "Provider credentials found in $CRED_DIR"
        HAS_PROVIDER=true
    fi
fi

# Check Ollama (local models count as a valid provider)
OLLAMA_URL="${OLLAMA_HOST:-http://localhost:11434}"
if command -v curl >/dev/null 2>&1; then
    OLLAMA_CODE=$(http_status "${OLLAMA_URL}/api/tags")
    if [ "$OLLAMA_CODE" = "200" ]; then
        pass "Ollama server reachable at $OLLAMA_URL"
        HAS_PROVIDER=true
    elif [ -n "${OLLAMA_HOST:-}" ]; then
        warn "Ollama server not reachable at $OLLAMA_URL (HTTP $OLLAMA_CODE)"
    fi
fi

if [ "$HAS_PROVIDER" = false ]; then
    fail "No provider API keys or local model servers found (set at least one: OPENAI_API_KEY, ANTHROPIC_API_KEY, etc., or run Ollama)"
fi

# ---------------------------------------------------------------------------
# 6. Python version
# ---------------------------------------------------------------------------
section "Python"

PYTHON_CMD=""
if command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_CMD="python"
fi

if [ -n "$PYTHON_CMD" ]; then
    # POSIX-safe version extraction (no grep -P)
    PY_VERSION=$($PYTHON_CMD -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
    PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 12 ]; then
        pass "Python $PY_VERSION (>= 3.12 required)"
    else
        fail "Python $PY_VERSION found but >= 3.12 is required"
    fi
else
    fail "Python not found in PATH"
fi

# Check if mindroom package is importable
if [ -n "$PYTHON_CMD" ]; then
    if $PYTHON_CMD -c "import mindroom" 2>/dev/null; then
        pass "mindroom package importable"
    else
        warn "mindroom package not importable (run 'uv sync --all-extras' or check virtualenv)"
    fi
fi

# ---------------------------------------------------------------------------
# 7. .env file
# ---------------------------------------------------------------------------
section "Environment File"

# Look for .env in common locations
ENV_FOUND=false
for env_path in ".env" "../.env" "../../.env"; do
    if [ -f "$env_path" ]; then
        pass ".env file found at $env_path"
        ENV_FOUND=true
        break
    fi
done

if [ "$ENV_FOUND" = false ]; then
    warn "No .env file found (API keys must be set via environment variables or credentials directory)"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "==========================================="
echo "  Results: $PASS passed, $FAIL failed, $WARN warnings"
echo "==========================================="

if [ "$FAIL" -gt 0 ]; then
    echo "  Some checks failed. Review the [FAIL] items above."
    exit 1
else
    if [ "$WARN" -gt 0 ]; then
        echo "  All critical checks passed. Review [WARN] items if needed."
    else
        echo "  All checks passed."
    fi
    exit 0
fi
