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

    print(f"Taking screenshot of app at http://localhost:{port}...")
    result = subprocess.run(
        ["bun", "run", "screenshot"],
        check=False,
        cwd=Path(__file__).parent,  # We're now in the frontend directory
        env={**os.environ, **env},
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Error taking screenshot: {result.stderr}")
        return False

    print(result.stdout)
    return True


def main() -> None:
    """Main function to take screenshots."""
    if len(sys.argv) != 2:
        print("Usage: python take_screenshot.py <port>")
        print("Example: python take_screenshot.py 3003")
        print("\nNote: The servers must be running first. Use ./run.sh to start them.")
        sys.exit(1)

    try:
        port = int(sys.argv[1])
    except ValueError:
        print(f"Error: '{sys.argv[1]}' is not a valid port number")
        sys.exit(1)

    print(f"Taking screenshot of app on port {port}...")

    # Take screenshot
    success = take_screenshot(port)

    if success:
        print("\n📸 Screenshots saved to frontend/screenshots/")
        print("You can now view the MindRoom Configuration Widget appearance!")
    else:
        print("\n❌ Failed to take screenshots.")
        print("Make sure the widget is running on the specified port.")
        sys.exit(1)


if __name__ == "__main__":
    main()
