---
icon: lucide/panels-top-left
---

# Agent Chat UI Actions

The `chat_ui` toolkit opens MindRoom Chat UI for the user: show the agent's worker browser in the Computer panel, open Settings, or show room members.
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

- `open_panel(panel="computer")` asks Chat to reveal the current agent's worker browser in the Computer panel in watch mode.
- `open_panel(panel="members")` requests the room's Members side panel and remains the default when `panel` is omitted.
  `computer` and `members` are the only supported panel values; `browser` is not a panel value.
- `show_computer()` remains a backward-compatible alias for `open_panel(panel="computer")`.
- `open_settings(section="general")` separately requests `general`, `account`, `notifications`, `devices`, `emojis-stickers`, `developer`, or `about` without changing account settings.

Each call sends an `m.room.message` notice in the current conversation.
Threaded calls use the runtime's canonical thread root; room-level calls remain at room level.
The Matrix event sender must equal the metadata's agent identity, or the request is rejected before sending.
Both Computer entry points retain the existing `action: "show_computer"` transport metadata and result action for compatibility with existing clients.
Members retains `action: "open_panel", panel: "members"`; Settings retains `action: "open_settings"` and its `section`.

## Control the browser, then show it

The [`browser`](web-scraping-and-browser.md#browser) toolkit controls the agent's worker browser when routed to that worker.
To let the user watch this worker browser, use `chat_ui.open_panel(panel='computer')`.
If the user asks to visit a page and show it, navigate separately before requesting the panel:

```python
browser_control(action="open", target="host", targetUrl="https://example.org")
chat_ui.open_panel(panel="computer")
```

Opening Computer only requests display of the worker browser.
It does not navigate, send a prompt to ChatGPT, take control, or open or control the user's local browser.
It does not send a chat prompt or mutate an account; the Matrix notice carries only the UI request.
The user's connected local browser uses the separately configured `browser` desktop target and [Matrix Desktop Bridge](desktop.md).
`web_browser_tools` instead asks the host operating system to open a browser; it does not reveal a worker browser in Chat.
Panel calls accept no `url` or `targetUrl`; browser navigation belongs in `browser_control`.
Success returns `UI action request sent.`, which confirms delivery of the request without confirming that any panel opened.

## What the user sees

MindRoom Chat automatically acts only for the addressed user when that exact room and thread are active, the client is visible and focused, the request arrived as a current live event, and the deployment explicitly trusts the agent's homeserver for automatic opening.
When the conversation is inactive, the event is historical, or automatic opening is otherwise unavailable, the notice remains in the conversation with a button the addressed user can choose later.
A dismissed request stays dismissed, while a new event can request the action again.

Other Matrix clients display the notice's fallback text, such as “Open Settings (general) in MindRoom Chat.”
Receiving or sending the notice is not evidence that a client opened anything.

The Chat deployment controls automatic opening through its runtime `config.json`:

```json
{
  "mindroom": {
    "uiActions": {
      "autoOpenFromHomeservers": ["mindroom.chat"]
    }
  }
}
```

The shipped MindRoom Chat configuration lists `mindroom.chat`.
An absent or empty list keeps requests passive with explicit buttons.
Entries match the exact server name in the agent's Matrix ID, including any port; URLs, wildcards, and implicit subdomain matching are unsupported.
The existing joined, same-homeserver `mindroom_` identity checks still apply.
Operators must reserve and control the agent username namespace on any homeserver they allow, as `mindroom.chat` does.
UI requests reveal only the bounded Chat surfaces described above; worker computer access remains independently authorized by the configured computer gateway.

## Worker computer requirements

`open_panel(panel="computer")` and its `show_computer()` alias use the existing MindRoom worker computer feature; `chat_ui` does not configure or expose a computer gateway.
The target agent still needs a dedicated Docker or Kubernetes worker, effective `worker_scope: user_agent`, worker-routed browser tools, and an operator-configured Chat computer API origin.
See [Worker Computer](worker-computer.md) for the backend, worker, proxy, and Chat configuration.

If the computer gateway or requesting agent is unavailable, Chat keeps the request visible and shows an unavailable state instead of using an event-provided endpoint.
When a worker computer is already under human control, an automatic request does not replace that session; the notice remains available for the user to choose.
Opening the worker from a request starts watch mode, and another request for the same agent preserves the current worker view instead of resetting it.
