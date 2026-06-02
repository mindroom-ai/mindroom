#!/usr/bin/env bash

set -euo pipefail

APP_NAME="MindRoom"
DISPLAY_NAME="MindRoom"
INSTALL=false
CREATE_DMG=false

usage() {
    cat <<'EOF'
Usage: macos/build-macos-app.sh [--install] [--dmg]

Build the native macOS menu bar app for MindRoom.

Options:
  --install   Copy the built app to /Applications and open it.
  --dmg       Create dist/macos/MindRoom.dmg.
  -h, --help  Show this help text.

Environment:
  CODESIGN_IDENTITY  Codesign identity to use. Defaults to ad-hoc signing (-).
  APP_VERSION        CFBundleShortVersionString to stamp into the app.
                     Defaults to the current package version when available.
  BUILD_VERSION      CFBundleVersion to stamp into the app. Defaults to
                     GITHUB_RUN_NUMBER, then the git commit count.
  SPARKLE_PUBLIC_ED_KEY
                     Public EdDSA key for Sparkle updates. If unset, the app
                     builds without enabling Sparkle update checks.
  UV_BINARY          uv binary to bundle. Defaults to the uv found on PATH.
  INSTALL_DIR        Install destination. Defaults to /Applications.
  MINDROOM_SKIP_OPEN Set to 1 to skip opening the app after --install.
  NOTARIZE           Set to 1 to notarize and staple the DMG. Requires --dmg.
  APPLE_ID           Apple ID email used for notarization.
  APPLE_APP_SPECIFIC_PASSWORD
                     App-specific password used by xcrun notarytool.
  APPLE_TEAM_ID      Apple Developer Team ID used for notarization.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --install)
            INSTALL=true
            shift
            ;;
        --dmg)
            CREATE_DMG=true
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

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PACKAGE_DIR="$ROOT_DIR/macos/$APP_NAME"
DIST_DIR="$ROOT_DIR/dist/macos"
APP_DIR="$DIST_DIR/$APP_NAME.app"
DMG_STAGING_DIR="$DIST_DIR/dmg-staging"
DMG_RW_PATH="$DIST_DIR/$APP_NAME-rw.dmg"
INFO_PLIST="$PACKAGE_DIR/Resources/Info.plist"
ENTITLEMENTS_PLIST="$PACKAGE_DIR/Resources/MindRoom.entitlements"
ICON_SOURCE_PNG="$ROOT_DIR/frontend/public/logo-square.png"
ICONSET_DIR="$DIST_DIR/MindRoom.iconset"
APP_ICON_ICNS="$DIST_DIR/MindRoom.icns"
CODESIGN_IDENTITY=${CODESIGN_IDENTITY:--}
APP_VERSION=${APP_VERSION:-}
BUILD_VERSION=${BUILD_VERSION:-${GITHUB_RUN_NUMBER:-}}
UV_BINARY=${UV_BINARY:-$(command -v uv || true)}
INSTALL_DIR=${INSTALL_DIR:-/Applications}
NOTARIZE=${NOTARIZE:-0}
SPARKLE_PUBLIC_ED_KEY=${SPARKLE_PUBLIC_ED_KEY:-}

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This script builds a macOS .app bundle and must run on macOS." >&2
    exit 1
fi

for required_tool in swift sips iconutil; do
    if ! command -v "$required_tool" >/dev/null 2>&1; then
        echo "$required_tool is required to build the MindRoom app." >&2
        exit 1
    fi
done

if [[ -z "$UV_BINARY" || ! -x "$UV_BINARY" ]]; then
    echo "uv is required so it can be bundled into the app. Set UV_BINARY or install uv." >&2
    exit 1
fi

if [[ ! -f "$INFO_PLIST" ]]; then
    echo "Info.plist is missing: $INFO_PLIST" >&2
    exit 1
fi

if [[ ! -f "$ENTITLEMENTS_PLIST" ]]; then
    echo "Entitlements plist is missing: $ENTITLEMENTS_PLIST" >&2
    exit 1
