"""Open a persistent Cinny browser profile for one-time manual login."""
# ruff: noqa: N999

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.browser import _DEFAULT_PROFILE, _VIEWPORT_HEIGHT, _VIEWPORT_WIDTH, _profile_dir


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-path", type=Path, required=True, help="MindRoom config.yaml path")
    parser.add_argument("--storage-path", type=Path, help="Override the runtime storage root")
    parser.add_argument("--profile", default=_DEFAULT_PROFILE, help="Browser profile name")
    parser.add_argument("--url", required=True, help="Cinny room or homeserver URL to open")
    return parser.parse_args()


def main() -> int:
    """Launch the headed persistent Chromium context for one manual login flow."""
    args = _parse_args()
    runtime_paths = resolve_runtime_paths(
        config_path=args.config_path,
        storage_path=args.storage_path,
        process_env={},
    )
    user_data_dir = _profile_dir(runtime_paths, args.profile)
    executable = (
        runtime_paths.env_value("BROWSER_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("google-chrome-stable")
    )
    launch_kwargs: dict[str, Any] = {
        "headless": False,
        "service_workers": "block",
        "user_data_dir": str(user_data_dir),
        "viewport": {"height": _VIEWPORT_HEIGHT, "width": _VIEWPORT_WIDTH},
    }
    if executable:
        launch_kwargs["executable_path"] = executable

    print(f"user_data_dir: {user_data_dir}", flush=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(**launch_kwargs)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(args.url, wait_until="domcontentloaded")
            input("Press Enter after the Cinny room timeline is visible...")
        finally:
            context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
