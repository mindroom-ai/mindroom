"""Minimal FastAPI app for sandbox runner sidecar."""

import os
from pathlib import Path

from fastapi import FastAPI

from mindroom.api.sandbox_runner import router as sandbox_runner_router
from mindroom.config import Config
from mindroom.tools_metadata import ensure_tool_registry_loaded

app = FastAPI(title="MindRoom Sandbox Runner")
app.include_router(sandbox_runner_router)


@app.on_event("startup")
async def _startup() -> None:
    config_path_env = os.getenv("MINDROOM_CONFIG_PATH") or os.getenv("CONFIG_PATH")
    config: Config | None = None
    config_path: Path | None = None
    if config_path_env:
        config_path = Path(config_path_env).expanduser()
        if config_path.exists():
            config = Config.from_yaml(config_path)
    ensure_tool_registry_loaded(config, config_path=config_path)
