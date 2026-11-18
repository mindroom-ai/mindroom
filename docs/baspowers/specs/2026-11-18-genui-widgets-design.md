# Native `ui` widgets in agent replies (chart, then form): design

- Date: 2026-11-18, revised the same day after two cross-model reviews (appendix A). Status: design only.
- Repos: `mindroom` (backend, at 09807d9f1) and `mindroom-chat` (client, at 26fb8040b), branch `feat/genui-widgets` in both.
- This file is the same in both repos.
- Plans: backend `docs/baspowers/plans/2026-11-18-genui-widgets-backend.md`, client `docs/baspowers/plans/2026-11-18-genui-widgets-chat.md`.
- Inputs: two code inventories, the A2UI research, the cross-model eval (rounds 1 and 2) and the variant B mockups.

## 1. Understanding

- Outcome: an agent can put a chart, and later a form, inside an ordinary reply, between its paragraphs, and MindRoom Chat draws it natively.
- Every other reader (Element, bridges, notifications, agents reading history) gets readable text, never raw JSON.
- A form's answer is an ordinary user message, so the agent's next turn needs no new backend handler.
- Assumption: rooms are mostly one person plus agents, with Chat as the main client; other clients only need a faithful fallback.

**Staging.**
Stage 1 is the chart: one pull request per repo, built now.
Stage 2 is the form: its own pull requests later.
The wire contract in section 5 already covers both, so Stage 2 changes no format.
Stage 1 code knows only `chart`: the Stage 1 backend treats a `form` block like any block it does not know (it stays a code block), and Stage 1 Chat shows the fallback text for any entry it cannot draw.
Section 9 lists what each stage contains.

## 2. Decisions taken as given

1. The agent writes one flat JSON widget per `ui` fence, the eval's round-2 schema; over-limit data goes to prose.
2. The backend validates each block, replaces it in `body` with a readable fallback and writes `io.mindroom.ui` on the message; an invalid block stays a code block.
3. One visible-text rule serves durable edits and live frames; an unclosed `ui` fence is hidden; a closed valid block becomes its widget at once.
4. Chat renders widgets only for MindRoom agents on the viewer's homeserver, validates again, and follows the latest readable same-sender replacement.
5. A form's Submit sends one ordinary `m.text` reply with a mention, a readable list and `io.mindroom.ui_response`; "answered" is derived from the timeline.
6. A per-agent switch, off by default, adds the prompt beside the interactive-question prompt; a single choice stays `interactive`.
7. The widget code is client core, hand-drawn SVG in a lazy chunk, with a Chart/Table toggle, keyboard access, theme tokens, phone layout, i18n and RTL.
8. `io.mindroom.ui` is bounded and never forces a sidecar or a split; when it would, the key is dropped and the text stays.
9. Review rulings: no `pending` signal and no skeleton; the JSON-text snapshot and the live-frame `ui` field stay; chart first, form second; the measured v4 prompt and the round-2 schema.

## 3. Choices made in this spec

