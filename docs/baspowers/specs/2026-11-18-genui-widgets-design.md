# Native `ui` widgets in agent replies (chart and form pilot): design

- Date: 2026-11-18. Status: design only, nothing is implemented.
- Repos: `mindroom` (backend, at 09807d9f1) and `mindroom-chat` (client, at 26fb8040b), branch `feat/genui-widgets` in both.
- This file is the same in both repos.
- Plans: backend `docs/baspowers/plans/2026-11-18-genui-widgets-backend.md`, client `docs/baspowers/plans/2026-11-18-genui-widgets-chat.md`.
- Inputs: two code inventories, the A2UI research, the cross-model eval (216/216 on 9 OpenRouter models with the final schema and prompt) and the variant B mockups.

## 1. Understanding

- Outcome: an agent can put a chart or a form inside an ordinary reply, between its paragraphs, and MindRoom Chat draws it natively.
- Every other reader (Element, bridges, notifications, agents reading history) gets readable text, never raw JSON.
- A form's answer is an ordinary user message, so the agent's next turn needs no new backend handler.
- Success: a chart streams in without a scroll jump, survives reload and a second device, and a form round trip works with zero new event types.
- Assumption: rooms are mostly one person plus agents, with Chat as the main client; other clients only need a faithful fallback.
- Assumption: the eval's final schema and prompt are frozen except for prompt wording a second eval round may adjust.

## 2. Decisions taken as given

1. The agent writes one flat JSON widget per top-level `ui` fence, exactly the eval's final schema; over-limit data goes to prose.
2. The backend validates each block, replaces it in `body` with a readable fallback and writes `io.mindroom.ui` on the message; an invalid block stays a code block.
3. One visible-text rule serves durable edits and live frames; an unclosed `ui` fence is hidden; a closed valid block becomes its widget at once.
4. Chat renders widgets only for MindRoom agents on the viewer's homeserver, validates again, and follows the latest readable same-sender replacement.
5. A form's Submit sends one ordinary `m.text` reply with a mention, a readable list and `io.mindroom.ui_response`; "answered" is derived from the timeline.
6. A per-agent switch, off by default, adds the prompt beside the interactive-question prompt; a single choice stays `interactive`.
7. The widget code is client core, hand-drawn SVG in a lazy chunk, with a Chart/Table toggle, keyboard access, theme tokens, phone layout, i18n and RTL.
8. `io.mindroom.ui` is bounded and never forces a sidecar or a split; when it would, the key is dropped and the text stays.

## 3. Choices made in this spec

| # | Choice | Why | Rejected |
|---|---|---|---|
| C1 | **Marker: the fallback is one fenced code block whose info string is `mindroom-ui-<id>`.** `formatted_body` then holds `<pre><code class="language-mindroom-ui-<id>">`. | One HTML node holds the whole multi-line fallback. It is written in the Markdown itself, so `body`, `formatted_body` and the live frame's `md` get it from one rule with no renderer change. `class` on `code` already passes the backend sanitizer, Element and Chat's sanitizer. `split` never cuts inside a fence. Chat's hook is the existing `pre` case. | A `🔧`-style text line: it marks one line, and a fallback is several blocks, so the client would have to guess where it ends. A `<div data-mindroom-ui>` wrapper: raw tags in `body`, which agents and plain-text clients read, and a new attribute to let through the sanitizers of both repos. Matching the fallback text as the question card does: needs the fallback writer in TypeScript too. |
| C2 | **Widget id: the first 10 hex characters of the SHA-256 of the widget's canonical JSON.** | The tool-marker idea (token in the text, data in a content key) is kept, with a content id instead of a position. Positions need renumbering when a reply continues after an approval or a restart, because the earlier span's body is already frozen as text. A content id also gives a stable key for draft values, and a changed form gets a new id, so an old answer never marks a different form as answered. | Ordinals `[N]` into `widgets[N-1]`: collide across spans and shift on edits. |
| C3 | **The snapshot holds each widget as its canonical JSON text** (`{"id", "json"}`). | Matrix event JSON forbids floats, and chart values and slider steps are often fractional (the reason `io.mindroom.ai_run` publishes fractions as strings). JSON text needs no number encoding, and backend and client validate the very same text, so one fixture pins both validators. | Numbers as decimal strings inside a JSON object: a second, key-aware schema on the wire and a conversion in two languages. |
| C4 | **Live frames carry the same value as the content key**, in a new optional `ui` field of the frame documents; an open fence is signalled by `pending` inside that value. | One function produces the value for both transports. Chat copies `ui` into the live content as `io.mindroom.ui`, so one reader and one render path serve both. | Keeping the raw `ui` block in the live `md` for Chat to parse: two visible-text rules. |
| C5 | **Config name: `ui_widgets`** (`agents.<name>.ui_widgets`, `defaults.ui_widgets`, default `false`). | Matches the fence name `ui` and the key `io.mindroom.ui`. It gates the prompt and the transform, so an agent that was never told about widgets cannot produce one. | `widgets`, `native_widgets`, `enable_ui_widgets`. |
| C6 | **The chart fallback table shows at most 40 rows** (first 20, a count line, last 20). | Estimated with a prototype of the writer: a 200-point, 3-series chart costs about 37 KB as a final edit with the full table and 13 KB with the cap; the edit budget is 27 KB. Without the cap the charts that most need drawing would lose their widget. Tables of 40 rows or fewer are complete. | Full table always. |
| C7 | **On an edit, `io.mindroom.ui` is only in `m.new_content`.** | Halves its cost; clients that read the outer fallback do not draw widgets. Reasoning already does this. | Duplicating it like `body`. |
| C8 | **A form can be submitted only once its reply has finished.** | A submit during a running reply would be a mid-turn message to the same agent. | Submit while streaming. |
| C9 | **Minimal mode: a `ui` context document plus one bootstrap line** when the switch is on. | A minimal agent never sees the document list unless it asks; without the line it cannot know widgets exist. About 12 tokens, only for opted-in agents. | Document only. |

