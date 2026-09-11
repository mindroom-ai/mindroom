"""Dashboard API for aggregate-only retained token usage."""

from fastapi import APIRouter, Request, Response

from mindroom.api import config_lifecycle
from mindroom.usage_stats import collect_admin_usage

__all__ = ["get_usage", "router"]

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("")
def get_usage(request: Request, response: Response) -> dict[str, object]:
    """Return retained usage by user and model under dashboard administrator auth."""
    config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    # FastAPI runs synchronous handlers in its thread pool, keeping SQLite scans
    # and report serialization off the runtime event loop.
    report = collect_admin_usage(config=config, runtime_paths=runtime_paths)
    response.headers["Cache-Control"] = "no-store"
    return report.to_dict()
