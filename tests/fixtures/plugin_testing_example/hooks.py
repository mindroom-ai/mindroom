"""The example plugin's hooks, one for each kind of event the plugin testing kit fires."""

from mindroom.plugin_api import (
    BeforeResponseContext,
    CustomEventContext,
    FinalResponseTransformContext,
    MessageEnrichContext,
    MessageReceivedContext,
    ToolBeforeCallContext,
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


@hook("message:before_response")
async def sign_reply(ctx: BeforeResponseContext) -> None:
    """Sign every reply."""
    ctx.draft.response_text += " -- the greeter"


@hook("message:final_response_transform")
async def sign_streamed_reply(ctx: FinalResponseTransformContext) -> None:
    """Sign every streamed reply."""
    ctx.draft.response_text += " -- the greeter"


@hook("greeter:waved")
async def wave_back(ctx: CustomEventContext) -> None:
    """Wave back at whoever waved."""
    if ctx.room_id is not None:
        await ctx.send_message(ctx.room_id, f"Waving back at {ctx.sender_id} for {ctx.payload['times']} waves")