## 4. Authoring format (unchanged from the eval)

One JSON object per top-level fenced block whose info string's first word is `ui` (backticks or tildes, indented at most 3 spaces).
Fences nested in another code block, indented 4 or more spaces, or unclosed are not widgets.
Unknown keys are rejected everywhere; strings are never coerced to numbers; booleans are not numbers.

**chart**: `type:"chart"`, `kind` (`bar`, `line`, `area`, `pie`), `title`, `labels`, `series` of `{name, data}`, optional `x` and `y` axis titles.
Each series has one number per label; series names are unique; a pie has exactly one series with no negative value and a positive sum.
A number in `labels` is accepted and turned into its string.

**form**: `type:"form"`, `title`, `fields`, optional `submit`.
Every field has `name` (unique), `kind`, `label`, optional `required` (boolean).

| Kind | Extra keys |
|---|---|
| `select`, `radio` | `options` (unique), optional `value` (one of the options) |
| `checkbox` | without `options`: one yes/no box, optional `value` boolean; with `options`: multiple choice, optional `value` list of options |
| `slider` | `min` < `max`, optional `step` > 0 with at most 10,000 steps, optional `value` in range, optional `unit` |
| `text` | optional `placeholder`, optional `value` |
| `date` | optional `min`, `max`, `value`, each a real `YYYY-MM-DD` date, `min` not after `max` |

A field whose name (underscores read as spaces) or label matches the secret pattern is rejected, and there is no password kind:
`pass(word|phrase|code)|\bpin( code| number)?$|\bsecret\b|api[ _-]?key|\b(access|auth|bearer|api)[ _-]?token|\bcvv\b`, case-insensitive.

## 5. Wire format

### 5.1 Canonical JSON and id

- Canonical JSON of a valid widget: the validated widget without absent optional keys, object keys sorted, separators `,` and `:` with no spaces, non-ASCII characters kept, and every whole number written without a fraction (`120`, not `120.0`).
- `id` = the first 10 lowercase hex characters of SHA-256 over the UTF-8 bytes of that text.
- Two identical widgets in one reply share one id and one entry; both places draw it.

### 5.2 The content key

```json
"io.mindroom.ui": {
  "version": 1,
  "widgets": [
    {"id": "df4d729a5d", "json": "{\"kind\":\"line\",\"labels\":[\"Jan\",\"Feb\",\"Mar\"],\"series\":[{\"data\":[120,180.5,150],\"name\":\"Signups\"}],\"title\":\"Signups\",\"type\":\"chart\",\"y\":\"users\"}"}
  ],
  "pending": "chart"
}
```

