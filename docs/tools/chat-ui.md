---
icon: lucide/panels-top-left
---

# Agent Chat UI Actions

The `chat_ui` toolkit lets an agent show parts of MindRoom Chat to the user: the Computer panel (a live view of the agent's own worker browser), the Canvas panel (an interactive web page the agent writes, which the user can answer), the Members panel, or a Settings section.
Each call posts a notice in the current conversation that MindRoom Chat turns into the requested view.
Success means the request was sent, not that the user saw it.

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

The toolkit is opt-in and works only for a configured agent replying in a Matrix conversation; teams and the router cannot use it.
A subagent running as a different agent cannot use it; the replying agent must make the request.
Canvases need a separate opt-in; see [Turn canvases on](../canvases.md#turn-canvases-on).
The agent cannot choose the recipient, room, thread, or a URL: every request goes to the current conversation and is addressed to the user the agent is answering.

## Actions

None of the actions touches the user's own computer or browser:

| Call | What the user sees | What it works on | What comes back to the agent |
| --- | --- | --- | --- |
| `open_panel(panel="computer")` or `show_computer()` | Computer panel | The agent's own worker browser, the one `browser_control` drives with `target="host"`; real websites | Nothing; if the user takes control and hands it back, a message mentioning the agent |
| `show_canvas(title=None, html=None, path=None, canvas_event_id=None, share_state=False)` | Canvas panel | A web page the agent wrote; it cannot load any website | The user's confirmed answer, as their next message |
| `read_canvas_state(canvas_event_id)` | Nothing | A canvas shown with `share_state=True` | What the page last saved, as the user's Chat last shared it |
| `open_panel(panel="members")` | Members panel | The people and agents in this room | Nothing |
| `open_settings(section="general")` | Settings dialog | The user's Chat settings, opened at one section; nothing is changed | Nothing |

- `open_panel` accepts only `members` (the default) and `computer`; `show_computer()` is the same as `open_panel(panel="computer")`.
- `open_settings` accepts `general` (the default), `account`, `notifications`, `devices`, `emojis-stickers`, `developer`, or `about`.
- `show_canvas` and `read_canvas_state` exist only when canvases are turned on for the agent; [Interactive Canvases](../canvases.md) covers turning them on, answers, shared state, updates, and writing pages.

The Computer, Canvas, and Members panels share one place on the screen, so opening one replaces whichever is open.
Requests made in a thread are posted in that thread; requests made at room level stay at room level.
A sent request returns `UI action request sent.`

## Control the browser, then show it

The [`browser`](web-scraping-and-browser.md#browser) toolkit controls the agent's worker browser when routed to that worker.
Opening the Computer panel does not navigate or take control, so navigate first and then request the panel:

```python
browser_control(action="open", target="host", targetUrl="https://example.org")
chat_ui.open_panel(panel="computer")
```

To control the user's own local browser instead, use the `browser` tool's `desktop` target through the [Matrix Desktop Bridge](desktop.md).
`web_browser_tools` opens a browser on the host operating system and does not show anything in Chat.

The Computer panel needs an enabled [Worker Computer](worker-computer.md), which owns the worker, browser, proxy, and Chat configuration and describes watching, taking control, and handing back.
If the computer is unavailable, Chat shows an unavailable state in the notice.
An automatic request never replaces a session the user is currently controlling; the user can open it from the notice instead.

## What the user sees

MindRoom Chat opens the requested view automatically only when all of these hold:

- The request is addressed to this user and arrives live, not from history.
- The user has that exact room and thread open, and the Chat window is visible and focused.
- The agent's homeserver is listed in `autoOpenFromHomeservers`.

Otherwise the notice stays in the conversation with a button the user can choose later.
Chat acts only on requests from a joined `mindroom_` agent account on the user's own homeserver.
Other Matrix clients show the notice's text, such as "Open Settings (general) in MindRoom Chat."

The Chat deployment controls automatic opening in its runtime `config.json`:

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
An absent or empty list keeps every request behind its button.
Entries match the exact server name in the agent's Matrix ID, including any port; URLs, wildcards, and subdomain matching are unsupported.
Operators must reserve and control the agent username namespace on any homeserver they allow, as `mindroom.chat` does.
