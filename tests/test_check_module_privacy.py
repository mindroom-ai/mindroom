"""Tests for ``scripts.check_module_privacy``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.check_module_privacy import find_private_candidates

if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _symbols(project_root: Path) -> set[tuple[str, str]]:
    return {(symbol.module, symbol.name) for symbol in find_private_candidates(project_root)}


def test_fastapi_route_functions_and_models_are_skipped(tmp_path: Path) -> None:
    """FastAPI route handlers and request/response models should not be flagged."""
    _write(
        tmp_path / "src" / "pkg" / "api.py",
        """
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

class RequestModel(BaseModel):
    value: int

@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

def local_helper() -> int:
    return 1
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.api", "health") not in symbols
    assert ("pkg.api", "RequestModel") not in symbols
    assert ("pkg.api", "router") not in symbols
    assert ("pkg.api", "local_helper") in symbols


def test_fastapi_related_type_aliases_and_derived_models_are_skipped(tmp_path: Path) -> None:
    """Names only referenced from route signatures/decorators should be skipped."""
    _write(
        tmp_path / "src" / "pkg" / "api.py",
        """
from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Annotated

router = APIRouter()

class BasePayload(BaseModel):
    name: str

class ExtendedPayload(BasePayload):
    age: int

RoomFilter = Annotated[str | None, Query(default=None)]

@router.get("/items", response_model=ExtendedPayload)
async def list_items(room_id: RoomFilter = None) -> ExtendedPayload:
    return ExtendedPayload(name="a", age=1)
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.api", "ExtendedPayload") not in symbols
    assert ("pkg.api", "RoomFilter") not in symbols
    assert ("pkg.api", "list_items") not in symbols


def test_typer_callbacks_are_skipped(tmp_path: Path) -> None:
    """Typer command callbacks should not be flagged."""
    _write(
        tmp_path / "src" / "pkg" / "cli.py",
        """
import typer

app = typer.Typer()

@app.command()
def run() -> None:
    pass

def local_helper() -> int:
    return 1
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.cli", "app") not in symbols
    assert ("pkg.cli", "run") not in symbols
    assert ("pkg.cli", "local_helper") in symbols


def test_logger_variable_is_ignored(tmp_path: Path) -> None:
    """Module-level logger should be ignored by privacy checks."""
    _write(
        tmp_path / "src" / "pkg" / "mod.py",
        """
from logging import getLogger

logger = getLogger(__name__)

def local_helper() -> int:
    logger.info("x")
    return 1
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.mod", "logger") not in symbols
    assert ("pkg.mod", "local_helper") in symbols


def test_pyproject_console_entrypoint_is_skipped(tmp_path: Path) -> None:
    """Console-script entrypoints in pyproject.toml should not be flagged."""
    _write(
        tmp_path / "src" / "pkg" / "cli.py",
        """
def main() -> int:
    return 0
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "pyproject.toml",
        """
[project]
name = "example"
version = "0.1.0"
scripts.example = "pkg.cli:main"
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.cli", "main") not in symbols


def test_fastapi_include_router_dependency_symbols_are_skipped(tmp_path: Path) -> None:
    """FastAPI dependency callbacks used via include_router should not be flagged."""
    _write(
        tmp_path / "src" / "pkg" / "api.py",
        """
from fastapi import APIRouter, Depends, FastAPI

app = FastAPI()
router = APIRouter()

async def verify_user() -> dict[str, str]:
    return {"id": "1"}

app.include_router(router, dependencies=[Depends(verify_user)])

def local_helper() -> int:
    return 1
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.api", "verify_user") not in symbols
    assert ("pkg.api", "local_helper") in symbols


def test_shell_uvicorn_entrypoint_is_skipped(tmp_path: Path) -> None:
    """Uvicorn entrypoints in shell scripts should not be flagged."""
    _write(
        tmp_path / "src" / "pkg" / "server.py",
        """
def build_app() -> object:
    return object()

asgi = build_app()
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "run-server.sh",
        """
#!/usr/bin/env bash
exec uvicorn pkg.server:asgi --host 0.0.0.0 --port 8000
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.server", "asgi") not in symbols