fi

if [[ ! -f "$ICON_SOURCE_PNG" ]]; then
    echo "MindRoom icon source is missing: $ICON_SOURCE_PNG" >&2
    exit 1
fi

if [[ "$NOTARIZE" == "1" && "$CREATE_DMG" != true ]]; then
    echo "NOTARIZE=1 requires --dmg so there is a distributable artifact to notarize." >&2
    exit 1
fi

codesign_args() {
    printf '%s\0' --force --sign "$CODESIGN_IDENTITY"
    if [[ "$CODESIGN_IDENTITY" != "-" ]]; then
        printf '%s\0' --timestamp --options runtime
    fi
}

sign_executable() {
    local target="$1"
    local args=()
    while IFS= read -r -d '' arg; do
        args+=("$arg")
    done < <(codesign_args)
    codesign "${args[@]}" "$target"
}

sign_app() {
    local target="$1"
    local args=()
    while IFS= read -r -d '' arg; do
        args+=("$arg")
    done < <(codesign_args)
    codesign --deep --entitlements "$ENTITLEMENTS_PLIST" "${args[@]}" "$target"
}

resolve_app_version() {
    local raw_version="$APP_VERSION"
    if [[ -z "$raw_version" ]]; then
        raw_version=$(
            PYPROJECT_PATH="$ROOT_DIR/pyproject.toml" python3 <<'PY' 2>/dev/null || true
import os
import pathlib
import tomllib

pyproject = pathlib.Path(os.environ["PYPROJECT_PATH"])
print(tomllib.loads(pyproject.read_text())["project"]["version"])
PY
        )
    fi
    if [[ -z "$raw_version" ]]; then
        raw_version="0.1.0"
    fi
    raw_version="${raw_version#v}"
    if [[ "$raw_version" =~ ^[0-9]+([.][0-9]+){0,2} ]]; then
        printf '%s\n' "${BASH_REMATCH[0]}"
        return
    fi
    echo "Could not derive a macOS app version from: $raw_version" >&2
    exit 1
}

resolve_build_version() {
    local build_version="$BUILD_VERSION"
    if [[ -z "$build_version" ]]; then
        build_version=$(git -C "$ROOT_DIR" rev-list --count HEAD 2>/dev/null || true)
    fi
    if [[ -z "$build_version" ]]; then
        build_version=$(date +%Y%m%d%H%M%S)
    fi
    build_version=$(printf '%s' "$build_version" | tr -cd '0-9.')
    if [[ -z "$build_version" || ! "$build_version" =~ ^[0-9]+([.][0-9]+)*$ ]]; then
        echo "Could not derive a numeric macOS app build version from: ${BUILD_VERSION:-unset}" >&2
        exit 1
    fi
    printf '%s\n' "$build_version"
}

stamp_info_plist() {
    local app_version
    local build_version
    app_version=$(resolve_app_version)
    build_version=$(resolve_build_version)

    /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $app_version" "$APP_DIR/Contents/Info.plist"
    /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $build_version" "$APP_DIR/Contents/Info.plist"
    if [[ -n "$SPARKLE_PUBLIC_ED_KEY" ]]; then
        /usr/libexec/PlistBuddy -c "Delete :SUPublicEDKey" "$APP_DIR/Contents/Info.plist" >/dev/null 2>&1 || true
        /usr/libexec/PlistBuddy -c "Add :SUPublicEDKey string $SPARKLE_PUBLIC_ED_KEY" "$APP_DIR/Contents/Info.plist"
    fi
    echo "Stamped app bundle version $app_version ($build_version)"
}

