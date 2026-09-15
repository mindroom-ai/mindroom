#!/usr/bin/env bash

set -euo pipefail

HELPER_APP=${1:?Usage: macos/verify-desktop-helper.sh HELPER_APP arm64|x86_64}
ARCHITECTURE=${2:?Expected helper architecture is required}
case "$ARCHITECTURE" in
    arm64|x86_64) ;;
    *) echo "Unsupported desktop helper architecture: $ARCHITECTURE" >&2; exit 2 ;;
esac
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

while IFS= read -r -d '' binary; do
    if file "$binary" | grep -q "Mach-O"; then
        lipo "$binary" -verify_arch "$ARCHITECTURE"
    fi
done < <(find "$HELPER_APP/Contents" -type f -print0)

echo "Verified fixed desktop helper identity: $EXPECTED_ID"
