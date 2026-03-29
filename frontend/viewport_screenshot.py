import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8766/integrations"
OUT_DIR = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/viewport-screenshots")
OUT_DIR.mkdir(exist_ok=True)

VIEWPORTS = [
    (375, 812, "mobile-375"),
    (500, 900, "narrow-500"),
    (768, 1024, "tablet-768"),
    (1280, 720, "desktop-1280"),
]

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        executable_path="/run/current-system/sw/bin/chromium",
        args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
    )
    for width, height, label in VIEWPORTS:
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(URL, wait_until="networkidle")
        page.wait_for_timeout(1000)
        path = OUT_DIR / f"{label}.png"
        page.screenshot(path=str(path), full_page=True)
        print(f"{label}: {width}x{height} -> {path}")
        page.close()
    browser.close()
    print(f"All screenshots in {OUT_DIR}")
