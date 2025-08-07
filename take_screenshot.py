#!/usr/bin/env python3
"""
Script to take screenshots of the MindRoom Configuration Widget.
Run from the project root: python take_screenshot.py
"""

import os
import subprocess
import sys
from pathlib import Path

# Get the frontend port from environment or use default
frontend_port = os.environ.get("FRONTEND_PORT", "3003")

# Simply delegate to the widget's screenshot script with the port
widget_script = Path(__file__).parent / "widget" / "take_screenshot.py"

if widget_script.exists():
    subprocess.run([sys.executable, str(widget_script), frontend_port])
else:
    print("Widget screenshot script not found!")
    print("Please ensure the widget is properly set up.")
    sys.exit(1)
