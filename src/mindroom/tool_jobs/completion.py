"""Late admission of stored job outcomes under the real response lifecycle lock."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_jobs.runtime import get_background_runtime

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import MessageEnvelope
    from mindroom.message_target import MessageTarget


async def admit_job_completion(
    envelope: MessageEnvelope,
    *,
    target: MessageTarget,
    runtime_paths: RuntimePaths,
) -> bool:
    """Suppress malformed, stale, consumed, revoked or misrouted completion claims."""
    if envelope.hook_source != "tool_job_completion":
        return True
    reference = envelope.tool_job_completion
    if reference is None:
        return False
    runtime = get_background_runtime(runtime_paths)
    if runtime is None:
        msg = "Tool job runtime is not ready for completion admission"
        raise RuntimeError(msg)
    job = await runtime.delivery_outcome(reference.job_id, reference.generation, reference.transaction_id)
    if job is None:
        return False
    owner = job.owner
    delivery = job.delivery
    return (
        delivery is not None
        and delivery.content.get("m.mentions") == {"user_ids": [reference.recipient_user_id]}
        and envelope.sender_id == reference.recipient_user_id
        and (delivery.event_id is None or delivery.event_id == envelope.source_event_id)
        and envelope.requester_id == owner.requester_id
        and envelope.agent_name == (owner.transport_agent_name or owner.agent_name)
        and target.room_id == owner.room_id
        and target.resolved_thread_id == owner.resolved_thread_id
        and target.session_id == owner.session_id
    )
