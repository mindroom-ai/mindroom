# Interactive Canvases

An agent can show the user a web page it wrote, such as a dashboard, report, slide deck, form, or picker, in the Canvas panel beside the conversation in MindRoom Chat.
The user can work in the page and send an answer, which reaches the agent as the user's next message.
Use a canvas when seeing or clicking beats reading or typing; for a quick choice between a few options, a plain reply or an [interactive question](https://docs.mindroom.chat/interactive/) is simpler.

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

### Let pages load libraries

Pages can also load libraries such as Chart.js, D3, Mermaid, or KaTeX from jsDelivr's npm CDN, `https://cdn.jsdelivr.net/npm/`.
This takes two more switches, both off by default; turn on both, because a page that needs a library breaks where Chat blocks it.

```yaml
agents:
  planner:
    display_name: Planner
    role: Plan trips and compare the options visually.
    model: default
    tools:
      - chat_ui:
          enable_show_canvas: true
          enable_canvas_libraries: true
```

```json
{
  "mindroom": {
    "canvas": {
      "enabled": true,
      "libraries": true
    }
  }
}
```

`enable_canvas_libraries` (boolean, default `false`) tells the agent it may use the source, and Chat's `libraries` lets pages load from it.
Pages may then load scripts, styles, and fonts from `/npm/` paths only; fetching data, external images, workers, and every other site stay blocked, and libraries that evaluate strings, such as Alpine or Vue in-page templates, do not run.
Opening a page that uses a library tells jsDelivr the viewer's IP address and which files they load, which is why Chat keeps libraries off unless its deployment allows them.

## What the user sees

Each canvas appears in the conversation as a notice with an **Open panel** button.
Chat opens the panel by itself under the same conditions as other [Chat UI actions](https://docs.mindroom.chat/tools/chat-ui/#what-the-user-sees); otherwise the user chooses **Open panel**.
The panel names the agent that made the page, and the user can resize it or expand it to the full width of the conversation.

When the panel cannot open, the **Open panel** button is disabled and a message below it says why:

| Message | Cause |
|---|---|
| Interactive panels are turned off in this app. | The Chat deployment has not enabled canvases. |
| Interactive panels are not available in the app yet. Open this conversation in a browser. | The native mobile app does not show canvases; Chat in a browser does. |
| End the call to open interactive panels. | Chat does not run canvases during a call. |

Other people in the room, and other Matrix clients, see only the notice text, `Interactive panel: <title>. Open it in MindRoom Chat to respond.`

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
An answer can carry up to 512 KB of JSON data, enough for a long document the user edited in the canvas; larger or non-JSON data is ignored, so no **Send** prompt appears.

When the page throws an error, fails to load a file, or tries to load something Chat blocks, the panel lists up to five of those errors with a **Tell** *agent* button.
Nothing is sent unless the user chooses it; the report reaches the agent like an answer:

```text
@mindroom_planner:example.org Canvas error ($canvas-event, revision $canvas-event):
Uncaught ReferenceError: drawChart is not defined (line 12)
Blocked https://unpkg.com/chart.js (script-src-elem)
```

## Update a canvas

`show_canvas` returns the canvas `event_id`.
Calling `show_canvas` again with `canvas_event_id` set to that ID replaces the page in place, for the next step of a flow or a new version of a file the agent edited, and the conversation keeps one card.
An update may leave out `title` to keep the title the canvas was first shown with.
An open panel switches to the new page at once, unless the user has been working in it; then Chat shows "*agent* updated this panel." with a **Load update** button, so unsent work is not lost.
Every update stays available: once a canvas has more than one version, the panel header shows *Version n of m* with buttons to go back and forward, and an earlier version offers **Show latest**.
The user can answer an earlier version, and switching waits while an answer is pending.
An agent can update only canvases it showed to the same user in the same room and thread, or at room level; otherwise the call fails and the agent shows a new canvas instead.

## Write a page

Pass the page as `html`, or as a `path` to an HTML file, which suits pages the agent builds and refines, such as slides.
Paths follow the same rules as [registering local files](https://docs.mindroom.chat/attachments/#registering-local-files); prefer workspace-relative paths such as `slides/deck.html`.

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

- **Self-contained:** write inline CSS and JavaScript. The page cannot fetch data or load external images, and it loads libraries only where [allowed](#let-pages-load-libraries); otherwise draw charts with inline SVG or a `<canvas>` element, and always embed images as `data:` URLs.
- **Submitting:** call `window.mindroom.submit(data, {label})` with JSON-serializable `data` and a short `label`, or use a `<form>`, whose fields become the data and whose `data-mindroom-label` attribute sets the label.
- **Responsive:** the panel ranges from narrow to the full conversation width.
- **No saved state:** the page starts fresh every time it loads, so anything the user must keep belongs in the answer.
- **Limits:** the `title` is one line of at most 120 characters, and the page is UTF-8 text of at most 4 MB.
  Each update of a page larger than about 24 KB stores a new copy on the homeserver that users cannot delete, so iterate on large pages sparingly.

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

For choices the variables cannot make, such as a chart palette, `window.mindroom.colorScheme` is `light` or `dark`.

## Privacy and safety

- The page cannot read the user's Matrix account, messages, cookies, or storage, and it cannot open pop-ups or navigate away; a page that tries is stopped, and Chat shows "This panel tried to leave its sandbox and was stopped." with a **Reload panel** button.
- In an encrypted room, the page and the answer are end-to-end encrypted like other messages.
- A canvas is not a safe place for secrets: browsers do not let a page block WebRTC, and a page that loads libraries can put data in the addresses it requests, so a malicious page could leak what the user types into it.
  Chat therefore names the agent that made each canvas and reminds the user that what they enter may leave the panel.
- Agents should show only pages they wrote, never downloaded or untrusted pages, because every canvas appears as the agent's own.
