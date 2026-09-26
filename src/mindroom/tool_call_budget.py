"""Per-run model-call cap derived from the per-turn tool-call budget, and scoped caller-owned request gates."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from mindroom.agno_compat_model_hooks import install_response_request_gate
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agno.models.base import Model

logger = get_logger(__name__)

_GATE_ATTR = "_mindroom_model_call_cap_installed"
_REQUEST_GATE_ATTR = "_mindroom_request_gate_installed"
_scoped_gate: ContextVar[Callable[[], bool] | None] = ContextVar("scoped_request_gate", default=None)
# Every tool-calling request spends at least one budgeted call, so a run within its budget needs at
# most budget + 1 requests; one more lets the model answer after Agno refuses its first batch.
_EXTRA_MODEL_REQUESTS = 2


def install_model_call_cap(model: Model, *, entity_name: str) -> None:
    """End every Agno run of this model after its ``tool_call_limit`` plus two model requests.

    Agno's ``tool_call_limit`` refuses counted tool calls but never ends a run, and calls to unknown
    tools or with unparseable arguments bypass it entirely. The cap counts each response loop's model
    requests instead, so every run ends whatever the model keeps asking for: the request past the cap
    never reaches the provider, and the run completes with the text produced so far.
    Each Agno response loop counts afresh, matching the scope of Agno's own tool-call count, so a
    continuation that starts a new loop gets a new count.
    """

    def open_run(tool_call_limit: int | None) -> Callable[[], bool] | None:
        if tool_call_limit is None:
            return None
        cap = tool_call_limit + _EXTRA_MODEL_REQUESTS
        requests = 0

        def allow_request() -> bool:
            nonlocal requests
            if requests < cap:
                requests += 1
                return True
            # Agno's loop ends after the refused request, so this logs once per run.
            logger.warning(
                "tool_call_limit_reached",
                entity=entity_name,
                budget=tool_call_limit,
                model_requests=requests,
            )
            return False

        return allow_request

    install_response_request_gate(model, marker=_GATE_ATTR, open_gate=open_run)


@contextmanager
def request_gate(model: Model, allow_request: Callable[[], bool]) -> Iterator[None]:
    """Ask ``allow_request`` before each model request of the response loops started inside the block.

    A refused request ends the run. The model keeps the gate installed, but loops started outside the block, such as
    later runs of the model, are never asked.
    """
    install_response_request_gate(model, marker=_REQUEST_GATE_ATTR, open_gate=lambda _limit: _scoped_gate.get())
    token = _scoped_gate.set(allow_request)
    try:
        yield
    finally:
        _scoped_gate.reset(token)