- `widgets`: entries in the order their blocks appear, at most 8, each `id` once.
- `pending` is optional and only on in-progress updates and live frames: the text ends inside an open `ui` fence, of kind `chart`, `form` or `widget` (not known yet).
- The key is absent when there is neither a widget nor a pending fence.
- An unknown `version` means no widgets.

### 5.3 The marker and the fallback text

Each valid block is replaced, in place, by a fenced block with the info string `mindroom-ui-<id>` whose content is the fallback text.
The fence is backticks, one longer than the longest backtick run in the content and at least three.
Every string from the widget is written with its whitespace runs collapsed to one space, so no value spans lines.

Chart fallback:

````text
```mindroom-ui-df4d729a5d
Signups (line chart, users)
     Signups
Jan      120
Feb    180.5
Mar      150
```
````

- Line 1: `<title> (<kind> chart)` or `<title> (<kind> chart, <y>)`.
- Line 2: the `x` title (or nothing) and the series names; then one row per label.
- Column 1 is left-aligned, the others right-aligned, columns two spaces apart, trailing spaces removed.
- Whole numbers print without a fraction, others in their shortest form.
- More than 40 rows: the first 20, the line `... <n> more rows ...`, the last 20.

Form fallback:

````text
```mindroom-ui-0fd993d70d
Plan the trip (form)
1. Pace (choose one): Relaxed | Packed (required)
2. Interests (choose any): Food | Museums
3. Daily budget (50 to 500 USD, step 10) [default: 150]
4. Start date (date, from 2026-12-01)
5. Notes (text)
Reply in your own words.
```
````

Each field line is `<n>. <label> ` followed by:

- `select`, `radio`: `(choose one): ` and the options joined by ` | `.
- `checkbox` with options: `(choose any): ` and the options joined by ` | `.
- `checkbox` without options: `(yes or no)`.
- `slider`: `(<min> to <max>[ <unit>][, step <step>])`.
- `text`: `(text)`.
- `date`: `(date[, from <min>][, to <max>])`.

- A default adds ` [default: <value>]` (`yes`/`no` for a box, options joined by `, ` for multiple choice); `required: true` adds ` (required)` last.
- A placeholder and the `submit` label are not in the fallback.
- The last line is always `Reply in your own words.`

### 5.4 A finished reply (the effective content of its final edit)

```json
{
  "msgtype": "m.text",
  "body": "Signups peaked in February.\n\n```mindroom-ui-df4d729a5d\nSignups (line chart, users)\n     Signups\nJan      120\nFeb    180.5\nMar      150\n```\n\nMarch is still 25% above January.",
  "format": "org.matrix.custom.html",
  "formatted_body": "<p>Signups peaked in February.</p>\n<pre><code class=\"language-mindroom-ui-df4d729a5d\">Signups (line chart, users)\n     Signups\nJan      120\nFeb    180.5\nMar      150\n</code></pre>\n<p>March is still 25% above January.</p>\n",
  "io.mindroom.stream_status": "completed",
  "io.mindroom.ui": {
    "version": 1,
    "widgets": [
      {"id": "df4d729a5d", "json": "{\"kind\":\"line\",\"labels\":[\"Jan\",\"Feb\",\"Mar\"],\"series\":[{\"data\":[120,180.5,150],\"name\":\"Signups\"}],\"title\":\"Signups\",\"type\":\"chart\",\"y\":\"users\"}"}
    ]
  }
}
```

The model wrote the same reply with a `ui` block holding `{"type":"chart","kind":"line","title":"Signups","labels":["Jan","Feb","Mar"],"series":[{"name":"Signups","data":[120,180.5,150]}],"y":"users"}` between the two paragraphs.
Relation, mention and run-metadata keys are unchanged and left out here.

### 5.5 In progress, and live frames

- Text shown while the reply streams: closed valid blocks are already replaced; the text is cut where an open top-level `ui` fence begins.
- The text is also cut at a last line that is only an opening fence still being typed (three or more backticks or tildes followed by nothing, `u` or `ui`, and no newline yet); `pending` is set only once the info string is `ui`.
- `pending` is `chart` or `form` when the open block already contains `"type":"chart"` or `"type":"form"`, else `widget`.
- Durable progress updates carry the value as `io.mindroom.ui` (in `m.new_content` when the update is an edit).
- Live frames (`io.mindroom.live`) carry the same value as `ui`: present in a snapshot when there is a value; in a delta, `ui` replaces the whole value and `null` clears it. Frame version stays 1.
- On a terminal update nothing is pending: a `ui` fence that never closed stays an ordinary code block, as an unclosed `interactive` block does.

