#!/usr/bin/env python3
"""Screenshot script for the MindRoom dashboard.

Usage:
    python take_screenshot.py [port-or-url]

Example:
    python take_screenshot.py
    python take_screenshot.py 3003
    python take_screenshot.py http://localhost:3003

The dashboard must be running first. Use `uv run mindroom run` by default,
or `./run-frontend.sh` when using the frontend dev server.

"""

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_DEMO_URL = "http://localhost:8765"


def _resolve_demo_url(target: str | None) -> str:
    """Resolve a screenshot target from CLI input or the default URL."""
    if target is None:
        return DEFAULT_DEMO_URL

    try:
        port = int(target)
    except ValueError:
        parsed = urlparse(target)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return target
        msg = f"Error: '{target}' is not a valid port number or URL"
        raise ValueError(msg) from None

    return f"http://localhost:{port}"


def take_screenshot(demo_url: str = DEFAULT_DEMO_URL) -> bool:
    """Take a screenshot of the dashboard using Puppeteer."""
    print(f"Taking screenshot of app at {demo_url}...")
    result = subprocess.run(
        ["bun", "run", "screenshot"],
        check=False,
        cwd=Path(__file__).parent,  # We're now in the frontend directory
        env={**os.environ, "DEMO_URL": demo_url},
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
    if len(sys.argv) > 2:
        print("Usage: python take_screenshot.py [port-or-url]")
        print("Examples:")
        print("  python take_screenshot.py")
        print("  python take_screenshot.py 8765")
        print("  python take_screenshot.py http://localhost:3003")
        print("\nNote: The dashboard must be running first. Use `uv run mindroom run`.")
        sys.exit(1)

    try:
        demo_url = _resolve_demo_url(sys.argv[1] if len(sys.argv) == 2 else None)
    except ValueError as exc:
        print(str(exc))
        sys.exit(1)

    print(f"Taking screenshot of app at {demo_url}...")

    # Take screenshot
    success = take_screenshot(demo_url)

    if success:
        print("\n📸 Screenshots saved to frontend/screenshots/")
        print("You can now view the MindRoom dashboard appearance!")
    else:
        print("\n❌ Failed to take screenshots.")
        print("Make sure the dashboard is running on the specified port.")
        sys.exit(1)


if __name__ == "__main__":
    main()
