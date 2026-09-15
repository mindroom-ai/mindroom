#!/usr/bin/env bash

set -euo pipefail

HELPER_APP=${1:?Usage: macos/verify-desktop-helper.sh HELPER_APP [--universal]}
UNIVERSAL=${2:-}
EXPECTED_ID="chat.mindroom.desktophelper"
EXECUTABLE="$HELPER_APP/Contents/MacOS/MindRoom Desktop Helper"

if [[ ! -x "$EXECUTABLE" ]]; then
    echo "Desktop helper executable is missing: $EXECUTABLE" >&2
    exit 1
fi
IDENTIFIER=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$HELPER_APP/Contents/Info.plist")
if [[ "$IDENTIFIER" != "$EXPECTED_ID" ]]; then
    echo "Desktop helper bundle identifier is $IDENTIFIER; expected $EXPECTED_ID." >&2
    exit 1
fi
if [[ -d "$HELPER_APP/Contents/Library/LaunchServices" ]]; then
    echo "Desktop helper unexpectedly contains a registered launch service." >&2
    exit 1
fi
codesign --verify --deep --strict "$HELPER_APP"

if [[ "$UNIVERSAL" == "--universal" ]]; then
    while IFS= read -r -d '' binary; do
        if file "$binary" | grep -q "Mach-O"; then
            lipo "$binary" -verify_arch arm64 x86_64
        fi
    done < <(find "$HELPER_APP/Contents" -type f -print0)
fi

echo "Verified fixed desktop helper identity: $EXPECTED_ID"