| # | Choice | Why | Rejected |
|---|---|---|---|
| C1 | **Marker: the fallback is one fenced code block whose info string is `mindroom-ui-<id>`.** `formatted_body` then holds `<pre><code class="language-mindroom-ui-<id>">`. | One HTML node holds the whole multi-line fallback. It is written in the Markdown itself, so `body`, `formatted_body` and the live frame's `md` get it from one rule with no renderer change. `class` on `code` already passes the backend sanitizer, Element and Chat's sanitizer. `split` never cuts inside a fence. Chat's hook is the existing `pre` case. | A `🔧`-style text line: it marks one line, and a fallback is several, so the client would have to guess where it ends. A `<div data-mindroom-ui>` wrapper: raw tags in `body`, which agents and plain-text clients read, and a new attribute to let through the sanitizers of both repos. Matching the fallback text as the question card does: needs the fallback writer in TypeScript too. |
| C2 | **Widget id: the first 10 hex characters of the SHA-256 of the widget's canonical JSON; an id a literal marker block in the same text already uses is skipped.** | The tool-marker idea (token in the text, data in a content key) is kept, with a content id instead of a position: a reply is rendered again on every write, across pauses and restarts, and a content id stays the same without renumbering. It is also a stable key for a form's draft and answer, and a changed form gets a new id. Skipping used ids keeps a marker block that a model copied from history inert. | Ordinals `[N]` into `widgets[N-1]`: shift when text is added above and collide across spans. |
| C3 | **The snapshot holds each widget as its canonical JSON text** (`{"id", "json"}`). | Matrix event JSON forbids floats, and chart values and slider steps are often fractional (the reason `io.mindroom.ai_run` publishes fractions as strings). JSON text needs no number encoding. Only the backend produces canonical text and ids; Chat treats the id as opaque and validates the text. | Numbers as decimal strings inside a JSON object: a second, key-aware schema on the wire and a conversion in two languages. |
| C4 | **Live frames carry the same value as the content key**, in a new optional `ui` field of the frame documents, replaced or cleared as a whole. | One function produces the value for both transports. Chat copies `ui` into the live content as `io.mindroom.ui`, so one reader and one render path serve both. | Keeping the raw `ui` block in the live `md` for Chat to parse: two visible-text rules. |
| C5 | **Config name: `ui_widgets`** (`agents.<name>.ui_widgets`, `defaults.ui_widgets`, default `false`). | Matches the fence name `ui` and the key `io.mindroom.ui`. It gates the prompt and the transform, so an agent that was never told about widgets cannot produce one. | `widgets`, `native_widgets`, `enable_ui_widgets`. |
| C6 | **The chart fallback table shows at most 40 rows** (first 20, a count line, last 20). | Estimated with a prototype of the writer: a 200-point, 3-series chart costs about 37 KB as a final edit with the full table and 13 KB with the cap; the edit budget is 27 KB. Without the cap the charts that most need drawing would lose their widget. Tables of 40 rows or fewer are complete. | Full table always. |
| C7 | **On an edit, `io.mindroom.ui` is only in `m.new_content`; the shared edit builder leaves it out of the outer copy.** | Halves its cost, and holds for every edit envelope, including one wrapped late around a row prepared before its create was acknowledged. Chat reads only the selected revision's own content. | Stripping it in the size step only: the late-wrapped envelope never passes that step. |
| C8 | **Only a fence that starts in column 0 can be a widget.** | A column-0 fence is top level in CommonMark, outside any list or quote, so replacing it in place cannot break a container. Of the 728 `ui` fences in the eval's cached replies, 725 start in column 0 and 3 are indented under a list item. | The eval's "up to 3 spaces" line scan, which takes a fence inside a list item for top level; a full Markdown parse on every live tick. |
| C9 | **The transform runs on the text of every reply write, last, and is idempotent.** | A reply's records hold the model's raw text for streamed spans, and Stop, restart, approval-pause and multi-span writes render from those records, not from what was sent. Running the same pure function wherever a reply body becomes Matrix content covers them all without storing widgets in the lifecycle. Idempotent because its own marker blocks are inert input. | Rendering once and freezing the result: the lifecycle would need a second, display copy of every segment. |
| C10 | **Minimal mode: a `ui` context document plus one bootstrap line** when the switch is on. | A minimal agent never sees the document list unless it asks; without the line it cannot know widgets exist. About 12 tokens, only for opted-in agents. | Document only. |

## 4. Authoring format (the eval's round-2 schema)

One JSON object per fenced block (backticks or tildes) that starts in column 0 and whose info string's first word is `ui`.
A fence inside another code block, an indented or quoted fence, and an unclosed fence are not widgets.
Unknown keys are rejected everywhere; strings are never coerced to numbers; booleans are not numbers; numbers must be finite.
An optional key may be absent or `null`; the canonical JSON never has `null`.
Parsing never raises: malformed JSON, a number too large for a double, and a lone surrogate all mean "not a widget".

**chart** (Stage 1): `type:"chart"`, `kind` (`bar`, `line`, `area`, `pie`), `title`, `labels`, `series` of `{name, data}`, optional `x` and `y` axis titles.
Each series has one number per label; series names are unique; a pie has exactly one series with no negative value and a positive sum.
A number in `labels` is accepted and normalized to a string by the backend, so on the wire labels are always strings.

**form** (Stage 2): `type:"form"`, `title`, `fields`, optional `submit`.
Every field has `name` (unique), `kind`, `label`, optional `required` (boolean).

| Kind | Extra keys |
|---|---|
| `select`, `radio` | `options` (unique), optional `value` (one of the options) |
| `checkbox` | without `options`: one yes/no box, optional `value` boolean; with `options`: multiple choice, optional `value` list of options |
| `slider` | `min` < `max`, optional `step` > 0 with at most 10,000 steps, optional `value` in range, optional `unit` |
| `text` | optional `placeholder`, optional `value` |
| `date` | optional `min`, `max`, `value`, each a real `YYYY-MM-DD` date from year 0001 on, `min` not after `max` |

There is no password kind.
A field is rejected when its name (underscores read as spaces) matches the secret pattern, or its label matches it without a negation (`no`, `not`, `never`, `without`, `don't`, `do not`) in the 24 characters before the match, as in "Destination URL (no password)":
`pass(word|phrase|code)|\bpin( code| number)?$|\bsecret\b|api[ _-]?key|\b(access|auth|bearer|api)[ _-]?token|\bcvv\b`, case-insensitive.
This filter steers models; it is not a confidentiality boundary.

