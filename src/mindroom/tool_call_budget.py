"""Per-turn tool-call budget: refuse calls past the limit, grant one closing reply, then end the run."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.agno_compat_model_hooks import install_tool_call_limit_stop
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from agno.models.base import Model

logger = get_logger(__name__)

_HOOK_ATTR = "_mindroom_tool_call_budget_hook_installed"


def install_tool_call_budget(model: Model, *, entity_name: str) -> None:
    """Make the owning agent's or team's ``tool_call_limit`` end runs that keep requesting tools.

    Agno refuses every call past the limit; the model then gets one more response to
    write its closing reply, and a run whose model requests tools again ends there.
    """

    def warn(limit: int) -> None:
        logger.warning("tool_call_limit_reached", entity=entity_name, limit=limit)

    install_tool_call_limit_stop(model, marker=_HOOK_ATTR, on_limit_reached=warn)