build_app_icon() {
    rm -rf "$ICONSET_DIR" "$APP_ICON_ICNS"
    mkdir -p "$ICONSET_DIR"
    sips -z 16 16 "$ICON_SOURCE_PNG" --out "$ICONSET_DIR/icon_16x16.png" >/dev/null
    sips -z 32 32 "$ICON_SOURCE_PNG" --out "$ICONSET_DIR/icon_16x16@2x.png" >/dev/null
    sips -z 32 32 "$ICON_SOURCE_PNG" --out "$ICONSET_DIR/icon_32x32.png" >/dev/null
    sips -z 64 64 "$ICON_SOURCE_PNG" --out "$ICONSET_DIR/icon_32x32@2x.png" >/dev/null
    sips -z 128 128 "$ICON_SOURCE_PNG" --out "$ICONSET_DIR/icon_128x128.png" >/dev/null
    sips -z 256 256 "$ICON_SOURCE_PNG" --out "$ICONSET_DIR/icon_128x128@2x.png" >/dev/null
    sips -z 256 256 "$ICON_SOURCE_PNG" --out "$ICONSET_DIR/icon_256x256.png" >/dev/null
    sips -z 512 512 "$ICON_SOURCE_PNG" --out "$ICONSET_DIR/icon_256x256@2x.png" >/dev/null
    sips -z 512 512 "$ICON_SOURCE_PNG" --out "$ICONSET_DIR/icon_512x512.png" >/dev/null
    sips -z 1024 1024 "$ICON_SOURCE_PNG" --out "$ICONSET_DIR/icon_512x512@2x.png" >/dev/null
    iconutil -c icns "$ICONSET_DIR" -o "$APP_ICON_ICNS"
    rm -rf "$ICONSET_DIR"
}

require_notarization_env() {
    if [[ "$CODESIGN_IDENTITY" == "-" ]]; then
        echo "A Developer ID signing identity is required when NOTARIZE=1." >&2
        exit 1
    fi
    for variable in APPLE_ID APPLE_APP_SPECIFIC_PASSWORD APPLE_TEAM_ID; do
        if [[ -z "${!variable:-}" ]]; then
            echo "$variable is required when NOTARIZE=1." >&2
            exit 1
        fi
    done
}

notarize_dmg() {
    local dmg_path="$1"
    local app_path="$2"
    require_notarization_env
    xcrun notarytool submit "$dmg_path" \
        --apple-id "$APPLE_ID" \
        --password "$APPLE_APP_SPECIFIC_PASSWORD" \
        --team-id "$APPLE_TEAM_ID" \
        --wait
    xcrun stapler staple "$app_path"
    xcrun stapler validate "$app_path"
    xcrun stapler staple "$dmg_path"
    xcrun stapler validate "$dmg_path"
}

create_drag_install_dmg() {
    local dmg_path="$1"
    local staging_size_kb
    local image_size_mb
    hdiutil detach "/Volumes/$DISPLAY_NAME" >/dev/null 2>&1 || true
    rm -rf "$DMG_STAGING_DIR" "$DMG_RW_PATH" "$dmg_path"
    mkdir -p "$DMG_STAGING_DIR"
    ditto "$APP_DIR" "$DMG_STAGING_DIR/$APP_NAME.app"
    ln -s /Applications "$DMG_STAGING_DIR/Applications"
    staging_size_kb=$(du -sk "$DMG_STAGING_DIR" | awk '{ print $1 }')
    image_size_mb=$((staging_size_kb / 1024 + 64))
    hdiutil create "$DMG_RW_PATH" -volname "$DISPLAY_NAME" -size "${image_size_mb}m" -fs HFS+ -ov
    local attach_output
    attach_output=$(hdiutil attach "$DMG_RW_PATH" -nobrowse -noautoopen)
    local volume_path
    volume_path=$(printf '%s\n' "$attach_output" | awk -F '\t' '/\/Volumes\// { mount=$NF } END { if (mount) print mount; else exit 1 }')
    ditto "$DMG_STAGING_DIR" "$volume_path"
    sync
    hdiutil detach "$volume_path" -force >/dev/null
    hdiutil convert "$DMG_RW_PATH" -format UDZO -imagekey zlib-level=9 -o "$dmg_path"
    rm -rf "$DMG_STAGING_DIR" "$DMG_RW_PATH"
}

