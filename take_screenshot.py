#!/usr/bin/env python3
"""
Script to take screenshots of the MindRoom Configuration Widget.
Run from the project root: python take_screenshot.py
"""

import subprocess
import sys
from pathlib import Path

# Simply delegate to the widget's screenshot script
widget_script = Path(__file__).parent / "widget" / "take_screenshot.py"

if widget_script.exists():
    subprocess.run([sys.executable, str(widget_script)])
else:
    print("Widget screenshot script not found!")
    print("Please ensure the widget is properly set up.")
    sys.exit(1)
