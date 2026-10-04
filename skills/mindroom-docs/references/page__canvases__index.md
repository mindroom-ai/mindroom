# Interactive Canvases

An agent can show the user a web page it wrote, such as a dashboard, report, slide deck, form, or picker, in the Canvas panel beside the conversation in MindRoom Chat.
The user can work in the page and send an answer, which reaches the agent as the user's next message.
Use a canvas when seeing or clicking beats reading or typing; for a quick choice between a few options, a plain reply or an [interactive question](https://docs.mindroom.chat/interactive/) is simpler.

## Turn canvases on

Canvases need two switches, and both are off by default.

First, give the agent the [`chat_ui`](https://docs.mindroom.chat/tools/chat-ui/) toolkit with `enable_show_canvas: true`:

```yaml
agents:
  planner:
    display_name: Planner
    role: Plan trips and compare the options visually.
    model: default
    tools:
      - chat_ui:
          enable_show_canvas: true
```

`enable_show_canvas` (boolean, default `false`) adds the `show_canvas` function; the rest of `chat_ui` works without it.
It can also be set in the `chat_ui` tool's saved settings for every agent that uses `chat_ui`; an agent's own entry wins, as described in [How Settings Combine](https://docs.mindroom.chat/tools/#how-settings-combine).

Second, enable canvases in the MindRoom Chat deployment's runtime `config.json`:

```json
{
  "mindroom": {
    "canvas": {
      "enabled": true
    }
  }
}
```

The shipped MindRoom Chat configuration sets `enabled` to `false`, so only whoever deploys Chat can turn canvases on.

## What the user sees

Each canvas appears in the conversation as a notice with an **Open panel** button.
Chat opens the panel by itself under the same conditions as other [Chat UI actions](https://docs.mindroom.chat/tools/chat-ui/#what-the-user-sees); otherwise the user chooses **Open panel**.
The panel names the agent that made the page, and the user can resize it or expand it to the full width of the conversation.

When the panel cannot open, the button says why:

| Message | Cause |
|---|---|
| Interactive panels are turned off in this app. | The Chat deployment has not enabled canvases. |
| Interactive panels are not available in the app yet. Open this conversation in a browser. | The native mobile app does not show canvases; Chat in a browser does. |
| End the call to open interactive panels. | Chat does not run canvases during a call. |

Other Matrix clients show only the notice text, `Interactive panel: <title>. Open it in MindRoom Chat to respond.`

## Answering a canvas

Typing, toggling, and moving between steps inside the page send nothing.
When the page submits an answer, Chat shows the user exactly what will be sent, and nothing is sent until the user chooses **Send**.
To change a pending answer, the user chooses **Discard** and answers again.

The answer is an ordinary message from the user in the canvas's thread; it mentions the agent and starts the agent's next turn:

```text
@mindroom_planner:example.org Canvas response ($canvas-event, revision $canvas-event): Pro plan
{"plan":"pro"}
```

The first ID is the canvas and the second is the version the user answered; when that is not the latest version, the user answered an earlier page.
MindRoom Chat shows the answer as a one-line receipt that expands to the exact data.
An answer can carry up to 512 KB of data, enough for a long document the user edited in the canvas.

## Update a canvas

`show_canvas` returns the canvas `event_id`.
Calling `show_canvas` again with `canvas_event_id` set to that ID replaces the page in place, for the next step of a flow or a new version of a file the agent edited, and the conversation keeps one card.
An open panel switches to the new page at once, unless the user has been working in it; then Chat shows "*agent* updated this panel." with a **Load update** button, so unsent work is not lost.
An agent can update only its own canvases from the same conversation; elsewhere it shows a new canvas instead.

## Write a page

Pass the page as `html`, or as a `path` to an HTML file, which suits pages the agent builds and refines, such as slides.
Paths follow the agent's `file_access`, like [`matrix_message` attachments](https://docs.mindroom.chat/tools/matrix-message/#attachments); prefer workspace-relative paths such as `slides/deck.html`.

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

- **Self-contained:** write inline CSS and JavaScript. The page cannot load or send anything over the network, so draw charts with inline SVG or a `<canvas>` element and embed images as `data:` URLs.
- **Submitting:** call `window.mindroom.submit(data, {label})` with JSON-serializable `data` and a short `label`, or use a `<form>`, whose fields become the data and whose `data-mindroom-label` attribute sets the label.
- **Responsive:** the panel ranges from narrow to the full conversation width.
- **No saved state:** the page starts fresh every time it loads, so anything the user must keep belongs in the answer.
- **Limits:** the `title` is one line of at most 120 characters, and the page is UTF-8 text of at most 4 MB.
  Pages larger than about 24 KB are stored as Matrix media, and each update stores a new copy, so iterate on large pages sparingly.

Chat exposes its current theme as CSS variables, so a page can match light and dark mode:

| Variable | Use |
|---|---|
| `--mr-bg` | Page background (the default body background) |
| `--mr-surface`, `--mr-surface-raised` | Cards and raised elements |
| `--mr-border` | Borders and dividers |
| `--mr-text`, `--mr-text-muted` | Text (the default body color) and secondary text |
| `--mr-accent`, `--mr-accent-text` | Primary actions and the text on them |
| `--mr-success`, `--mr-warning`, `--mr-danger` | Status colors |
| `--mr-radius`, `--mr-font` | Corner radius and font family |

## Privacy and safety

- The page cannot read the user's Matrix account, messages, cookies, or storage, and it cannot open pop-ups or navigate away; a page that tries to leave its sandbox is stopped.
- Nothing reaches the conversation until the user chooses **Send**.
- A canvas is not a safe place for secrets: browsers do not let a page block WebRTC, so a malicious page could leak what the user types into it.
  Chat therefore names the agent that made each canvas and reminds the user that what they enter may leave the panel.
- Agents should show only pages they wrote, never downloaded or untrusted pages, because every canvas appears as the agent's own.
