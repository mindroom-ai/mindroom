#!/usr/bin/env python3
"""
Simple screenshot script that assumes servers are already running.
Run ./widget/run.sh first, then run this script.
"""

import os
import subprocess
import sys
from pathlib import Path


def check_servers() -> tuple[int, int]:
    """Check which ports the servers are running on."""
    import socket

    frontend_port = None
    backend_port = None

    # Check common frontend ports
    for port in [3000, 3001, 3002, 3003, 3004, 3005, 3006]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(("localhost", port))
        sock.close()
        if result == 0:
            # Quick check if it's our app by looking for the title
            try:
                import urllib.request

                response = urllib.request.urlopen(f"http://localhost:{port}", timeout=1)
                content = response.read().decode("utf-8")
                if "MindRoom" in content:
                    frontend_port = port
                    break
            except Exception:
                pass

    # Check backend port
    for port in [8001, 8080]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(("localhost", port))
        sock.close()
        if result == 0:
            backend_port = port
            break

    return frontend_port, backend_port


def take_screenshot(port: int = 3000) -> bool:
    """Take a screenshot of the widget using Puppeteer."""
    env = {
        "DEMO_URL": f"http://localhost:{port}",
    }

    print(f"Taking screenshot of app at http://localhost:{port}...")
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
    """Main function to take screenshots."""
    print("Checking for running servers...")
    frontend_port, backend_port = check_servers()

    if not frontend_port:
        print("\n❌ Frontend server not running!")
        print("Please run: ./widget/run.sh")
        sys.exit(1)

    if not backend_port:
        print("\n⚠️  Backend server not running. Screenshots may show errors.")

    print(f"✅ Found frontend on port {frontend_port}")
    if backend_port:
        print(f"✅ Found backend on port {backend_port}")

    # Take screenshot
    success = take_screenshot(frontend_port)

    if success:
        print("\n📸 Screenshots saved to widget/frontend/screenshots/")
        print("You can now view the MindRoom Configuration Widget appearance!")
    else:
        print("\n❌ Failed to take screenshots.")
        sys.exit(1)


if __name__ == "__main__":
    main()
