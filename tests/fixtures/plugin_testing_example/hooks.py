"""The example plugin's hooks, one for each kind of event the plugin testing kit fires."""

from mindroom.plugin_api import (
    AfterResponseContext,
    BeforeResponseContext,
    CancelledResponseContext,
    CustomEventContext,
    FinalResponseTransformContext,
    MessageEnrichContext,
    MessageReceivedContext,
    SystemEnrichContext,
    ToolBeforeCallContext,
    emit_custom_event,
    hook,
)


@hook("tool:before_call")
async def refuse_strangers(ctx: ToolBeforeCallContext) -> None:
    """Decline greeting someone named Stranger."""
    if ctx.arguments.get("name") == "Stranger":
        ctx.decline("Strangers are not greeted.")


@hook("message:received")
async def acknowledge(ctx: MessageReceivedContext) -> None:
    """Answer each message in its room, naming the room's history visibility."""
    room_id = ctx.envelope.room_id
    visibility = await ctx.query_room_state(room_id, "m.room.history_visibility", "")
    await ctx.send_message(room_id, f"Got: {ctx.envelope.body} ({(visibility or {}).get('history_visibility')})")


@hook("message:enrich")
async def add_greeting(ctx: MessageEnrichContext) -> None:
    """Tell the model which greeting the plugin uses."""
    ctx.add_metadata("greeting", f"Greet {ctx.envelope.requester_id} with {ctx.settings['greeting']}.")


@hook("system:enrich")
async def add_tone(ctx: SystemEnrichContext) -> None:
    """Ask the model for a friendly tone."""
    ctx.add_instruction("tone", "Be friendly.")


@hook("message:before_response")
async def sign_reply(ctx: BeforeResponseContext) -> None:
    """Sign every reply."""
    ctx.draft.response_text += " -- the greeter"


@hook("message:final_response_transform")
async def sign_streamed_reply(ctx: FinalResponseTransformContext) -> None:
    """Sign every streamed reply."""
    ctx.draft.response_text += " -- the greeter"


@hook("message:after_response")
async def note_reply(ctx: AfterResponseContext) -> None:
    """Note each reply sent."""
    await ctx.send_message(ctx.result.envelope.room_id, f"Replied: {ctx.result.response_text}")


@hook("message:cancelled")
async def note_cancelled(ctx: CancelledResponseContext) -> None:
    """Note each reply that was cancelled."""
    await ctx.send_message(ctx.info.envelope.room_id, f"Cancelled: {ctx.info.failure_reason}")


@hook("greeter:waved")
async def wave_back(ctx: CustomEventContext) -> None:
    """Answer a wave with an event of the plugin's own, as a tool's event can chain another."""
    await emit_custom_event("greeter", "greeter:waved_back", {"times": ctx.payload["times"]})


@hook("greeter:waved_back")
async def say_waved_back(ctx: CustomEventContext) -> None:
    """Say in the room that the plugin waved back."""
    if ctx.room_id is not None:
        await ctx.send_message(ctx.room_id, f"Waving back at {ctx.sender_id} for {ctx.payload['times']} waves")