quit_running_app() {
    if ! pgrep -x "$APP_NAME" >/dev/null 2>&1; then
        return
    fi
    osascript -e "quit app \"$APP_NAME\"" >/dev/null 2>&1 || true
    for _ in {1..20}; do
        if ! pgrep -x "$APP_NAME" >/dev/null 2>&1; then
            return
        fi
        sleep 0.2
    done
    pkill -x "$APP_NAME" >/dev/null 2>&1 || true
}

echo "Building $DISPLAY_NAME..."
swift build -c release --package-path "$PACKAGE_DIR" --product "$APP_NAME"
BIN_DIR=$(swift build -c release --package-path "$PACKAGE_DIR" --show-bin-path)
BINARY="$BIN_DIR/$APP_NAME"

if [[ ! -x "$BINARY" ]]; then
    BINARY=$(find "$PACKAGE_DIR/.build" -type f -name "$APP_NAME" -path "*/release/$APP_NAME" -print -quit)
fi

if [[ ! -x "$BINARY" ]]; then
    echo "Built binary not found: $BINARY" >&2
    exit 1
fi

echo "Building app icon..."
build_app_icon

rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Frameworks" "$APP_DIR/Contents/Resources/bin"

SPARKLE_FRAMEWORK="$BIN_DIR/Sparkle.framework"
if [[ ! -d "$SPARKLE_FRAMEWORK" ]]; then
    SPARKLE_FRAMEWORK=$(find "$PACKAGE_DIR/.build" -type d -name Sparkle.framework -path "*/release/*" -print -quit)
fi

if [[ ! -d "$SPARKLE_FRAMEWORK" ]]; then
    echo "Built Sparkle framework not found: $SPARKLE_FRAMEWORK" >&2
    exit 1
fi

cp "$BINARY" "$APP_DIR/Contents/MacOS/$APP_NAME"
cp "$INFO_PLIST" "$APP_DIR/Contents/Info.plist"
ditto "$SPARKLE_FRAMEWORK" "$APP_DIR/Contents/Frameworks/Sparkle.framework"
cp "$UV_BINARY" "$APP_DIR/Contents/Resources/bin/uv"
cp "$APP_ICON_ICNS" "$APP_DIR/Contents/Resources/MindRoom.icns"
chmod 755 "$APP_DIR/Contents/MacOS/$APP_NAME" "$APP_DIR/Contents/Resources/bin/uv"

if ! otool -l "$APP_DIR/Contents/MacOS/$APP_NAME" | grep -q '@executable_path/../Frameworks'; then
    install_name_tool -add_rpath "@executable_path/../Frameworks" "$APP_DIR/Contents/MacOS/$APP_NAME"
fi

stamp_info_plist
sign_executable "$APP_DIR/Contents/Resources/bin/uv"
sign_app "$APP_DIR"

echo "Built $APP_DIR"

if [[ "$CREATE_DMG" == true ]]; then
    DMG_PATH="$DIST_DIR/$APP_NAME.dmg"
    create_drag_install_dmg "$DMG_PATH"
    if [[ "$CODESIGN_IDENTITY" != "-" ]]; then
        codesign --force --sign "$CODESIGN_IDENTITY" --timestamp "$DMG_PATH"
    fi
    if [[ "$NOTARIZE" == "1" ]]; then
        notarize_dmg "$DMG_PATH" "$APP_DIR"
    fi
    echo "Built $DMG_PATH"
fi

if [[ "$INSTALL" == true ]]; then
    INSTALL_PATH="$INSTALL_DIR/$APP_NAME.app"
    mkdir -p "$INSTALL_DIR"
    quit_running_app
    rm -rf "$INSTALL_PATH"
    ditto "$APP_DIR" "$INSTALL_PATH"
    codesign --verify --deep --strict "$INSTALL_PATH"
    if [[ "${MINDROOM_SKIP_OPEN:-0}" != "1" ]]; then
        open "$INSTALL_PATH"
    fi
    echo "Installed $INSTALL_PATH"
fi
