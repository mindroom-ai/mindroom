#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OUTPUT_DIR=${OUTPUT_DIR:-"$ROOT_DIR/dist/macos/desktop-helper"}
UV_BINARY=${UV_BINARY:-$(command -v uv || true)}
ARCHITECTURE=$(uname -m)

usage() {
    cat <<'EOF'
Usage: macos/build-desktop-helper.sh [--output DIR] [--arch arm64|x86_64]

Build the fixed-identity Python desktop helper as a nested-app-ready onedir bundle.
Build one architecture with matching Python and dependency wheels.
HELPER_PYTHON may override the matching managed Python 3.13 interpreter.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output)
            OUTPUT_DIR=$2
            shift 2
            ;;
        --arch)
            ARCHITECTURE=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "The desktop helper app must be built on macOS." >&2
    exit 1
fi
if [[ -z "$UV_BINARY" || ! -x "$UV_BINARY" ]]; then
    echo "uv is required to build the desktop helper." >&2
    exit 1
fi

case "$ARCHITECTURE" in
    arm64) PYTHON_ARCHITECTURE=aarch64 ;;
    x86_64) PYTHON_ARCHITECTURE=x86_64 ;;
    *) echo "Unsupported desktop helper architecture: $ARCHITECTURE" >&2; exit 2 ;;
esac
HELPER_PYTHON=${HELPER_PYTHON:-"cpython-3.13-macos-${PYTHON_ARCHITECTURE}-none"}
export MINDROOM_HELPER_TARGET_ARCH="$ARCHITECTURE"
export MACOSX_DEPLOYMENT_TARGET=14.0

rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR/dist" "$OUTPUT_DIR/work"

HELPER_ENVIRONMENT="$OUTPUT_DIR/environment"
env -u UV_NO_SYNC UV_PROJECT_ENVIRONMENT="$HELPER_ENVIRONMENT" "$UV_BINARY" sync \
    --locked \
    --project "$ROOT_DIR" \
    --only-group desktop-helper \
    --python "$HELPER_PYTHON" \
    --python-platform "${PYTHON_ARCHITECTURE}-apple-darwin"
# Install local source and version metadata without the backend's dependency set.
"$UV_BINARY" pip install --python "$HELPER_ENVIRONMENT/bin/python" --no-deps --editable "$ROOT_DIR"
"$UV_BINARY" run --no-project --python "$HELPER_ENVIRONMENT/bin/python" \
    /usr/bin/arch "-${ARCHITECTURE}" python -m PyInstaller \
    --clean \
    --noconfirm \
    --distpath "$OUTPUT_DIR/dist" \
    --workpath "$OUTPUT_DIR/work" \
    "$ROOT_DIR/macos/MindRoomDesktopHelper.spec"

HELPER_APP="$OUTPUT_DIR/dist/MindRoom Desktop Helper.app"
if [[ ! -x "$HELPER_APP/Contents/MacOS/MindRoom Desktop Helper" ]]; then
    echo "PyInstaller did not produce the expected desktop helper app." >&2
    exit 1
fi
echo "$HELPER_APP"
