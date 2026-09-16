# Agent Chat UI Actions

The `chat_ui` tool lets an agent ask MindRoom Chat to reveal its worker computer, open a Settings section, or open the room's Members panel.
It sends a normal Matrix notice with a readable fallback and bounded action metadata.
The tool reports that the request was sent; it cannot know whether a client opened the requested interface.

## Enable the tool

Add `chat_ui` to the agent that should be allowed to request UI actions:

```yaml
agents:
  researcher:
    display_name: Researcher
    role: Research topics with browser and shell tools.
    model: default
    tools:
      - chat_ui
      - browser
      - shell
```

The toolkit is opt-in and requires a live Matrix room context.
It does not accept a URL, credential, API endpoint, DOM selector, Matrix identity, room ID, or thread ID from the model.
MindRoom supplies the addressed requester, configured agent sender, current room, and canonical thread from the active turn.
Delegated transport identities and team contexts are rejected because they cannot identify one Matrix sender and one agent worker safely.

## Actions

- `show_computer()` asks Chat to reveal the requesting agent's worker computer in watch mode.
- `open_settings(section="general")` opens `general`, `account`, `notifications`, `devices`, `emojis-stickers`, `developer`, or `about`.
- `open_panel(panel="members")` opens the Members side panel. `members` is the only supported panel in this release.

Each call sends an `m.room.message` notice in the current conversation.
Threaded calls use the runtime's canonical thread root; room-level calls remain at room level.
The Matrix event sender must equal the metadata's agent identity, or the request is rejected before sending.

## What the user sees

MindRoom Chat automatically acts only for the addressed user when that exact room and thread are active, the client is visible and focused, and the request arrived as a current live event.
When the conversation is inactive, the event is historical, or automatic opening is otherwise unavailable, the notice remains in the conversation with a button the addressed user can choose later.
A dismissed request stays dismissed, while a new event can request the action again.

Other Matrix clients display the notice's fallback text, such as “Open Settings (general) in MindRoom Chat.”
Receiving or sending the notice is not evidence that a client opened anything.

## Worker computer requirements

`show_computer()` uses the existing MindRoom worker computer feature; `chat_ui` does not configure or expose a computer gateway.
The target agent still needs a dedicated Docker or Kubernetes worker, effective `worker_scope: user_agent`, worker-routed browser tools, and an operator-configured Chat computer API origin.
See [Worker Computer](https://docs.mindroom.chat/tools/worker-computer/) for the backend, worker, proxy, and Chat configuration.

If the computer gateway or requesting agent is unavailable, Chat keeps the request visible and shows an unavailable state instead of using an event-provided endpoint.
When a worker computer is already under human control, an automatic request does not replace that session; the notice remains available for the user to choose.
Opening the worker from a request starts watch mode, and another request for the same agent preserves the current worker view instead of resetting it.
