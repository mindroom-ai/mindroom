"""Drive an approval continuation's claim and advance directly, without the reply span production pairs them with."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.event_journal import approval_continuations
from mindroom.event_journal.approval_continuations import ApprovalAdvance

if TYPE_CHECKING:
    from mindroom.event_journal import PrincipalStore
    from mindroom.event_journal.approval_continuations import ApprovalCall, ApprovalContinuation


async def claim_continuation(
    principal: PrincipalStore,
    approval_id: str,
    *,
    runtime_generation: str,
    legacy_show_tool_calls: bool | None = None,
) -> ApprovalContinuation | None:
    """Claim one ready paused run for exactly one execution attempt, as an approval resume's claim does."""
    return await principal._backend.write(
        lambda transaction: approval_continuations.claim(
            transaction,
            principal.principal_id,
            approval_id=approval_id,
            runtime_generation=runtime_generation,
            legacy_show_tool_calls=legacy_show_tool_calls,
        ),
    )


async def advance_continuation(
    principal: PrincipalStore,
    approval_id: str,
    *,
    claimant_generation: int,
    run_id: str,
    session_id: str,
    calls: tuple[ApprovalCall, ...],
    runtime_model_name: str | None = None,
    delegation_storage_bindings: dict[str, dict[str, object]] | None = None,
    cli_call: dict[str, object] | None = None,
    continuation_count: int | None = None,
) -> ApprovalContinuation | None:
    """Replace one claimed generation with the next exact Agno pause, as a further pause does."""
    advance = ApprovalAdvance(
        approval_id=approval_id,
        claimant_generation=claimant_generation,
        run_id=run_id,
        session_id=session_id,
        calls=calls,
        runtime_model_name=runtime_model_name,
        delegation_storage_bindings=delegation_storage_bindings,
        cli_call=cli_call,
        continuation_count=continuation_count,
    )
    return await principal._backend.write(lambda transaction: advance.apply(transaction, principal.principal_id))