## 5. Wire format (version 1, both stages)

### 5.1 Canonical JSON and id

- Canonical JSON of a valid widget: the validated widget without absent optional keys, object keys sorted, separators `,` and `:` with no spaces, non-ASCII characters kept.
- A whole number below 2^53 in magnitude is written without a fraction (`120`, not `120.0`); any other number in its shortest round-trip form.
- `id` = the first 10 lowercase hex characters of SHA-256 over the UTF-8 bytes of that text.
- If the text being rendered already holds a marker block with that id, the id is instead taken from the text followed by `#1`, then `#2`, until it is free.
- Two identical blocks authored in one text share one id and one entry; both places draw it.
- Chat never computes an id.

### 5.2 The content key

```json
"io.mindroom.ui": {
  "version": 1,
  "widgets": [
    {"id": "df4d729a5d", "json": "{\"kind\":\"line\",\"labels\":[\"Jan\",\"Feb\",\"Mar\"],\"series\":[{\"data\":[120,180.5,150],\"name\":\"Signups\"}],\"title\":\"Signups\",\"type\":\"chart\",\"y\":\"users\"}"}
  ]
}
```

- `widgets`: entries in the order their blocks appear, at most 8, each `id` once, and at most 16,384 UTF-8 bytes over all `json` strings together.
- The key is absent when there is no entry.
- A reader ignores the whole value on another `version`, more than 8 entries, a repeated id, or more than 16,384 bytes.
- An entry whose JSON is valid but of a `type` the reader does not draw is not an error: its place shows the fallback text.

### 5.3 The marker and the fallback text

Each valid block is replaced, in place, by a fenced block with the info string `mindroom-ui-<id>` whose content is the fallback text.
The fence is backticks, one longer than the longest backtick run in the content and at least three, and a blank line separates the block from the text on each side.
Every string from the widget is written with its whitespace runs collapsed to one space, so no value spans lines or closes the fence.

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
- Numbers print as in the canonical JSON.
- More than 40 rows: the first 20, the line `... <n> more rows ...`, the last 20.

Form fallback (Stage 2):

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
This is `m.new_content` of the edit; the edit's outer copy has the same `body` and no `io.mindroom.ui`.

### 5.5 In progress, and live frames

- While the reply streams, closed valid blocks are already replaced, and the text is cut where an open column-0 `ui` fence begins (its opening line complete).
- Nothing signals the open fence, and Chat shows nothing for it until it closes.
- Durable progress updates carry the value as `io.mindroom.ui` (in `m.new_content` when the update is an edit).
- Live frames (`io.mindroom.live`) carry the same value as `ui`: present in a snapshot when there is a value; in a delta, `ui` replaces the whole value and `null` clears it. Frame version stays 1.
- A frame that does not fit its size limit is sent without `ui` before anything else is cut; the fallback block shows until a frame that fits brings it back.
- A client older than this field ignores `ui`; a delta that carries nothing else makes it wait for the next snapshot.
- On the final update of a reply that ended inside an open `ui` fence, the partial block stays an ordinary code block.
- A Stop, restart or error note is written below what the reply had shown, so the hidden partial block is not revealed and cannot swallow the note.

### 5.6 A form answer (Stage 2)

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
- The mention link and `m.mentions` come from the form event's actual sender, never from anything in the widget.
- The relation is what Chat's `getMessageRelation(eventId, undefined, threadId)` builds; outside a thread it is a plain reply.
- `values` by kind: option string; boolean; list of option strings (an empty list is an answer); number; text; `YYYY-MM-DD`.
- A number that is not a safe integer is written as its decimal string (`"2.5"`), because event JSON forbids floats.
- A field left unanswered (no choice, empty text, no date) is absent from `values` and from the list.
- Body lines are `- <label>: <value>`, each on one line: `yes`/`no` for a box, options joined by `, ` or `none`, the slider value followed by its unit.
- With no line at all the body ends with `(no answers)`.
- Every label, value and name put into `formatted_body` is HTML-escaped.
- The built event is measured before it is sent: over the event limit it is sent without `formatted_body`, and if still over it is not sent, the draft stays and the form says the answers are too long.
- The fixed words in the body are English on the wire, like the canvas answer line.

## 6. Backend (mindroom)

### 6.1 Modules