### 5.6 A form answer

The form's reply carries `{"id": "0fd993d70d", "json": "{\"fields\":[{\"kind\":\"radio\",\"label\":\"Pace\",\"name\":\"pace\",\"options\":[\"Relaxed\",\"Packed\"],\"required\":true},{\"kind\":\"checkbox\",\"label\":\"Interests\",\"name\":\"interests\",\"options\":[\"Food\",\"Museums\"]},{\"kind\":\"slider\",\"label\":\"Daily budget\",\"max\":500,\"min\":50,\"name\":\"budget\",\"step\":10,\"unit\":\"USD\",\"value\":150},{\"kind\":\"date\",\"label\":\"Start date\",\"min\":\"2026-12-01\",\"name\":\"start\"},{\"kind\":\"text\",\"label\":\"Notes\",\"name\":\"notes\",\"placeholder\":\"Anything else\"}],\"submit\":\"Plan my trip\",\"title\":\"Plan the trip\",\"type\":\"form\"}"}`.
Submit sends this event content (the Notes field was left empty):

```json
{
  "msgtype": "m.text",
  "body": "@mindroom_atlas:example.org Plan the trip:\n- Pace: Relaxed\n- Interests: Food, Museums\n- Daily budget: 150 USD\n- Start date: 2026-12-05",
  "format": "org.matrix.custom.html",
  "formatted_body": "<a href=\"https://matrix.to/#/%40mindroom_atlas%3Aexample.org\">Atlas</a> Plan the trip:<ul><li>Pace: Relaxed</li><li>Interests: Food, Museums</li><li>Daily budget: 150 USD</li><li>Start date: 2026-12-05</li></ul>",
  "m.mentions": {"user_ids": ["@mindroom_atlas:example.org"]},
  "m.relates_to": {
    "rel_type": "m.thread",
    "event_id": "$root:example.org",
    "is_falling_back": false,
    "m.in_reply_to": {"event_id": "$form:example.org"}
  },
  "io.mindroom.ui_response": {
    "version": 1,
    "event_id": "$form:example.org",
    "widget": "0fd993d70d",
    "values": {"pace": "Relaxed", "interests": ["Food", "Museums"], "budget": 150, "start": "2026-12-05"}
  }
}
```

- `event_id` is the form message's own id (the original event, not an edit); `widget` is the form's id.
- The relation is what Chat's `getMessageRelation(eventId, undefined, threadId)` builds; outside a thread it is a plain reply.
- `values` by kind: option string; boolean; list of option strings (an empty list is an answer); number; text; `YYYY-MM-DD`.
- A number that is not a safe integer is written as its decimal string (`"2.5"`), because event JSON forbids floats.
- A field left unanswered (no choice, empty text, no date) is absent from `values` and from the list.
- Body lines are `- <label>: <value>`: `yes`/`no` for a box, options joined by `, ` or `none`, the slider value followed by its unit.
- With no line at all the body ends with `(no answers)`.
- The fixed words in the body are English on the wire, like the canvas answer line.

## 6. Backend (mindroom)

### 6.1 Modules

- `src/mindroom/ui_widgets/models.py`: the eval schema as strict pydantic models, the limits, and `parse_widget(json_text)`.
- `src/mindroom/ui_widgets/render.py`: the fence scan, canonical JSON, id, fallback writer, and `render_ui_widgets(text, *, in_progress)`, which returns the visible text and the `io.mindroom.ui` value (or `None`).
- `UI_WIDGETS_CONTENT_KEY = "io.mindroom.ui"` in `constants.py`.
- No handler for `io.mindroom.ui_response`: the answer is a normal mention turn and the agent reads its body.

### 6.2 The visible-text rule

`streaming._visible_text` is the one place: it runs `render_ui_widgets` first on the accumulated text, then the interactive-question formatting on the result, and returns the value with the text.
Durable edits (`_prepare_delivery_from_snapshot`) put the value in the content; `live_state` puts the same value in `LiveDocs.ui`.
The non-streamed final (`delivery_gateway`, where it formats the interactive question) runs the same function with `in_progress=False`.
All three run it only when `ui_widgets` resolves true for the replying agent.

