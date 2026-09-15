# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir bundle for the fixed-identity desktop helper."""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

root = Path(SPECPATH).parent
hiddenimports = collect_submodules("mindroom.desktop") + collect_submodules("mindroom.matrix")
datas = []
binaries = []
for package in ("mcp", "nio", "olm", "pyautogui", "PIL"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

analysis = Analysis(
    [str(root / "src/mindroom/desktop/native_entry.py")],
    pathex=[str(root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + ["ApplicationServices", "Cocoa", "Quartz", "ScreenCaptureKit"],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="MindRoom Desktop Helper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    target_arch=os.environ.get("MINDROOM_HELPER_TARGET_ARCH") or None,
)
collected = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="MindRoom Desktop Helper",
)
app = BUNDLE(
    collected,
    name="MindRoom Desktop Helper.app",
    icon=None,
    bundle_identifier="chat.mindroom.desktophelper",
    info_plist={
        "CFBundleDisplayName": "MindRoom Desktop Helper",
        "CFBundleName": "MindRoom Desktop Helper",
        "CFBundleVersion": "1",
        "LSBackgroundOnly": True,
        "LSMinimumSystemVersion": "14.0",
        "NSAppleEventsUsageDescription": "MindRoom controls only the applications you explicitly allow.",
    },
)