- `src/mindroom/ui_widgets/models.py`: the eval schema as strict pydantic models, the limits, and `parse_widget(json_text)`; Stage 1 has `chart` only.
- `src/mindroom/ui_widgets/render.py`: the column-0 fence scan, canonical JSON, id, fallback writer, and `render_ui_widgets(text, *, in_progress)`, which returns the visible text and the `io.mindroom.ui` value (or `None`).
- The result for one block is memoized by the block's text, so a live tick does not validate and hash closed widgets again.
- `UI_WIDGETS_CONTENT_KEY = "io.mindroom.ui"` in `constants.py`.
- No handler for `io.mindroom.ui_response`: the answer is a normal mention turn and the agent reads its body.

### 6.2 Where the transform runs

Checked in the code: a streamed span's record holds the raw accumulated text (`_progress_state` forwards `accumulated_text`, and the gateway's stream hooks build the presentation from it), and Stop, restart, approval-pause and multi-span writes render that record and send it through `send_text` or `edit_text`.
So the transform cannot run in one place and be frozen; it runs wherever a reply body becomes Matrix content, only when `ui_widgets` resolves true for the replying agent:

1. `streaming._visible_text`, for durable stream edits and `live_state`, after the interactive-question formatting. Both stream constructors pass the switch: `send_streaming_response` and `stream_progress_edits` (the one a resumed reply uses).
2. `DeliveryGateway.send_text` and `edit_text`, for a request that carries a `reply_write`, just before `format_message_with_mentions`, with `in_progress=False`. This covers the non-streamed final, every note write, the approval pause and the multi-span body.
3. The gateway's stream hooks record the progress text cut at an open `ui` fence, so a later note is appended below what was shown.

Rules of the function:

- It runs last, after the interactive formatting, so that pass never scans fallback text.
- A valid closed block becomes its fallback block and gets an entry while the reply has fewer than 8 entries and stays within 16,384 bytes; past that the fallback alone shows.
- An invalid closed block is left exactly as written; a final write logs a warning with the validation errors and never the block's text.
- A marker block already in the text is ordinary text: it gets no entry and its id is not reused (C2).
- Text without a column-0 `ui` fence is returned as the same object.

What follows from running it on every write:

| Write | Result |
|---|---|
| Stream progress and final | Closed blocks are widgets; an open fence is hidden while streaming and a code block on the final |
| Stop, restart or error note | Rendered from the record: closed blocks are widgets again, the open fence was never recorded, the note is below |
| Approval pause, resume, second pause, final | Each write renders the whole recorded reply, so earlier spans keep their widgets |
| A body rebuilt from text Matrix already showed (a marker block in it) | That block stays readable text without a widget |

Scope of the first slice: an agent's own replies only; not teams, the router, `matrix_message`, the OpenAI-compatible API or Slack delivery, where a `ui` block stays a code block.
`thread_move` copies an allowlist of content keys, and `io.mindroom.ui` is not added to it.

### 6.3 Size and transport

1. `build_matrix_edit_content` leaves `io.mindroom.ui` out of the outer copy (C7).
2. `fit_ui_widgets_to_ordinary_event` in `matrix/large_messages.py` runs wherever `fit_reasoning_to_ordinary_event` runs, just before it: if the event with the key and without its reasoning is over the limit that would move it to a sidecar or split it (27,000 bytes for an edit, 55,000 otherwise, the first-segment target under `split`), or over the 64,000-byte hard limit as delivered, the key is removed. Reasoning is then fitted into what is left, as today.
3. A reply row prepared before its create was acknowledged is stored as a standalone payload and wrapped into an edit when claimed, with no second fit. For such a row the key is fitted against the edit it will become, before the row is frozen.
4. The throttle for oversized progress edits (`should_send_oversized_nonterminal_streaming_edit`) sizes the edit as step 2 would send it, so a droppable key never slows ordinary updates.
5. Live frames: the publisher drops `ui` first when a frame does not fit, and records the documents it sent, so the next frame that fits restores it (section 5.5).

So a widget never rides on a sidecar preview or a split reply, and the fallback text is what large-message handling sees.

### 6.4 Config, prompt, documents

- `agents.<name>.ui_widgets: bool | None` and `defaults.ui_widgets: bool = False`; `config.resolution.ui_widgets(config, name)` returns the agent's value, else the default, and `False` for a team, the router or an unknown name.
- The field is live (no restart) and listed in the dashboard's Responses section.
- `UI_WIDGETS_PROMPT` in `prompts.py`, overridable like the other prompts, is appended right after `INTERACTIVE_QUESTION_PROMPT` when both `include_interactive_questions` and `ui_widgets` hold.
- Minimal mode: the prompt is the context document `ui`, the `mindroom-agent context` help names it, and the bootstrap gains the line ``Charts in replies: read `mindroom-agent context read ui` first.`` (Stage 2: "Charts and forms").
- User docs: a short `docs/widgets.md`, one row in `docs/configuration/agents.md`, and one sentence each in `docs/streaming.md` and `docs/interactive.md`.
- `docs/widgets.md` says that Chat draws widgets for `mindroom_` accounts on the viewer's homeserver, so the deployment must reserve that namespace.