- A valid block always becomes its fallback block.
- It gets an entry while the reply has fewer than 8 entries and the value stays within 16,384 bytes; past that the fallback alone shows.
- An invalid closed block is left exactly as written; the final update logs a warning with the validation errors.
- A marker fence already in the text (an earlier span's frozen body, or one a model copied) is an ordinary code block: it gets no entry and is never renumbered.

Accepted limits of the first slice:

- A reply that continues after a tool approval or a restart keeps the earlier span's widgets as fallback text only, because that span's body is frozen as rendered text.
- A terminal write that re-renders a frozen body (the restart or cancellation note on an old span) carries no `io.mindroom.ui`.
- Widgets apply to an agent's own replies only: not to teams, the router, `matrix_message`, the OpenAI-compatible API or Slack delivery, where the block stays a code block.

### 6.3 Size

`fit_ui_widgets_to_ordinary_event` in `matrix/large_messages.py` runs wherever `fit_reasoning_to_ordinary_event` runs, just before it:

1. On an edit the key is kept only in `m.new_content`.
2. If the event with the key and without its reasoning is over the limit that would move it to a sidecar or split it (27,000 bytes for an edit, 55,000 otherwise, the first-segment target under `split`), or over the 64,000-byte hard limit as delivered, the key is removed.
3. Reasoning is then fitted into what is left, as today.

So a widget never rides on a sidecar preview or a split reply, and the fallback text is what large-message handling sees.

### 6.4 Config, prompt, documents

- `agents.<name>.ui_widgets: bool | None` and `defaults.ui_widgets: bool = False`; `config.resolution.ui_widgets(config, name)` returns the agent's value, else the default, and `False` for a team, the router or an unknown name.
- The field is live (no restart) and listed in the dashboard's Responses section.
- `UI_WIDGETS_PROMPT` in `prompts.py`, overridable like the other prompts, is appended right after `INTERACTIVE_QUESTION_PROMPT` when both `include_interactive_questions` and `ui_widgets` hold.
- Minimal mode: the prompt is the context document `ui`, the `mindroom-agent context` help names it, and the bootstrap gains the line ``Charts and forms in replies: read `mindroom-agent context read ui` first.``
- User docs: a short `docs/widgets.md`, one row in `docs/configuration/agents.md`, and one sentence each in `docs/streaming.md` and `docs/interactive.md`.

### 6.5 Prompt text

`UI_WIDGETS_PROMPT` is `genui-eval/prompt_fenced.md` verbatim (399 tokens); if the second eval round changes its wording, the newer file wins.

`````text
To show a native widget, write one JSON object in a ```ui code block. The default is prose; use a widget only when it clearly beats text:

- `chart`: the user asks for a chart or plot, or wants to see a trend or breakdown of numbers you have.
- `form`: you need several answers before continuing. They arrive as the user's next message. For a single choice use ```interactive.

```ui
{"type":"chart","kind":"line","title":"Signups","labels":["Jan","Feb","Mar"],"series":[{"name":"Signups","data":[120,180,150]}],"y":"users"}
```

```ui
{"type":"form","title":"Plan the trip","fields":[
{"name":"pace","kind":"radio","label":"Pace","options":["Relaxed","Packed"]},
{"name":"interests","kind":"checkbox","label":"Interests","options":["Food","Museums"]},
{"name":"budget","kind":"slider","label":"Daily budget","min":50,"max":500,"step":10,"value":150,"unit":"USD"}]}
```

IMPORTANT:
- Chart kinds: bar, line, area, pie. Field kinds: select, radio, checkbox, slider, text, date. Use only the keys shown. Max 8 series, 200 points, 12 fields; for more, answer in prose, don't sample or average.
- Never invent data. Chart only numbers the user gave or a tool returned, unchanged; otherwise say you don't have them.
- Write prose that stands alone: state the takeaway (for a form: what you need) in a sentence. Don't write "here is the chart".
- No widget for definitions, how-tos, code questions, yes/no answers, single numbers or small comparisons (use a table).
- No password or secret fields.
- To show widget JSON as code, use ```json, not ```ui.
`````

## 7. Client (mindroom-chat)

### 7.1 Reading and trust

- `io.mindroom.ui` joins `AGENT_ONLY_CONTENT_KEYS`, so it is stripped from any sender that is not a MindRoom agent on the viewer's homeserver (`isMindroomAgentUserIdForViewer`).
- `readUiWidgets(content)` in `matrix/protocol/uiWidgets.ts` accepts only `version: 1`, at most 8 entries, each a 10-hex `id` and a string `json` of at most 16,384 bytes, and an optional known `pending`.
- It reads the content the reply shows (the latest readable replacement by the same sender), never the original event behind an edit, and the key is not borrowed from older edits.
- A redacted or undecryptable reply has no content, so no widget.
- `parseUiWidget(json)` is the strict validator with the backend's bounds; lengths count code points, as pydantic does.
- A marker without a valid entry renders as a plain code block holding the fallback text, with no language label.
- Nothing in a widget is HTML, a URL to load, or code; every string renders as text.

### 7.2 Placement

- The base HTML parser's `pre` case checks for `code.language-mindroom-ui-<id>` and renders `UiWidgetSlot` with the code block as its fallback.
- The slot reads `UiWidgetsContext`; `renderAgentMessageContent` provides it only for a MindRoom agent's reply, with the entries, the event, room, thread and `readOnly`.
- Without a provider (other senders, previews, plugins) the slot renders the fallback, so a pasted marker does nothing.
- Durable replies and live replies take the same path, because the live content gets `io.mindroom.ui` from the frame's `ui`.
- While `pending` is set and the reply is still streaming, a skeleton 325 px high follows the body; it has `role="status"` and a label per kind.
- Validation, the chart and the form load as one lazy chunk; the slot shows the same skeleton until it arrives.
- Previews (thread cards, Activity, notifications, search) keep using `body`, so they show the fallback text.

### 7.3 Chart

- A bordered card in the question card's frame (variant B); variants A and C are a style-only change in `UiChart.css.ts`.
- Header: title, the `y` title as a subtitle, and a Chart/Table toggle (two buttons with `aria-pressed`; icons only under 420 px).
- Plot: SVG drawn at the card's measured width with fixed text size; bar (grouped), line, area and pie (a donut with a value and percent legend).
- The plot is one tab stop: arrow keys move between labels, Home and End jump, and the tooltip follows focus or hover and is announced politely.
- Table view is a real `<table>` with the labels as row headers; it is what screen readers get.
- Numbers use the app's locale formatting; colors come from `theme/colors.css.ts` tokens, 8 series colors, light and dark.
- RTL: the card lays out with logical properties and the plot itself stays left to right.

### 7.4 Form

- The same card frame; native controls with bound labels; one column under 420 px.
- Initial values are the widget's `value`s; a slider without one starts at `min`.
- Draft values live in memory only, keyed by room, event and widget id, so they survive a row leaving the virtual timeline and die with the tab.
- Submit is offered when the reply has finished and under the question card's rule: the view is not read-only, the event has a confirmed id, and the viewer is not an agent account.
- `required` fields use native validation; Submit with one missing focuses it.
- Submit sends the event of section 5.6 at once, with no confirm step.
- Answered: the timeline holds a message by the viewer, not redacted, that replies to the form's event and whose `io.mindroom.ui_response` names its `event_id` and `widget`; a local echo counts, so the card folds on Send.
- An answered form is a one-row receipt (check, title, "Answered", chevron) that opens to the label and value list, built from the receipt styles tool approvals use.
- Removing the answer message brings the form back; other people's answers do not mark it answered for the viewer.

## 8. Limits

| Limit | Value | Enforced by |
|---|---|---|
| Block JSON text | 16,384 UTF-8 bytes | backend, client |
| Series, points, fields, options | 8, 200, 12, 12 (at least 2 options) | backend, client |
| `title` 80, series name, label entry, `x`, `y` 40, field `label` and `placeholder` 120, option 60, `submit` 24, `unit` 16, text `value` 200 | code points, at least 1 except text `value` | backend, client |
| Field `name` | `^[a-z][a-z0-9_]{0,31}$` | backend, client |
| Slider steps | 10,000 | backend, client |
| Entries per reply | 8 | backend, client |
| `io.mindroom.ui` value | 16,384 bytes of compact JSON | backend |
| Fallback table rows | 40 | backend |
| Typed text answer | 500 characters | client |

## 9. Deferred

- Widgets in team replies, `matrix_message`, the OpenAI-compatible API, Slack and the Apple Watch app.
- Carrying widgets across the spans of one reply, and on restart or cancellation notes.
- A preview rule that shows only the widget's title line.
- A form on a thread's root message shows as answered only inside its thread.
- A "Form data" disclosure on the answer message, draft persistence across reloads, a confirm step.
- More widget kinds, layout, expressions, images, maps, follow-up chips, inline canvas submit, a plugin hook for code-block renderers.
- Stacked bars, scatter, time axes, decimation of more than 200 points.
- Enabling by default, and local models (Qwen prose regressed in the eval; Gemma was not measured).

## 10. Test strategy

- **Shared contract fixture** `ui-widgets-v1.json`, the same file in both repos (backend `tests/ui_widgets/fixtures/`, client `src/app/messages/widgets/__fixtures__/`), as the live-frame fixtures already are.
  - `widgets`: JSON texts with `valid`, and for valid ones `canonical` and `id`. Python and TypeScript must give the same verdict; Python also pins `canonical` and `id`.
  - `renders`: `markdown`, `in_progress`, expected `text`, `ui` and `html`. Python pins the transform and `markdown_to_html(text)`; TypeScript pins `readUiWidgets(ui)`, the marker ids found in `html`, and its live Markdown renderer on `text`.
  - `responses`: a form, the answers and the expected event content of section 5.6. TypeScript pins the builder and reader; the backend has no code for it.
- **Live-frame fixtures**: two new cases in `live-frames-v1.json` (a delta that sets `ui`, a snapshot that has it), in both repos.
- **Backend unit tests**: schema, scan, fallback, in-progress hiding and `pending`, streaming edits and live frames, the non-streamed final, the size rule, config resolution, the prompt in standard and minimal mode.
- **Client unit tests**: reader, validator, slot and trust cases, chart geometry and table, form values and drafts, the answer builder, answered derivation, live content.
- **Client e2e**: `#chart` and `#form` stub-model scenarios against a backend checkout that has the feature, with screenshots at desktop and 390 px, light and dark.
- **Fresh live test** (never lab or production): from `~/repos/dev`, `just live up --chat <chat worktree> --backend <backend worktree>` (`scripts/live.py`), set `ui_widgets: true` on the Helper in the printed `config.yaml`, drive it with `just live ask` and a small Playwright script, then `just live down`.

| # | Scenario | Pass when |
|---|---|---|
| L1 | Ask for a chart of numbers given in the message | Skeleton while the fence is open, card when it closes, no scroll jump; the final event has the entry; reload shows the card |
| L2 | Read L1's event over the API | `body` holds the table, no JSON; `io.mindroom.ui` only in `m.new_content` |
| L3 | Ask for something that needs a form; fill and Submit | One user message as in 5.6; the card folds to a receipt; the agent's next reply uses the values; reload and a second session show the receipt |
| L4 | Fractional data and a slider with step 0.5, in an unencrypted and an encrypted room | Both events are accepted by the homeserver and render |
| L5 | A block with an unknown type | Stays a code block, no error banner |
| L6 | Stop while a `ui` fence is open | No stuck skeleton; the partial block is a code block |
| L7 | A long reply (over the edit budget) that contains a chart | The key is dropped, the table shows, no sidecar is caused by the key |
| L8 | The same prompts with `ui_widgets` off | No widget prompt in the request, a `ui` block stays code |
| L9 | Regenerate L1's reply into prose | The card is gone |
| L10 | A minimal-mode agent with the switch on | It reads the `ui` document and produces a valid block |

A failure is rerun on a deployment without `--backend` to tell a regression from an existing bug.

## 11. Risks

- The fallback is a code block in other clients; a monospace table suits a chart, and is plain but readable for a form.
- A model may imitate a `mindroom-ui-` block it sees in thread history; it is then an ordinary code block.
- Initial CSS had about 600 bytes of gzip headroom when last measured (2026-11-13), so the skeleton's styles must stay tiny and everything else must live in the lazy chunk.
- The eval cases were easy; overuse on borderline prompts is not measured, which is why the switch is off by default.
