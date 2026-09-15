#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OUTPUT_DIR=${OUTPUT_DIR:-"$ROOT_DIR/dist/macos/desktop-helper"}
PYINSTALLER_VERSION=${PYINSTALLER_VERSION:-6.16.0}
UV_BINARY=${UV_BINARY:-$(command -v uv || true)}
HELPER_PYTHON=${HELPER_PYTHON:-3.13}
UNIVERSAL=false

usage() {
    cat <<'EOF'
Usage: macos/build-desktop-helper.sh [--output DIR] [--universal]

Build the fixed-identity Python desktop helper as a nested-app-ready onedir bundle.
Set HELPER_PYTHON to a Python 3.13 executable; universal builds require both
arm64 and x86_64 slices in that interpreter and every collected native library.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output)
            OUTPUT_DIR=$2
            shift 2
            ;;
        --universal)
            UNIVERSAL=true
            shift
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

rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR/dist" "$OUTPUT_DIR/work"
if [[ "$UNIVERSAL" == true ]]; then
    if ! command -v lipo >/dev/null 2>&1; then
        echo "lipo is required to build a universal desktop helper." >&2
        exit 1
    fi
    export MINDROOM_HELPER_TARGET_ARCH=universal2
    HELPER_PYTHON_EXECUTABLE=$(UV_NO_SYNC=1 "$UV_BINARY" python find "$HELPER_PYTHON")
    if ! lipo "$HELPER_PYTHON_EXECUTABLE" -verify_arch arm64 x86_64; then
        echo "Universal helper builds require a Python runtime containing arm64 and x86_64 slices." >&2
        echo "Set HELPER_PYTHON to a universal Python 3.13 executable and retry." >&2
        exit 1
    fi
fi

env -u UV_NO_SYNC "$UV_BINARY" run \
    --isolated \
    --locked \
    --project "$ROOT_DIR" \
    --no-default-groups \
    --extra desktop \
    --python "$HELPER_PYTHON" \
    --with "pyinstaller==$PYINSTALLER_VERSION" \
    pyinstaller \
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