### 6.5 Prompt text

Stage 2's `UI_WIDGETS_PROMPT` is the measured v4 prompt, `genui-eval/prompt_fenced.md` (400 tokens), verbatim:

`````text
To show a native widget, write one JSON object in a ```ui code block. The default is prose; use a widget only when it clearly beats text:

- `chart`: the user asks for a chart or plot, or wants a trend or breakdown of numbers you have.
- `form`: you need several answers before continuing. They arrive as the user's next message. For a single choice use ```interactive.

```ui
{"type":"chart","kind":"line","title":"Signups","labels":["Jan","Feb","Mar"],"series":[{"name":"Signups","data":[120,180,150]}],"y":"users"}
```

```ui
{"type":"form","title":"Plan the trip","fields":[
{"name":"pace","kind":"radio","label":"Pace","options":["Relaxed","Packed"]},
{"name":"interests","kind":"checkbox","label":"Interests","options":["Food","Museums"]},
{"name":"budget","kind":"slider","label":"Daily budget","min":50,"max":500,"value":150,"unit":"USD"}]}
```

Rules:
- Chart kinds: bar, line, area, pie. Field kinds: select, radio, checkbox, slider, text, date. Use only the keys shown. Max 8 series, 200 points, 12 fields; for more, answer in prose, don't sample or average.
- Never invent data. Chart only numbers the user gave or a tool returned, unchanged; otherwise say you don't have them.
- Write prose that stands alone: state the takeaway (for a form: what you need) in a sentence, since some clients show only text. Don't write "here is the chart".
- No widget for definitions, how-tos, code questions, yes/no answers, single numbers or small comparisons (use a table).
- No password or secret fields.
- To show widget JSON as code, use ```json, not ```ui.
`````

Stage 1's prompt is v4 with every form part removed.
It is derived, not measured as a whole; its chart cases are rerun through the eval before the switch is turned on for more than the pilot agent.

`````text
To show a native widget, write one JSON object in a ```ui code block. The default is prose; use a widget only when it clearly beats text:

- `chart`: the user asks for a chart or plot, or wants a trend or breakdown of numbers you have.

```ui
{"type":"chart","kind":"line","title":"Signups","labels":["Jan","Feb","Mar"],"series":[{"name":"Signups","data":[120,180,150]}],"y":"users"}
```

Rules:
- Chart kinds: bar, line, area, pie. Use only the keys shown. Max 8 series, 200 points; for more, answer in prose, don't sample or average.
- Never invent data. Chart only numbers the user gave or a tool returned, unchanged; otherwise say you don't have them.
- Write prose that stands alone: state the takeaway in a sentence, since some clients show only text. Don't write "here is the chart".
- No widget for definitions, how-tos, code questions, yes/no answers, single numbers or small comparisons (use a table).
- To show widget JSON as code, use ```json, not ```ui.
`````

## 7. Client (mindroom-chat)

### 7.1 Reading and trust

- A widget is drawn only inside a scope that one provider component creates, and that provider checks for itself, at render time: the event is not redacted, and `isMindroomAgentUserIdForViewer(event sender, the current client's user id)` holds.
- It does not rely on a boolean passed in, on the `io.mindroom.*` metadata heuristic, a display name, a marker or a label.
- `io.mindroom.ui` also joins `AGENT_ONLY_CONTENT_KEYS` as a second line; that list trusts agents of any signed-in account, which is why it is not the gate.
- That predicate is a localpart prefix on the viewer's homeserver, not proof that the account runs MindRoom; the deployment must reserve the namespace.
- The value is read from exactly the content that supplies the body shown: the selected edit's own `m.new_content`, or the event's own content when it has no readable edit by its sender.
- Chat today copies every `io.mindroom.*` key a newer edit lacks from older edits and from the edit's outer content (`copyResolvedEditMetadata`); `io.mindroom.ui` and `io.mindroom.ui_response` are excluded from that copy, so an edit without the key removes the widget.
- An edit that cannot be read yet leaves the last readable revision as a whole; a redacted original shows nothing, even with a readable edit or live frames held.
- Live frames already pass the binder's checks (an agent of the current viewer, the same sender as the reply's event, encrypted in an encrypted room), and the live content replaces the durable `io.mindroom.ui` rather than merging with it.
- `readUiWidgets(content)` enforces section 5.2; `parseChartWidget(json)` is the strict validator for the canonical wire text, with the backend's bounds and code-point lengths. It is stricter than authoring in two ways: labels must be strings and optional keys must be absent.
- A marker without a valid entry, or with an entry this build does not draw, renders as a plain code block holding the fallback text, with no language label.
- Nothing in a widget is HTML, a URL to load, an SVG reference or code; every string renders as text.
- Link previews are found by scanning the whole body; `mindroom-ui-` blocks are cut from that input, so a label such as `example.com` under a chart produces no preview card or request. Prose around the widget keeps its previews.

