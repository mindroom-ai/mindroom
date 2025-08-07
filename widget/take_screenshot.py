#!/usr/bin/env python3
"""
Script to take screenshots of the MindRoom Configuration Widget using Puppeteer.
This allows viewing the widget's appearance without opening a browser.
"""

import os
import subprocess
import sys
import time
from pathlib import Path


def start_frontend_server() -> subprocess.Popen[bytes]:
    """Start the frontend dev server in the background."""
    print("Starting frontend server...")
    process = subprocess.Popen(
        ["npm", "run", "dev"],
        cwd=Path(__file__).parent / "frontend",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for server to start
    time.sleep(5)
    return process


def start_backend_server() -> subprocess.Popen[bytes]:
    """Start the backend server in the background."""
    print("Starting backend server...")
    backend_dir = Path(__file__).parent / "backend"

    # Check if config.yaml exists at project root
    config_path = Path(__file__).parent.parent / "config.yaml"
    if not config_path.exists():
        print(f"Warning: config.yaml not found at {config_path}")
        print("The UI will show an error. Please ensure config.yaml exists.")

    # Check if venv exists, if not suggest using uv
    venv_path = backend_dir / ".venv"
    if not venv_path.exists():
        print("Backend virtual environment not found.")
        print("Please run: cd widget/backend && uv sync")
        sys.exit(1)

    # Use the default port 8001
    process = subprocess.Popen(
        [str(venv_path / "bin" / "python"), "-m", "uvicorn", "src.main:app", "--port", "8001"],
        cwd=backend_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for server to start
    time.sleep(3)
    return process


def take_screenshot() -> bool:
    """Take a screenshot of the widget using Puppeteer."""
    env = {
        "DEMO_URL": "http://localhost:3001",  # Frontend runs on 3001
    }

    print("Taking screenshot...")
    result = subprocess.run(
        ["npm", "run", "screenshot"],
        cwd=Path(__file__).parent / "frontend",
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
    """Main function to coordinate the screenshot process."""
    backend_process = None
    frontend_process = None

    try:
        # Start the backend server
        backend_process = start_backend_server()

        # Start the frontend server
        frontend_process = start_frontend_server()

        # Take screenshot
        success = take_screenshot()

        if success:
            print("\nScreenshots saved to widget/frontend/screenshots/")
            print("You can now view the MindRoom Configuration Widget appearance!")
        else:
            print("\nFailed to take screenshots.")
            sys.exit(1)

    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)
    finally:
        # Clean up: stop the servers
        if frontend_process:
            print("\nStopping frontend server...")
            frontend_process.terminate()
            frontend_process.wait()

        if backend_process:
            print("Stopping backend server...")
            backend_process.terminate()
            backend_process.wait()


if __name__ == "__main__":
    main()
