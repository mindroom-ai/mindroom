#!/usr/bin/env bash
# Bundle the upstream Metal-enabled server and its runtime libraries, pinned by digest.
set -euo pipefail
DESTINATION=${1:?Pass the destination resource directory}
BUILD=b11146
DIGEST=1ad3f9eff80edb9dbef4259ad564d1720612ef7eea48fa4afed0e54f5f3d5711
CACHE="${XDG_CACHE_HOME:-$HOME/Library/Caches}/mindroom-build/llama.cpp-$BUILD"
ARCHIVE="$CACHE/llama-$BUILD-bin-macos-arm64.tar.gz"
mkdir -p "$CACHE" "$DESTINATION"
if [[ ! -f "$ARCHIVE" ]]; then
    curl --fail --location --retry 3 --output "$ARCHIVE.partial" \
        "https://github.com/ggml-org/llama.cpp/releases/download/$BUILD/llama-$BUILD-bin-macos-arm64.tar.gz"
    mv "$ARCHIVE.partial" "$ARCHIVE"
fi
if [[ $(shasum -a 256 "$ARCHIVE" | cut -d ' ' -f 1) != "$DIGEST" ]]; then
    echo "llama.cpp archive checksum mismatch: $ARCHIVE" >&2
    exit 1
fi
TEMP=$(mktemp -d)
trap 'rm -rf "$TEMP"' EXIT
tar -xzf "$ARCHIVE" -C "$TEMP"
SERVER=$(find "$TEMP" -type f -name llama-server -print -quit)
SOURCE=$(dirname "$SERVER")
cp "$SOURCE/llama-server" "$DESTINATION/"
cp -P "$SOURCE/"*.dylib "$DESTINATION/"
cp "$SOURCE/LICENSE" "$DESTINATION/LICENSE"
printf '%s\n' "$BUILD" > "$DESTINATION/BUILD"