### 7.2 Placement

- The base HTML parser's `pre` case checks for `code.language-mindroom-ui-<id>` and renders `UiWidgetSlot` with the code block as its fallback.
- Without a scope (other senders, previews, plugins) the slot renders the fallback, so a pasted marker does nothing.
- Durable replies and live replies take the same path, because the live content gets `io.mindroom.ui` from the frame's `ui`.
- Validation and the chart load as one lazy chunk; until it arrives the slot shows the fallback block.
- Previews (thread cards, Activity, notifications, search) keep using `body`, so they show the fallback text.

### 7.3 Chart (Stage 1)

- A bordered card in the question card's frame (variant B); the frame is one style, so A or C is a change to it alone.
- Header: title, the `y` title as a subtitle, and a Chart/Table toggle (two buttons with `aria-pressed`; icons only under 420 px).
- Plot: SVG drawn at the card's measured width with fixed text size; bar (grouped), line, area and pie (a donut with a value and percent legend).
- The plot is one tab stop: arrow keys move between labels, Home and End jump, and the tooltip follows focus or hover and is announced politely.
- Table view is a real `<table>` with the labels as row headers; it is what screen readers get.
- Numbers use the app's locale formatting; colors come from `theme/colors.css.ts` tokens, 8 series colors, light and dark.
- RTL: the card lays out with logical properties and the plot itself stays left to right.
- Extreme values: ticks are computed by index and capped at 12, never by repeated addition; when the scaled values, the domain or a pie's sum are not finite, the card shows the table alone.

### 7.4 Form (Stage 2)

- The same card frame; native controls with bound labels; one column under 420 px; a line the app owns under Submit says the answers are sent as a message in this conversation, whatever the `submit` label says.
- Initial values are the widget's `value`s; a slider without one starts at `min` and shows the value the control really holds.
- Draft values live in memory only, keyed by account, room, event and widget id; they are cleared on sign-out and when the answer is sent, and kept when sending fails.
- Submit is offered when the reply's status is absent or `completed` (not `approval_pending`, stopped or failed), the view is not read-only, the event id is confirmed, the viewer is not an agent, and the timeline shown reaches its live end; the conditions are checked again at the click, and a second click while sending does nothing.
- `required` uses native validation; a required multiple choice needs at least one box.
- Submit sends the event of section 5.6 with one transaction id and no confirm step.
- Answered means: a message by the current user in the same room, readable, not redacted, `m.text`, that replies to the form's event and whose own `io.mindroom.ui_response` names that event and this widget id. An edit of the answer changes nothing, and the values are validated against the form before they are shown.
- A local echo still sending folds the card at once; one that fails or is cancelled brings the form back with its draft; the acknowledged event keeps it folded.
- The plan is recomputed on local echo changes, decryption, redaction and pagination, in the room view and in thread views.
- An answered form is a one-row receipt (check, title, "Answered", chevron) that opens to the label and value list, built from the disclosure styles tool approvals use.
- Other people's answers do not mark it answered for the viewer.

## 8. Limits

| Limit | Value | Enforced by |
|---|---|---|
| Block JSON text | 16,384 UTF-8 bytes | backend, client |
| Series, points, fields, options | 8, 200, 12, 12 (at least 2 options) | backend, client |
| `title` 80; series name, label entry, `x`, `y` 40; field `label` and `placeholder` 120; option 60; `submit` 24; `unit` 16; text `value` 200 | code points, at least 1 except text `value` | backend, client |
| Field `name` | `^[a-z][a-z0-9_]{0,31}$` | backend, client |
| Slider steps | 10,000 | backend, client |
| Entries per reply | 8 | backend, client |
| All `json` strings of one reply | 16,384 UTF-8 bytes | backend, client |
| Fallback table rows | 40 | backend |
| Chart ticks | 12 | client |
| Typed text answer | 500 characters | client |
| Answer event | the event size limit, measured as built | client |

## 9. Stages

| | Stage 1: chart (built now) | Stage 2: form (later) |
|---|---|---|
| Backend | `ui_widgets` switch, chart schema, render and fallback, every write path, size and transport rules, live `ui`, chart-only prompt, docs | form schema with the negated secret filter, form fallback, the v4 prompt, the bootstrap line's wording |
| Chat | reader, trust and effective-content rules, slot, link-preview rule, chart, fallback for entries it cannot draw | form validator, form card, drafts, answer builder with its size guard, answered plan, receipt |
| Wire | version 1 as in section 5 | unchanged |
| Mixed versions | A Stage 2 backend with Stage 1 Chat: the form shows as its fallback text and the user answers in words | A Stage 1 backend never emits a form entry |

