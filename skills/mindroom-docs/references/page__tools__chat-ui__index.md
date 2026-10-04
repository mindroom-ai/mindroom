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
Canvases need a separate opt-in; see [Turning canvases on](#turning-canvases-on).
The agent cannot choose the recipient, room, thread, or a URL: every request goes to the current conversation and is addressed to the user the agent is answering.

## Actions

None of the actions touches the user's own computer or browser:

| Call | What the user sees | What it works on | What comes back to the agent |
| --- | --- | --- | --- |
| `open_panel(panel="computer")` or `show_computer()` | Computer panel | The agent's own worker browser, the one `browser_control` drives with `target="host"`; real websites | Nothing; if the user takes control and hands it back, a message mentioning the agent |
| `show_canvas(title, html=None, path=None, canvas_event_id=None)` | Canvas panel | A web page the agent wrote; it cannot load any website | The user's confirmed answer, as their next message |
| `open_panel(panel="members")` | Members panel | The people and agents in this room | Nothing |
| `open_settings(section="general")` | Settings dialog | The user's Chat settings, opened at one section; nothing is changed | Nothing |

- `open_panel` accepts only `members` (the default) and `computer`; `show_computer()` is the same as `open_panel(panel="computer")`.
- `open_settings` accepts `general` (the default), `account`, `notifications`, `devices`, `emojis-stickers`, `developer`, or `about`.
- `show_canvas` exists only when canvases are turned on for the agent; see [Interactive canvases](#interactive-canvases).

The Computer, Canvas, and Members panels share one place on the screen, so opening one replaces whichever is open.
Requests made in a thread are posted in that thread; requests made at room level stay at room level.
A sent request returns `UI action request sent.`

## Control the browser, then show it

The [`browser`](https://docs.mindroom.chat/tools/web-scraping-and-browser/#browser) toolkit controls the agent's worker browser when routed to that worker.
Opening the Computer panel does not navigate or take control, so navigate first and then request the panel:

```python
browser_control(action="open", target="host", targetUrl="https://example.org")
chat_ui.open_panel(panel="computer")
```

To control the user's own local browser instead, use the `browser` tool's `desktop` target through the [Matrix Desktop Bridge](https://docs.mindroom.chat/tools/desktop/).
`web_browser_tools` opens a browser on the host operating system and does not show anything in Chat.

The Computer panel needs an enabled [Worker Computer](https://docs.mindroom.chat/tools/worker-computer/), which owns the worker, browser, proxy, and Chat configuration and describes watching, taking control, and handing back.
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

## Interactive canvases

`show_canvas` shows a web page the agent wrote beside the conversation and returns what the user chose.
Use it when seeing or clicking beats reading or typing: dashboards, reports, charts, slides, menus, forms, pickers, and multi-step flows.

=== "Overview · 27 s"

    A narrated film of all four: a scheduling grid, a data fit, a slide deck, and a trip planner.

    <video class="only-light" controls playsinline preload="none" poster="https://github.com/user-attachments/assets/77e36a87-9fec-41f9-b626-73b950003845" aria-label="Canvas film: four agent canvases beside the conversation" style="width: 100%">
      <source src="https://github.com/user-attachments/assets/381f7395-4f1b-4167-8749-7f85080da8ce" type="video/mp4">
    </video>
    <video class="only-dark" controls playsinline preload="none" poster="https://github.com/user-attachments/assets/d2b7a4a6-0723-4d22-af2a-6b2ee7213602" aria-label="Canvas film: four agent canvases beside the conversation" style="width: 100%">
      <source src="https://github.com/user-attachments/assets/1a820f71-dd19-415f-9301-611310c3d510" type="video/mp4">
    </video>

=== "Scheduling"

    A week grid of four people's availability; the pick goes back with **Send**, and the agent books the meeting after approval.

    <video controls playsinline preload="metadata" aria-label="An agent shows a week grid; the user picks a slot and sends it, and the agent books it" style="width: 100%">
      <source src="https://github.com/user-attachments/assets/12aed795-793e-4de2-96fa-979f04d9545f#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
      <source src="https://github.com/user-attachments/assets/8fcc4a9d-73f6-4fd6-9b13-c5f456397cbc#t=0.1" type="video/mp4">
    </video>

=== "Data"

    An interactive fit drawn in inline SVG with the theme variables; leaving out an outlier refits the curve live.

    <video controls playsinline preload="metadata" aria-label="An agent shows a data fit; the user leaves out an outlier and sends back the fit" style="width: 100%">
      <source src="https://github.com/user-attachments/assets/28b4b4a2-fc73-4521-b93e-ff6dd8b35aa5#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
      <source src="https://github.com/user-attachments/assets/6abc66b1-73ae-4692-9c7c-0bdb986bac73#t=0.1" type="video/mp4">
    </video>

=== "Slides"

    A deck the agent writes to a workspace file and shows by `path`; after a change request it rewrites the file and the open deck updates in place.

    <video controls playsinline preload="metadata" aria-label="An agent shows a slide deck from its workspace and updates it in place after a change request" style="width: 100%">
      <source src="https://github.com/user-attachments/assets/15d64a9a-cf64-4f60-89ea-9249ea35bee0#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
      <source src="https://github.com/user-attachments/assets/bfe89d42-9d05-4554-b0dc-d731d73f9da3#t=0.1" type="video/mp4">
    </video>

=== "Trip planner"

    One canvas, two steps: pick a stay, then plan the days against a live budget, updated in place with `canvas_event_id`.

    <video controls playsinline preload="metadata" aria-label="One canvas, two steps: pick a stay, then plan the days against a live budget" style="width: 100%">
      <source src="https://github.com/user-attachments/assets/02f40dbd-6e39-463a-831d-d941ef32ecf9#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
      <source src="https://github.com/user-attachments/assets/e9c04cf1-74c2-48de-866b-b43ac529146d#t=0.1" type="video/mp4">
    </video>

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

### Turning canvases on

Canvases need two switches, and both are off by default.

The agent gets `show_canvas` only when `enable_show_canvas` (boolean, default `false`) is true in its `chat_ui` entry:

```yaml
agents:
  researcher:
    tools:
      - chat_ui:
          enable_show_canvas: true
```

It can also be set in the `chat_ui` tool's saved settings for every agent that uses `chat_ui`; an agent's own entry wins, as described in [How Settings Combine](https://docs.mindroom.chat/tools/#how-settings-combine).

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

When they are off, the notice shows its text and its button explains that interactive panels are turned off.
Canvases open automatically under the same conditions as other requests; otherwise the user chooses the notice's **Open panel** button.

### Pages and files

`title` is one line of at most 120 characters.
Pass the page as either `html` or a `path`, never both.
A `path` such as `slides/deck.html` suits pages the agent builds and refines: edit the file, then call `show_canvas` again with the same `path` and the canvas ID.
Paths follow the agent's `file_access`, like [`matrix_message` attachments](https://docs.mindroom.chat/tools/matrix-message/): the default `workspace` keeps them inside the workspace, and `unrestricted` allows any file the MindRoom process can read.
The file is read by the MindRoom process, so `~` means that process's home rather than a worker's; prefer workspace-relative paths.
The page must be UTF-8 text of at most 4 MB.

Pages up to about 24,000 characters travel inside the Matrix event.
Larger pages are uploaded as Matrix media (encrypted in encrypted rooms), and each update uploads a new copy that users cannot delete, so iterate on large pages sparingly.

Agents should only show pages they wrote.
A canvas always appears as the agent's own, so a downloaded or untrusted page would be presented under the agent's name, and what the user types into it could leave the panel.

### Design

Write self-contained HTML with inline CSS and JavaScript.
External scripts, styles, fonts, images, and network requests are blocked, so draw charts with inline SVG or a `<canvas>` element and embed images as `data:` URLs.
The user can resize the panel and expand it to the full width of the conversation, so use a responsive layout.

MindRoom Chat exposes its current theme as CSS variables, so a page can match light and dark mode:

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

Typing, toggling, dragging, and moving between steps inside the canvas send nothing.
When the page calls `window.mindroom.submit(data, {label})`, or the user submits a `<form>` (its fields become the data and its `data-mindroom-label` attribute the label), Chat shows what will be sent and the user confirms with **Send**.
While an answer waits for confirmation, the page cannot change it; the user discards it to choose again.
`data` must be JSON-serializable and at most 512 KB, enough for a long document the user edited in the canvas.

The answer is an ordinary threaded message from the user that mentions the agent, so it starts the agent's next turn:

```text
@mindroom_planner:example.org Canvas response ($canvas-event, revision $canvas-event): Pro plan
{"plan":"pro"}
```

The first ID is the canvas and the second is the version the user answered; if that revision is not the latest update, the user answered an earlier version of the page.
Decimal numbers and integers too large for JSON arrive as text.
MindRoom Chat shows the message as a one-line receipt that expands to the exact data; other clients show the text.

### Updating a canvas in place

`show_canvas` returns the canvas `event_id`.
To show the next step of the same flow, call `show_canvas` again with `canvas_event_id` set to that ID, not a revision ID.
The timeline keeps one card, an open panel switches to the new page, and the call returns `Canvas update sent.`
If the user has been working in the panel and has not sent anything since, Chat asks before replacing it.
An agent can update only its own canvases for the same user in the same room and thread; a canvas shown at room level can be updated from any thread of that room, because answering it starts a thread.

### Sandbox and limits

MindRoom Chat runs the page in a sandboxed frame:

- It cannot read the user's Matrix account, messages, cookies, or storage, and it cannot open pop-ups or navigate Chat or itself to other sites.
- Network requests and external resources are blocked.
- Nothing is posted to the conversation without the user's explicit **Send**.

These protections do not make a canvas a safe place for secrets.
Browsers do not let a page block WebRTC, so a malicious canvas could still leak what the user types into it.
Chat therefore shows which agent made each canvas and reminds the user that what they enter may leave the panel.
Chat does not run canvases during a call.
