"""Month-to-date spend a clean stop keeps beside the caps, so a start soon after needs no full scan.

The next start reads and deletes the file, so a crash or a later start can never reuse it.
"""

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


def keep_spend_state(path: Path, state: SpendState | None) -> None:
    """Write ``state`` atomically for the next start, or delete any kept state when there is none to keep."""
    if state is None:
        path.unlink(missing_ok=True)
    else:
        write_json_file_durable(path, state.model_dump(mode="json"))


def take_spend_state(path: Path) -> SpendState | None:
    """Read and delete the state a clean stop kept, or return None when a scan must count spend instead."""
    try:
        data = path.read_bytes()
        path.unlink()
        return SpendState.model_validate_json(data)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # A file that cannot be deleted could be read again after a crash, so it is never resumed either.
        logger.warning("budget_spend_state_unusable", path=str(path), exc_info=True)
        return None
