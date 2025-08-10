#!/usr/bin/env python3
"""Screenshot script for MindRoom Configuration Widget.

Usage:
    python take_screenshot.py <port>

Example:
    python take_screenshot.py 3003

The servers must be running first. Use ./run.sh to start them.

"""

import os
import subprocess
import sys
from pathlib import Path


def take_screenshot(port: int = 3003) -> bool:
    """Take a screenshot of the widget using Puppeteer."""
    env = {
        "DEMO_URL": f"http://localhost:{port}",
    }

    result = subprocess.run(
        ["pnpm", "run", "screenshot"],
        check=False,
        cwd=Path(__file__).parent / "frontend",
        env={**os.environ, **env},
        capture_output=True,
        text=True,
    )

    return result.returncode == 0


def main() -> None:
    """Main function to take screenshots."""
    if len(sys.argv) != 2:
        sys.exit(1)

    try:
        port = int(sys.argv[1])
    except ValueError:
        sys.exit(1)

    # Take screenshot
    success = take_screenshot(port)

    if success:
        pass
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
