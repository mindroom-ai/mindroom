#!/usr/bin/env bash

set -euo pipefail

OUTPUT=${1:?Usage: macos/build-universal-uv.sh OUTPUT}
UV_BINARY=${UV_BINARY:-$(command -v uv)}
UV_VERSION=$("$UV_BINARY" --version | awk '{ print $2 }')
BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$BUILD_DIR"' EXIT

for architecture in aarch64 x86_64; do
    archive="uv-${architecture}-apple-darwin.tar.gz"
    for suffix in "" .sha256; do
        curl --fail --location --silent --show-error \
            "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${archive}${suffix}" \
            --output "$BUILD_DIR/${archive}${suffix}"
    done
    (cd "$BUILD_DIR" && shasum --algorithm 256 --check "${archive}.sha256")
    tar -xzf "$BUILD_DIR/$archive" -C "$BUILD_DIR"
done
mkdir -p "$(dirname "$OUTPUT")"
lipo -create "$BUILD_DIR/uv-aarch64-apple-darwin/uv" "$BUILD_DIR/uv-x86_64-apple-darwin/uv" -output "$OUTPUT"
chmod 755 "$OUTPUT"
lipo "$OUTPUT" -verify_arch arm64 x86_64
