#!/usr/bin/env python3
"""Take screenshots of the platform frontend from the project root.

This is a convenience wrapper that calls the platform frontend screenshot tool.
"""

import subprocess
import sys
from pathlib import Path

PLATFORM_FRONTEND_DIR = Path(__file__).parent / "platform-frontend"
SCREENSHOT_SCRIPT = PLATFORM_FRONTEND_DIR / "take_screenshot.py"


def main() -> None:
    """Run the platform frontend screenshot script."""
    if not SCREENSHOT_SCRIPT.exists():
        print(f"Error: {SCREENSHOT_SCRIPT} not found", file=sys.stderr)
        print("Make sure you're running this from the saas-platform directory", file=sys.stderr)
        sys.exit(1)

    # Pass all arguments through to the actual script
    cmd = [sys.executable, str(SCREENSHOT_SCRIPT), *sys.argv[1:]]

    try:
        result = subprocess.run(cmd, check=False)
        sys.exit(result.returncode)
    except Exception as e:
        print(f"Error running screenshot script: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
