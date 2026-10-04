---
icon: lucide/panels-top-left
---

# Agent Chat UI Actions

The `chat_ui` toolkit shows parts of MindRoom Chat to the user: the Computer panel (a live view of the agent's own worker browser), the Canvas panel (an interactive web page the agent writes, which the user can answer), the Members panel, or a Settings section.
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
Canvases are a separate opt-in inside it; see [Turning canvases on](#turning-canvases-on).
It does not accept a URL, credential, API endpoint, DOM selector, Matrix identity, room ID, or thread ID from the model.
The only identifiers it accepts are a canvas's own event ID, to update that canvas, and a workspace file path for a canvas page, read under the agent's `file_access`.
MindRoom supplies the addressed requester, configured agent sender, current room, and canonical thread from the active turn.
Delegated transport identities and team contexts are rejected because they cannot identify one Matrix sender and one agent worker safely.

## Actions

Each action works on a different thing, and none of them touches the user's own computer or browser:

| Call | What the user sees | What it works on | What comes back to the agent |
| --- | --- | --- | --- |
| `open_panel(panel="computer")` or `show_computer()` | Computer panel | The agent's own worker browser, the one `browser_control` drives with `target="host"`; real websites | Nothing; if the user takes control and hands it back, a message mentioning the agent |
| `show_canvas(...)`, opt-in with `enable_show_canvas` | Canvas panel | A web page the agent wrote; it cannot load any website | The user's confirmed answer, as their next message |
| `open_panel(panel="members")` | Members panel | The people and agents in this room | Nothing |
| `open_settings(section=...)` | Settings dialog | The user's Chat settings, opened at one section; nothing is changed | Nothing |

The Computer, Canvas, and Members panels share one place on the screen, so opening one replaces whichever is open.
The toolkit adds the same map to the agent's instructions on every turn, listing only the functions the agent has after `include_tools` or `exclude_tools`.

- `open_panel(panel="computer")` asks Chat to reveal the current agent's worker browser in the Computer panel in watch mode.
- `open_panel(panel="members")` requests the room's Members side panel and remains the default when `panel` is omitted.
  `computer` and `members` are the only supported panel values; `browser` is not a panel value.
- `show_computer()` remains a backward-compatible alias for `open_panel(panel="computer")`.
- `open_settings(section="general")` separately requests `general`, `account`, `notifications`, `devices`, `emojis-stickers`, `developer`, or `about` without changing account settings.
- `show_canvas(title, html=None, path=None, canvas_event_id=None)` shows an agent-made web page, such as a dashboard, slides, or a form, in a side panel and receives the user's answer; see [Interactive canvases](#interactive-canvases). It is available only when `enable_show_canvas` is true for the agent; see [Turning canvases on](#turning-canvases-on).

Each call sends an `m.room.message` notice in the current conversation; a canvas update sends an `m.replace` edit of the canvas's notice instead.
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

## Interactive canvases

`show_canvas` lets an agent show a web page beside the conversation and read what the user chose.
Use it when seeing or clicking beats reading or typing: dashboards, reports, charts, slides, menus, forms, pickers, and multi-step flows.
It is off by default on both sides; see [Turning canvases on](#turning-canvases-on).

```python
chat_ui.show_canvas(
    title="Choose a plan",
    html="""
<style>
  .plans { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }
  .plan { background: var(--mr-surface); border: 1px solid var(--mr-border);
          border-radius: var(--mr-radius); padding: 16px; }
  button { background: var(--mr-accent); color: var(--mr-accent-text); border: 0;
           border-radius: 8px; padding: 8px 14px; cursor: pointer; }
</style>
<div class="plans">
  <div class="plan"><h3>Basic</h3><p>For trying things out.</p>
    <button onclick="mindroom.submit({plan: 'basic'}, {label: 'Basic plan'})">Choose Basic</button></div>
  <div class="plan"><h3>Pro</h3><p>For daily work.</p>
    <button onclick="mindroom.submit({plan: 'pro'}, {label: 'Pro plan'})">Choose Pro</button></div>
</div>
""",
)
```

### Pages and files

Pass the page as `html`, or as a workspace-relative `path` (for example `slides/deck.html`) to an HTML file in the agent's workspace.
A path suits pages the agent builds and refines, such as a slide deck: edit the file, then call `show_canvas` again with the same `path` and the canvas ID to show the new version.
Paths follow the agent's `file_access` setting, like [`matrix_message` attachments](matrix-message.md): with the default `workspace` access they must stay inside the workspace, and with `unrestricted` access any file the MindRoom process can read can be shown.
The file is read by the MindRoom process, so `~` means that process's home rather than a worker's home; prefer workspace-relative paths.
The page must be UTF-8 text and at most 4 MB.

Agents should only show pages they wrote.
A canvas always appears as the agent's own, so showing a downloaded page or a file from an untrusted source would present someone else's page, with the same risk that what the user types into it can leave the panel.

### Design

Canvases are full web pages, so agents can build polished dashboards and reports.
Write self-contained HTML with inline CSS and JavaScript; external scripts, styles, fonts, images, and network requests are blocked, so draw charts with inline SVG (or a canvas element) and embed images as `data:` URLs.
The user can resize the panel and expand it to the full width of the conversation, so use a responsive layout.

MindRoom Chat exposes its current theme as CSS variables, so a page can match light and dark mode without guessing colors:

| Variable | Use |
|---|---|
| `--mr-bg` | Page background (the default body background) |
| `--mr-surface`, `--mr-surface-raised` | Cards and raised elements |
| `--mr-border` | Borders and dividers |
| `--mr-text`, `--mr-text-muted` | Text (the default body color) and secondary text |
| `--mr-accent`, `--mr-accent-text` | Primary actions and the text on them |
| `--mr-success`, `--mr-warning`, `--mr-danger` | Status colors |
| `--mr-radius`, `--mr-font` | Corner radius and font family |

### How the answer reaches the agent

Everything the user does inside the canvas stays there: typing, toggling, dragging, and moving between steps send nothing.
When the page calls `window.mindroom.submit(data, {label})`, or the user submits a `<form>` (its fields become the data and its `data-mindroom-label` attribute the label), MindRoom Chat shows what will be sent and the user confirms with **Send**.
While an answer waits for confirmation, the page cannot change it; the user discards it to choose again.
The answer is an ordinary threaded message from the user that mentions the agent, so it starts the agent's next turn like any reply:

```text
@mindroom_planner:example.org Canvas response ($canvas-event, revision $canvas-event): Pro plan
{"plan":"pro"}
```

MindRoom Chat displays that message as a one-line receipt (expandable to the exact data) and carries the same values in `io.mindroom.canvas_response`; other Matrix clients show the text.
`data` must be JSON-serializable and at most 512 KB, enough for a long document the user edited in the canvas.
An answer too large for one Matrix event is sent the way MindRoom sends long replies: the event is a short preview, and the whole message is an uploaded file (encrypted in encrypted rooms) that MindRoom downloads before the agent reads it, so the agent still receives one ordinary message.
Numbers that are not whole (or are too large for a JSON integer) arrive as text, because homeservers refuse them in unencrypted events, and object keys arrive sorted.

### Updating a canvas in place

`show_canvas` returns the canvas `event_id`.
To show the next step of the same flow, call `show_canvas` again with `canvas_event_id` set to that ID.
MindRoom sends a Matrix edit of the original notice, so the timeline keeps one card and an open panel switches to the new page.
If the user has been working in the panel and has not sent anything since, Chat asks before replacing it.
An agent can update only its own canvases for the same person in the same room and thread.
A canvas shown outside a thread can be updated from any thread of that room, because the user's answer to it starts a thread.

### Size

A page whose edit fits a Matrix event (27,000 bytes of serialized event, about 24,000 characters of plain ASCII HTML) travels inside the event.
A larger page, up to 4 MB, is uploaded as Matrix media (encrypted in end-to-end encrypted rooms) and the event carries only a reference that MindRoom Chat downloads, decrypts, and caches.
Every update of a large page uploads a new copy, and Matrix does not let users delete uploaded media, so iterate on large pages sparingly.
The tool reports an error for pages above 4 MB or when the upload fails.

### Sandbox and limits

MindRoom Chat runs the page in a sandboxed frame with its own opaque origin:

- It cannot read the user's Matrix account, messages, cookies, or storage, and it cannot open pop-ups or navigate Chat.
- Its Content Security Policy blocks ordinary network requests (fetch, images, scripts, styles) and external resources; use inline CSS, inline JavaScript, inline SVG, and `data:` URLs.
- Chat blocks the frame from navigating to other sites.
- Nothing is posted to the conversation without the user's explicit **Send**.

These protections do not make a canvas a safe place for secrets.
Browsers do not let a page block WebRTC, so a malicious canvas could still leak what the user types into it.
Chat therefore shows which agent made each canvas and reminds the user that what they enter may leave the panel.
Chat does not run canvases during a call, because call frames listen to the page's messages.

### Turning canvases on

Canvases need two switches, and both are off by default.

The agent gets `show_canvas` only when `enable_show_canvas` is true for it; otherwise it neither has the function nor sees it in its instructions.
Set it in the agent's `chat_ui` entry:

```yaml
agents:
  researcher:
    tools:
      - chat_ui:
          enable_show_canvas: true
```

Like other tool options, it can also be set in the `chat_ui` tool's saved settings, which apply to every agent that uses `chat_ui`.
An agent's own entry wins, so `enable_show_canvas: false` there keeps canvases away from that agent even when the saved setting is on.

MindRoom Chat shows canvases only when the deployment enables them in its runtime `config.json`:

```json
{
  "mindroom": {
    "canvas": {
      "enabled": true
    }
  }
}
```

When they are off, the notice shows its text fallback and its button explains that interactive panels are turned off.
Automatic opening follows the same `autoOpenFromHomeservers` rule as other UI requests; otherwise the user opens the canvas with the notice's **Open panel** button.


## Worker computer requirements

`open_panel(panel="computer")` and its `show_computer()` alias use the existing MindRoom worker computer feature; `chat_ui` does not configure or expose a computer gateway.
The target agent still needs a dedicated Docker or Kubernetes worker, effective `worker_scope: user_agent`, worker-routed browser tools, and an operator-configured Chat computer API origin.
See [Worker Computer](worker-computer.md) for the backend, worker, proxy, and Chat configuration.

If the computer gateway or requesting agent is unavailable, Chat keeps the request visible and shows an unavailable state instead of using an event-provided endpoint.
When a worker computer is already under human control, an automatic request does not replace that session; the notice remains available for the user to choose.
Opening the worker from a request starts watch mode, and another request for the same agent preserves the current worker view instead of resetting it.