## 10. Deferred

- Widgets in team replies, `matrix_message`, the OpenAI-compatible API, Slack and the Apple Watch app.
- A signal and placeholder for a widget that is still streaming.
- A preview rule that shows only the widget's title line.
- A "Form data" disclosure on the answer message, draft persistence across reloads, a confirm step.
- More widget kinds, layout, expressions, images, maps, follow-up chips, inline canvas submit, a plugin hook for code-block renderers.
- Stacked bars, scatter, time axes, decimation of more than 200 points.
- Enabling by default, and local models (Qwen's standalone prose is 34/38 with v4; Gemma is not measured).
- The secret filter also excuses a secret word when a negation for another word precedes it ("No password, but your API key"); tightening it needs the eval's blocks rerun.

## 11. Test strategy

- **Shared contract fixture** `ui-widgets-v1.json`, byte-identical in both repos (backend `tests/ui_widgets/fixtures/`, client `src/app/messages/widgets/__fixtures__/`), as the live-frame fixtures already are. Cases carry `stage`.
  - `widgets`: authored JSON with `valid`, and for valid ones `canonical` and `id`. Python pins all four. TypeScript must accept every `canonical` and reject every invalid `json`; it never compares number spelling.
  - `wire_invalid`: texts only the client can meet (a numeric label, an explicit `null`), which it must reject.
  - `renders`: `markdown`, `in_progress`, expected `text`, `ui` and `html`. Python pins the transform and `markdown_to_html(text)`. TypeScript pins `readUiWidgets(ui)` and that its sanitized parser and its live Markdown both yield one `code.language-mindroom-ui-<id>` per entry holding the fallback text; it does not compare whole HTML.
  - `responses` (Stage 2): a form, the answers and the expected event of section 5.6, pinned by TypeScript.
  - The two examples of section 5 are pinned by hand from this spec (they were computed with a prototype, not with the implementation); the rest is generated by the implementation and reviewed.
  - Boundary families: bytes at 16,384 and 16,385 per block and over all entries; fractional and exponent numbers; `1e400`; an integer too large for a double; a boolean as a number; lengths in emoji; duplicate keys, names and ids; absent against `null`; extreme finite and subnormal values.
- **Live-frame fixture** `live-frames-v1.json`: three new cases (a delta that sets `ui`, a delta that clears it, a snapshot with it), in both repos; the backend's frame test ingests `ui`.
- **Order across repos**: the backend lands its schema, render and frame tasks first (both fixtures complete); Chat copies both files and checks `sha256sum`.
- **Backend tests** named in the plan: schema, scan, fallback, ids, both stream constructors, the write hooks, the edit builder, the size step at real limits, the late-wrapped row, the throttle, the frame publisher, and the lifecycle from persisted records (pause, resume, second pause, final; Stop and restart with frames on and off; an open fence after a closed one).
- **Client tests** named in the plan: reader, validator, the scope's trust cases through the real renderer and binder, effective content with real `MatrixEvent` edits, link previews switched on, chart geometry at extremes, the table, live set and clear.
- **Client e2e**: a `#chart` stub scenario against a backend checkout that has the feature. A run that skips because the backend lacks it does not count as passed; the pull request records the backend commit it ran against.
- **Fresh live test, a release gate** (never lab or production): from `~/repos/dev`, `just live up --chat <chat worktree> --backend <backend worktree>` (`scripts/live.py`), set `ui_widgets: true` on the Helper in the printed `config.yaml`, drive it with `just live ask` and a small Playwright script, then `just live down`. The report names both commits.

| # | Scenario (Stage 1) | Pass when |
|---|---|---|
| L1 | Ask for a chart of numbers given in the message | Nothing shows while the fence is open, the card appears when it closes, between durable edits too; the final event has the entry; reload shows the card |
| L2 | Read L1's event over the API | `body` holds the table, no JSON; `io.mindroom.ui` only in `m.new_content` |
| L3 | Fractional data in an unencrypted and an encrypted room | Both events are accepted by the homeserver and render |
| L4 | A block with an unknown type, and a `form` block | Each stays a code block, no error banner |
| L5 | Stop while a `ui` fence is open, after an earlier chart closed | The earlier chart is still a card, no partial JSON, the note is below the text |
| L6 | A tool approval in the middle of a reply that already drew a chart; approve | The chart is a card during the pause and after the final |
| L7 | Restart the backend mid-reply after a chart closed | The interrupted reply keeps its card and its note |
| L8 | A long reply (over the edit budget) that contains a chart | The key is dropped, the table shows, no sidecar is caused by the key |
| L9 | The same prompts with `ui_widgets` off | No widget prompt in the request, a `ui` block stays code |
| L10 | Regenerate L1's reply into prose | The card is gone |
| L11 | A chart whose labels are domain names, link previews on | No preview card and no preview request for the labels |
| L12 | A minimal-mode agent with the switch on | It reads the `ui` document and produces a valid block |

A failure is rerun on a deployment without `--backend` to tell a regression from an existing bug.
Stage 2 adds the form round trip, the receipt on a second session, a failed send, and an oversized answer.

## 12. Risks

- The fallback is a code block in other clients; a monospace table suits a chart, and is plain but readable for a form.
- A model may imitate a `mindroom-ui-` block it sees in thread history; it is then an ordinary code block and its id is never reused.
- The reply lifecycle was read, not run: section 6.2's table is what the plan's lifecycle tests must prove before the feature is called done.
- Initial CSS had about 600 bytes of gzip headroom when last measured (2026-11-13), so everything but the slot lives in the lazy chunk.
- Stage 1's prompt is a cut of the measured one.

## Appendix A. Review response (2026-11-18)

Reviews: Sol (GPT-6.2 Sol), findings S1 to S6 and S-minor; Astra (GPT-6.2 Astra), findings A1 to A14. Each was checked against the code at the commits above.

**Accepted**

- S1, A5 (trust): verified that `AGENT_ONLY_CONTENT_KEYS` trusts agents of any signed-in account and that the render gate uses the current client. The scope provider now checks sender and current viewer itself; namespace reservation is documented (7.1, 6.4).
- A1, S2 (a removed widget returns): verified, `copyResolvedEditMetadata` copies every `io.mindroom.*` key from older edits and the wrapper. Both keys are excluded; tests use real edits (7.1).
- A2, S6 (injection and requests), in part: verified that link previews scan the whole body, fences included. Marker blocks are cut from preview input; answers escape every interpolation and take the mention from the sender; the form shows an app-owned line about where answers go (7.1, 5.6, 7.4).
- A3, S4 (pause, Stop, restart): verified that records hold raw text and that note, pause and multi-span writes render them through `send_text` and `edit_text`, and that a resumed reply uses `stream_progress_edits`. The transform now runs at every write, the open fence is not recorded, both constructors get the switch (C9, 6.2).
- A4, S3 (size at real boundaries): verified the late-wrapped row and the unfitted throttle. The edit builder omits the outer key, the late row is fitted as its edit, the throttle and the frame publisher account for the key (C7, 6.3).
- A9 (measured contract): the v4 prompt and the round-2 schema are now the text of this spec; `null`, year 0001, huge integers and lone surrogates are specified (4, 6.5).
- A10 (numeric work): number printing is bounded, the aggregate byte limit is enforced on both sides, geometry is index-based with a table fallback (5.1, 5.2, 7.3).
- A11 (ids and top level): used marker ids are skipped; only column-0 fences count; the transform runs after the interactive pass (C2, C8, 6.2).
- A12 (transport): frames drop `ui` first and restore it; block results are memoized; older clients resync on a snapshot (5.5, 6.1, 6.3).
- A13, S-minor (fixtures and order): three live cases, backend contract tasks first, hand-pinned oracles, semantic comparison instead of HTML bytes, live test as a gate with recorded commits (11).
- A14: only an identified open `ui` fence is hidden; size tests assert both inequalities; a skipped e2e run is not a pass (5.5, 11).
- S5, A6, A7, A8 (form receipts, drafts, authority, answer size): accepted for Stage 2 and written into 5.6 and 7.4.
- Scope: `pending` and the skeleton are gone; chart first, form second.

**Declined**

- A2, the part asking to switch off linkification and previews for URLs inside the user's answer message: it is the user's own ordinary message, and an agent can already put a URL in its prose, so the widget adds no new request.
- A3, the part asking to store display segments separately from run input in the reply records: the same guarantees come from running an idempotent transform at the write boundary, without a second copy of each segment in the lifecycle.
- A6, the part asking to follow edits of an answer: Sol asked for the opposite, and the answer's own content is the simpler rule; an edit of the answer changes nothing.
- A9, tightening the secret filter for a second, unnegated match: the round-2 schema is used as measured; listed in section 10.
- A11, a real Markdown token scan for top-level fences: the column-0 rule gives the same guarantee for 725 of 728 measured fences without parsing on every live tick.
- Scope cuts not taken: unaligned fallback columns, a plain pie, unlabelled-toggle and tick simplifications (they depart from the mockups for a few lines each), and leaving the switch out of the dashboard (one line beside `show_reasoning`).
