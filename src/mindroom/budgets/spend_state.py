"""Month-to-date spend a clean, idle stop keeps beside the caps, so the start right after it needs no full scan."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from mindroom.budgets.pricing import model_prices
from mindroom.budgets.spend import SpendSnapshot  # noqa: TC001 - pydantic resolves SpendState's fields at runtime
from mindroom.constants import resolve_session_state_root
from mindroom.durable_write import write_json_file_durable
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


class SpendState(BaseModel):
    """The latest completed scan, the fingerprint of the settings it counted with, and the reply costs it does not cover."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    fingerprint: str
    snapshot: SpendSnapshot
    reply_costs: dict[str, float]


def spend_fingerprint(config: Config, runtime_paths: RuntimePaths) -> str:
    """Return a digest of whether budgets are on and of the settings that change what a scan counts.

    Those are prices, the session stores a scan reads, and the agents, teams, bot accounts, internal user, and aliases.
    """
    prices = [
        [provider, model_id, priced.pricing.model_dump(mode="json"), priced.input_includes_cache]
        for (provider, model_id), priced in sorted(model_prices(config).items())
    ]
    payload = [
        config.budgets is not None,
        prices,
        str(resolve_session_state_root(runtime_paths.storage_root, runtime_paths).expanduser().resolve()),
        sorted((name, agent.private is not None) for name, agent in config.agents.items()),
        sorted(config.teams),
        sorted(config.bot_accounts),
        config.mindroom_user.username if config.mindroom_user is not None else None,
        {canonical: sorted(aliases) for canonical, aliases in config.authorization.aliases.items()},
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def discard_spend_state(path: Path) -> None:
    """Delete the state at ``path``, or overwrite it with state no start accepts when it cannot be deleted."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("budget_spend_state_not_deleted", path=str(path), exc_info=True)
        write_json_file_durable(path, {"version": 0})


def read_spend_state(path: Path) -> SpendState | None:
    """Return the state a clean stop kept at ``path``, or None when a scan must count spend instead."""
    try:
        return SpendState.model_validate_json(path.read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.warning("budget_spend_state_unusable", path=str(path), exc_info=True)
        return None
