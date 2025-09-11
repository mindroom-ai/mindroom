#!/usr/bin/env python3
"""Take screenshots of the platform frontend.

This script launches the Next.js dev server (if not already running) and takes
screenshots of various pages using Puppeteer.

Usage:
    python take_screenshot.py               # Take all screenshots
    python take_screenshot.py --page landing  # Take specific page screenshot
    python take_screenshot.py --list         # List available pages
"""

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
NODE_SCRIPT = SCRIPT_DIR / "take_screenshot.js"


def take_screenshots(page: str | None = None) -> None:
    """Take screenshots using the Node.js script."""
    if not NODE_SCRIPT.exists():
        print(f"Error: {NODE_SCRIPT} not found", file=sys.stderr)
        sys.exit(1)

    # Check if we should use nix-shell
    shell_nix = SCRIPT_DIR / "shell.nix"
    if shell_nix.exists():
        # Use nix-shell to provide Chromium
        cmd = ["nix-shell", "--run", f"node {NODE_SCRIPT}"]
        print("Using nix-shell to provide Chromium...")
    else:
        # Try to run directly
        cmd = ["node", str(NODE_SCRIPT)]

    if page:
        # For now, the Node script doesn't support individual pages
        # This could be enhanced in the future
        print("Note: Individual page selection not yet implemented. Taking all screenshots.")

    # Run the Node script
    try:
        result = subprocess.run(cmd, cwd=SCRIPT_DIR, check=True)
        sys.exit(result.returncode)
    except subprocess.CalledProcessError as e:
        print(f"Error running screenshot script: {e}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as e:
        if "nix-shell" in str(e):
            print(
                "Error: nix-shell not found. Please install Nix or run 'node take_screenshot.js' directly.",
                file=sys.stderr,
            )
        else:
            print("Error: Node.js not found. Please install Node.js first.", file=sys.stderr)
        sys.exit(1)


def list_pages() -> None:
    """List available pages to screenshot."""
    pages = [
        "landing - Landing page (desktop and mobile)",
        "pricing - Pricing page",
        "login - Login page",
        "signup - Signup page",
        "dashboard - Main dashboard",
        "instance - Instance management",
        "billing - Billing page",
        "usage - Usage analytics",
        "settings - User settings",
    ]

    print("Available pages to screenshot:")
    for page in pages:
        print(f"  • {page}")


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Take screenshots of the platform frontend",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--page",
        help="Specific page to screenshot (not yet implemented)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available pages",
    )

    args = parser.parse_args()

    if args.list:
        list_pages()
    else:
        take_screenshots(args.page)


if __name__ == "__main__":
    main()
