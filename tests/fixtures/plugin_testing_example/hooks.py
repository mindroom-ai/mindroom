"""The example plugin's hooks: one declines greeting a stranger, one answers each message it receives."""

from mindroom.plugin_api import MessageReceivedContext, ToolBeforeCallContext, hook


@hook("tool:before_call")
async def refuse_strangers(ctx: ToolBeforeCallContext) -> None:
    """Decline greeting someone named Stranger."""
    if ctx.arguments.get("name") == "Stranger":
        ctx.decline("Strangers are not greeted.")


@hook("message:received")
async def acknowledge(ctx: MessageReceivedContext) -> None:
    """Answer each message in its room."""
    await ctx.send_message(ctx.envelope.room_id, f"Got: {ctx.envelope.body}")
