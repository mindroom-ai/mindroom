#!/usr/bin/env bash
# Robust .env file loader that handles various edge cases

load_env_file() {
    local env_file="${1:-.env}"

    if [ ! -f "$env_file" ]; then
        echo "❌ Environment file not found: $env_file" >&2
        return 1
    fi

    # Parse .env file line by line
    while IFS= read -r line || [ -n "$line" ]; do
        # Skip empty lines and comments
        if [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]]; then
            continue
        fi

        # Remove inline comments (but preserve # in values)
        line="${line%%#*}"

        # Trim whitespace
        line="$(echo -e "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"

        # Skip if not a valid assignment
        if [[ ! "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
            continue
        fi

        # Export the variable
        export "$line"
    done < "$env_file"

    echo "✅ Loaded environment from $env_file"
}

# Alternative: Use dotenv if available (more robust for complex cases)
load_env_with_dotenv() {
    local env_file="${1:-.env}"

    if command -v python3 &> /dev/null; then
        python3 -c "
import os
import sys

def load_dotenv(filepath):
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, _, value = line.partition('=')
            if key and value:
                # Remove quotes if present
                value = value.strip()
                if (value.startswith('\"') and value.endswith('\"')) or \
                   (value.startswith(\"'\") and value.endswith(\"'\")):
                    value = value[1:-1]
                os.environ[key.strip()] = value
                print(f'export {key.strip()}=\"{value}\"')

try:
    load_dotenv('$env_file')
except Exception as e:
    print(f'Error loading .env: {e}', file=sys.stderr)
    sys.exit(1)
" | while read -r export_cmd; do
            eval "$export_cmd"
        done
        echo "✅ Loaded environment from $env_file using Python parser"
    else
        # Fallback to simple source
        set -a
        source "$env_file"
        set +a
        echo "✅ Loaded environment from $env_file using source"
    fi
}

# If sourced directly, load the .env file
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    load_env_file "$@"
fi
