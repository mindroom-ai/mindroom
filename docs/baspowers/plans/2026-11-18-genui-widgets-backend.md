# Native `ui` widgets: backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** baspowers:subagent-driven-development (six tasks that share one wire contract with a second repo; a wrong byte in the fixture or a missed delivery path ships a widget that only breaks in Chat, so each task gets its own review).

**Goal:** Turn valid `ui` blocks in an opted-in agent's reply into a readable fallback block plus `io.mindroom.ui`, on durable edits, live frames and non-streamed finals.

**Architecture:** A new `mindroom.ui_widgets` package validates blocks (pydantic port of the eval schema) and renders the visible text and the content value in one pure function. `streaming._visible_text` and the non-streamed final call it when `ui_widgets` resolves true for the agent; the value rides in the event content and in `LiveDocs.ui`. A size step next to the reasoning fit drops the key before it could cause a sidecar or a split.

**Tech Stack:** Python 3.13, pydantic v2, markdown-it-py, pytest, nix-shell + uv.

**Spec:** `docs/baspowers/specs/2026-11-18-genui-widgets-design.md` (sections 4, 5, 6, 8 and 10 are the contract; read it first).

## Global Constraints

- Content key `io.mindroom.ui`, constant `UI_WIDGETS_CONTENT_KEY` in `src/mindroom/constants.py` next to `UI_ACTION_CONTENT_KEY`.
- Value shape exactly `{"version": 1, "widgets": [{"id", "json"}], "pending"?}`; `pending` in `chart|form|widget`; optional keys omitted, never `null`; no key when there is no widget and nothing pending.
- Marker: fenced block with info string `mindroom-ui-<id>`; `id` = first 10 hex of SHA-256 of the canonical JSON (sorted keys, `(",", ":")` separators, `ensure_ascii=False`, whole numbers without a fraction, absent optionals left out).
- Limits: `MAX_BLOCK_BYTES = 16_384`, `MAX_SERIES = 8`, `MAX_POINTS = 200`, `MAX_FIELDS = 12`, `MAX_OPTIONS = 12`, `MAX_WIDGETS = 8`, `MAX_UI_BYTES = 16_384`, `FALLBACK_MAX_ROWS = 40`.
- The event content must hold no float anywhere: widgets travel as JSON text.
- A reply text with no `ui` fence, and any reply of an agent without `ui_widgets`, is returned and delivered byte-identical to today.
- No change to what the model sees unless `ui_widgets` is on (prompt cache stays byte-identical for every other agent).
- `ui_widgets` config field: `agents.<name>.ui_widgets: bool | None`, `defaults.ui_widgets: bool = False`.
- `UI_WIDGETS_PROMPT` is `genui-eval/prompt_fenced.md` verbatim (spec 6.5; if that file is newer than the spec, the file wins).
- Source to port: the eval's `schema.py` and `test_schema.py` (local files in the DevAgent workspace under `genui-eval/`, not in this repo); the spec's section 4 and 8 restate every rule.
- The fixture `tests/ui_widgets/fixtures/ui-widgets-v1.json` is shared with mindroom-chat (`src/app/messages/widgets/__fixtures__/ui-widgets-v1.json`); the two copies must stay byte-identical (`sha256sum` both after any change). Same for `tests/live_frames/fixtures/live-frames-v1.json`.
- Tests: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest <files> -x -n 0 --no-cov'`; run each new test file once with the repo's default addopts.
- Docs: one sentence per line; user docs get only sentences that answer a user question.
- Never `git add .`; add files by path.

## Review Focus

- A reply that continues after a tool approval or restart: the earlier span's marker block is already in the text; it must stay as written, get no entry, and never collide with a new widget (Task 2 test `test_an_existing_marker_block_is_left_alone`, Task 4 resumed-stream test).
- An unencrypted room: the final content must serialize with no float even for `180.5` and slider `step: 0.5` (Task 4 test walks the content).
- A title or label holding backticks or newlines must not close the fallback fence or add lines (Task 2).
- Stop or an error while a `ui` fence is open: the terminal update has no `pending` and shows the partial block as code (Task 4).
- A chart inside a reply near the edit budget: the key is dropped, the text is intact, and the key itself never triggers a sidecar or a split (Task 5).

---

### Task 1: Schema port and the fixture's `widgets` section

**Files:**
- Create: `src/mindroom/ui_widgets/__init__.py` (docstring only), `src/mindroom/ui_widgets/models.py`
- Create: `tests/ui_widgets/__init__.py`, `tests/ui_widgets/test_models.py`, `tests/ui_widgets/fixtures/ui-widgets-v1.json`

**Interfaces:**
- Produces: the limit constants above; models `Series`, `Chart`, `Select`, `Radio`, `Checkbox`, `Slider`, `Text`, `Date`, `Form` (strict, `extra="forbid"`); `parse_widget(json_text: str) -> tuple[Chart | Form | None, list[str]]`, which never raises.

- [ ] **Step 1: Write the fixture's `widgets` section.** File shape: `{"version": 1, "widgets": [...], "renders": [], "responses": []}`; each case `{"name", "json", "valid"}` and, when valid, `"canonical"` and `"id"` (filled in Task 2). Bases: `CHART = {"type":"chart","kind":"bar","title":"Orders","labels":["Q1","Q2"],"series":[{"name":"Orders","data":[120,180.5]}]}` and `FORM` = the six-field form of `test_schema.py` (select, slider, checkbox with options, checkbox without, date with `min`, text with `placeholder` and `required:false`).

| Valid cases | Invalid cases |
|---|---|
| `CHART`; `CHART` + `x`,`y`; 200 labels and points; `labels: [2021, 2022.0]`; `FORM`; a text field with a 120-character label; a label of 120 emoji; slider named `max_tokens` labelled "Max tokens"; text labelled "Destination path or URL (no credentials)"; the spec's 5.4 chart and 5.6 form | `CHART` + `"color":"red"`; one label for two values; `kind: scatter`; data `["1","2"]`; data `[true,2]`; pie with two series; pie with `[-1,2]`; pie with `[0,0]`; two series named alike; 201 points; 9 series; `NaN` in data; `1e400` in data; `labels: [true,null]`; field kind `password`; label "Your password"; name `api_key`; label "GitHub access token"; a 121-character label; slider `min 5 max 1`; slider `min "1"`; slider with 10,001 steps; select with one option; select `value` not in options; date `12/01/2026`; date `2026-02-30`; name `A b`; checkbox `value: ["x"]` without options; a field with an extra `type` key; 13 fields; two fields named alike; `[1]`; `{oops`; `{"type":"map"}`; a chart padded with spaces to 16,385 bytes |

- [ ] **Step 2: Write the failing test** in `tests/ui_widgets/test_models.py`:

```python
@pytest.mark.parametrize("case", _FIXTURE["widgets"], ids=[c["name"] for c in _FIXTURE["widgets"]])
def test_parse_widget_agrees_with_the_shared_fixture(case: dict[str, object]) -> None:
    widget, errors = parse_widget(case["json"])
    assert (widget is not None) is case["valid"], errors
    assert bool(errors) is not case["valid"]

def test_parse_widget_never_raises_on_junk() -> None:
    for text in ("", "null", "[" * 100_000, '{"type":"chart","series":7}', "\ud800"):
        assert parse_widget(text)[0] is None
```

- [ ] **Step 3: Run it.** Expected: FAIL, `ModuleNotFoundError: mindroom.ui_widgets`.
- [ ] **Step 4: Implement `models.py`** by porting the eval's models, `SECRET_RE`, the pre-pass that stringifies numeric labels and widens ints where floats are expected, the constant rejection, and `parse` as `parse_widget`. Measure the byte limit so that a lone surrogate cannot raise. Leave the eval's `scan`, `normalize` and `validate` out.
- [ ] **Step 5: Run it.** Expected: PASS. Also run `just check-module-privacy`.
- [ ] **Step 6: Commit** (`git add` the five files; message `feat(ui-widgets): strict chart and form schema`).

### Task 2: Canonical JSON, fallback text and `render_ui_widgets`

**Files:**
- Create: `src/mindroom/ui_widgets/render.py`, `tests/ui_widgets/test_render.py`
- Modify: `src/mindroom/constants.py`, `tests/ui_widgets/fixtures/ui-widgets-v1.json`, `tests/ui_widgets/test_models.py`

**Interfaces:**
- Consumes: `parse_widget`, `Chart`, `Form` from Task 1.
- Produces:
  - `UI_WIDGETS_CONTENT_KEY = "io.mindroom.ui"`
  - `canonical_widget_json(widget: Chart | Form) -> str`, `widget_id(canonical: str) -> str`
  - `fallback_block(widget: Chart | Form, widget_id: str) -> str` (the whole fenced block, no trailing newline)
  - `@dataclass(frozen=True, slots=True) class UiRender: text: str; content: dict[str, object] | None`
  - `render_ui_widgets(text: str, *, in_progress: bool) -> UiRender`

- [ ] **Step 1: Write the failing tests.**

```python
CHART = '{"type":"chart","kind":"line","title":"Signups","labels":["Jan","Feb","Mar"],"series":[{"name":"Signups","data":[120,180.5,150]}],"y":"users"}'

def test_canonical_json_and_id_match_the_spec() -> None:
    widget, _ = parse_widget(CHART)
    canonical = canonical_widget_json(widget)
    assert canonical == '{"kind":"line","labels":["Jan","Feb","Mar"],"series":[{"data":[120,180.5,150],"name":"Signups"}],"title":"Signups","type":"chart","y":"users"}'
    assert widget_id(canonical) == "df4d729a5d"

def test_chart_fallback_is_the_spec_table() -> None:
    widget, _ = parse_widget(CHART)
    assert fallback_block(widget, "df4d729a5d") == (
        "```mindroom-ui-df4d729a5d\nSignups (line chart, users)\n     Signups\nJan      120\nFeb    180.5\nMar      150\n```"
    )

def test_text_without_a_ui_fence_is_returned_as_is() -> None:
    text = "Hello\r\n\n```python\nx = '```ui'\n```\n"
    render = render_ui_widgets(text, in_progress=False)
    assert render.text is text and render.content is None
```

  Plus, in the same file: `test_form_fallback_is_the_spec_text` (spec 5.3 form, id `0fd993d70d`); `test_a_title_of_backticks_gets_a_longer_fence` (title "```" gives a 4-backtick fence and a block that `markdown_to_html` renders as one `<pre>`); `test_strings_are_collapsed_to_one_line` (label `"a\n```\nb"` stays on its line); `test_a_45_point_chart_shows_40_rows` (line 24 of the block is `... 5 more rows ...`, after the fence, the title, the header and 20 rows); `test_entries_stop_at_8_and_at_16384_bytes` (9 small charts give 9 marker blocks and 8 entries; two 9 KB charts give two blocks and one entry); `test_an_existing_marker_block_is_left_alone`; `test_identical_widgets_share_one_entry`.
- [ ] **Step 2: Add the fixture's `renders` cases** (`{"name", "markdown", "in_progress", "text", "ui", "html"}`, `ui` null when there is no value) and the parametrized test `test_render_agrees_with_the_shared_fixture`, which asserts `render.text == case["text"]`, `render.content == case["ui"]` and `markdown_to_html(case["text"]) == case["html"]`. Cases: the 5.4 reply; the 5.6 form; an unknown `type` (unchanged, null); a block nested in a four-backtick fence, one indented four spaces under a list item, and a valid `~~~ui` block in one text (only the last is replaced); two identical charts; an open chart fence in progress (text cut before the fence, `{"version":1,"widgets":[],"pending":"chart"}`); an open fence with no `type` yet (`pending: "widget"`); a closed chart followed by an open form (one entry and `pending: "form"`); text ending in a bare ```` ```u ```` line in progress (cut, null); the same open chart fence with `in_progress: false` (unchanged, null); only an open fence (text `""`).
- [ ] **Step 3: Add the fixture's `responses` case** exactly as spec 5.6: `{"name": "trip form", "form": <canonical JSON>, "event_id": "$form:example.org", "thread_id": "$root:example.org", "agent_user_id": "@mindroom_atlas:example.org", "agent_name": "Atlas", "answers": {"pace": "Relaxed", "interests": ["Food", "Museums"], "budget": 150, "start": "2026-12-05"}, "content": <the 5.6 event>}`. The backend has no code for it; it lives here so the wire examples stay in one file.
- [ ] **Step 4: Run.** Expected: FAIL, `ImportError: render`.
- [ ] **Step 5: Implement `render.py`.** Scan as the eval's `scan` does (top-level fences of backticks or tildes, at most 3 spaces of indent, first info word `ui`, closing fence of the same character and at least the same length with no info), tracking the fence the text ends in. Replace valid closed blocks in place; in progress, cut at an open `ui` fence or at a last line that is an opener with no info, `u` or `ui`; sniff `pending` with `"type"\s*:\s*"(chart|form)"`. Return the input object itself when nothing changed. Fallback rules are spec 5.3. With `in_progress=False`, log one warning per closed `ui` block that failed validation, with its errors (as `interactive` warns about an unparsed block).
- [ ] **Step 6: Fill `canonical` and `id` for every valid `widgets` case** from the implementation, check the two spec values by eye, and extend `test_models.py` to assert them.
- [ ] **Step 7: Run both test files.** Expected: PASS.
- [ ] **Step 8: Commit** (message `feat(ui-widgets): fallback text, ids and the visible-text render`).

### Task 3: The `ui_widgets` switch and the prompt

**Files:**
- Modify: `src/mindroom/config/agent.py` (beside `show_reasoning`), `src/mindroom/config/models.py` (defaults, beside `show_reasoning`), `src/mindroom/config/resolution.py`
- Modify: `src/mindroom/prompts.py` (constant and the overridable-names list), `src/mindroom/orchestration/config_updates.py` (`_ENTITY_CONSTRUCTION_PROMPTS`, `_AGENT_LIVE_FIELDS`)
- Modify: `src/mindroom/agents.py` (`_build_agent_instructions`, the minimal `surface` replace), `src/mindroom/agent_loop/build.py` (`ui_prompt`), `src/mindroom/agent_cli/minimal_agent.py`, `src/mindroom/agent_cli/context.py`, `src/mindroom/agent_cli/main.py` (help text)
- Modify: `frontend/src/components/Settings/settingsSections.ts` (after `show_reasoning`), `frontend/src/test/fixtures/config-schema.json` (regenerated by `.github/scripts/generate_config_schema.py`), `docs/configuration/agents.md` (both tables)
- Test: `tests/test_config_resolution.py`, `tests/test_agents.py`, `tests/test_minimal_agent.py`

**Interfaces:**
- Produces: `config.resolution.ui_widgets(config: Config, name: str | None) -> bool`; `ResolvedAgent.ui_widgets: bool`; prompt name `UI_WIDGETS_PROMPT`; `AgentSurface.ui_prompt: str = ""`; `minimal_system_message(..., ui_widgets: bool = False)`; context document `ui`.

- [ ] **Step 1: Write the failing tests.**
  - `test_ui_widgets_is_off_unless_the_agent_or_the_defaults_turn_it_on`: unset agent with default defaults gives `False`; `agents.b.ui_widgets: true` gives `True`; `defaults.ui_widgets: true` gives `True` for an unset agent and `False` for an agent with `ui_widgets: false`; a team name, the router name, an unknown name and `None` give `False` even with the default on.
  - `test_the_widget_prompt_follows_the_interactive_prompt_when_enabled`: `instructions[i + 1] == config.get_prompt("UI_WIDGETS_PROMPT")` where `instructions[i]` is the interactive prompt; with the switch off, or `include_interactive_questions=False`, the widget prompt is not in the instructions and the instructions equal today's.
  - `test_a_minimal_agent_reads_the_widget_prompt_as_a_context_document`: `minimal_context_documents(build)["ui"]` equals the prompt and the bootstrap contains ``Charts and forms in replies: read `mindroom-agent context read ui` first.``; with the switch off the document is `""` and the bootstrap equals today's.
- [ ] **Step 2: Run.** Expected: FAIL (`ui_widgets` is not a config field).
- [ ] **Step 3: Implement.** `ui_widgets()` reads only `config.agents`; `ResolvedAgent.ui_widgets` is set in `resolve_agent`; the prompt is appended right after the interactive prompt under `include_interactive_questions and agent.ui_widgets`; the minimal surface sets `ui_prompt` under the same condition; the bootstrap line goes after the skills and memory guidance lines; the `context` help becomes `list, read NAME (read interactive before emitting an interactive question, ui before a chart or form)`.
- [ ] **Step 4: Run the three test files and `tests/test_config_schema.py`.** Expected: PASS.
- [ ] **Step 5: Commit** (message `feat(ui-widgets): per-agent ui_widgets switch and prompt`).

### Task 4: Streaming edits and live frames

**Files:**
- Modify: `src/mindroom/streaming.py` (`_visible_text`, `_StreamingDeliverySnapshot`, `_prepare_delivery_from_snapshot`, `StreamingResponse`, `_delivery_snapshot`, `live_state`, `send_streaming_response`)
- Modify: `src/mindroom/live_frames/frames.py` (`LiveDocs`, `to_json`, `live_delta`), `src/mindroom/delivery_gateway.py` (the `send_streaming_response` call)
- Modify: `tests/live_frames/fixtures/live-frames-v1.json`
- Test: `tests/live_frames/test_frames.py`, `tests/live_frames/test_live_streaming.py`, `tests/test_streaming_edits.py`

**Interfaces:**
- Consumes: `render_ui_widgets`, `UI_WIDGETS_CONTENT_KEY`, `ui_widgets()`.
- Produces:
  - `_visible_text(accumulated_text: str, *, in_progress: bool, ui_widgets: bool) -> tuple[str, interactive.InteractiveMetadata | None, dict[str, object] | None]`
  - `StreamingResponse.ui_widgets: bool = False`, `_StreamingDeliverySnapshot.ui_widgets: bool`, `send_streaming_response(..., ui_widgets: bool = False)`
  - `LiveDocs.ui: dict[str, object] | None = None`; `to_json()` has `"ui"` only when it is not `None`; `live_delta` sets `delta["ui"] = after.ui` (so `None` becomes JSON `null`) when it changed.

- [ ] **Step 1: Add three fixture cases** to `live-frames-v1.json`: a delta that sets `ui` to the 5.4 value with `pending` absent; a delta `{"ui": null}`; a snapshot whose `after` has `ui`. Existing cases stay untouched.
- [ ] **Step 2: Write the failing tests.**

```python
def test_live_frames_carry_ui_in_snapshot_and_delta() -> None:
    value = {"version": 1, "widgets": [], "pending": "chart"}
    before = LiveDocs(md="a", reasoning="", tools=(), status="streaming", activity="")
    after = replace(before, ui=value)
    assert "ui" not in before.to_json()
    assert after.to_json()["ui"] == value
    assert live_delta(before, after) == {"ui": value}
    assert live_delta(after, before) == {"ui": None}
```

  - `test_an_open_ui_fence_is_hidden_and_signalled` (live streaming): after the chunks `"Signups peaked.\n\n```ui\n{\"type\":\"chart\","` the frame's `md` is `Signups peaked.` and `ui == {"version": 1, "widgets": [], "pending": "chart"}`.
  - `test_a_closed_ui_block_becomes_its_widget_at_once`: after the block closes, `md` holds the `mindroom-ui-df4d729a5d` block, `ui["widgets"][0]["id"] == "df4d729a5d"`, no `pending`; the final event's `m.new_content` has the same value and `body` has no `{"type"`.
  - `test_without_the_switch_a_ui_block_is_delivered_as_written`: the body still holds the `ui` fence and no content has the key.
  - `test_a_stream_stopped_inside_a_ui_fence_shows_the_partial_block`: the terminal content has no `pending` and its body ends with the partial JSON.
  - `test_a_resumed_reply_keeps_the_earlier_marker_block`: `resumed_text` holding a `mindroom-ui-aaaaaaaaaa` block plus a new `ui` block gives one entry and both blocks.
  - `test_widget_content_holds_no_float`: walk the final content of a reply with `180.5` and `"step":0.5`; no value is a `float`.
- [ ] **Step 3: Run.** Expected: FAIL.
- [ ] **Step 4: Implement.** In `_visible_text`, when `ui_widgets`, render first and feed `render.text or _PROGRESS_PLACEHOLDER` to the interactive formatting. `_prepare_delivery_from_snapshot` sets `extra_content[UI_WIDGETS_CONTENT_KEY]` when the value is not `None`; `live_state` passes it as `LiveDocs.ui`. The gateway passes `ui_widgets=ui_widgets(config, self.deps.agent_name)`.
- [ ] **Step 5: Run the three files plus `tests/test_streaming.py` and `tests/test_interactive.py`.** Expected: PASS.
- [ ] **Step 6: Commit** (message `feat(ui-widgets): widgets on streamed edits and live frames`).

### Task 5: The non-streamed final and the size rule

**Files:**
- Modify: `src/mindroom/matrix/large_messages.py`, `src/mindroom/matrix/client_delivery.py` (both `fit_reasoning_to_ordinary_event` calls), `src/mindroom/delivery_gateway.py` (the durable prepare beside `fit_reasoning_to_ordinary_event`, and the final delivery where `interactive.parse_and_format_interactive(draft.response_text, ...)` runs)
- Test: `tests/test_large_messages.py`, `tests/test_response_delivery_gateway.py`

**Interfaces:**
- Produces: `fit_ui_widgets_to_ordinary_event(client: nio.AsyncClient, room_id: str, content: dict[str, Any], *, room_encrypted: bool, size_limit: int | None = None) -> dict[str, Any]`, the same signature as the reasoning fit, returning `content` itself when it has no key.

- [ ] **Step 1: Write the failing tests** (`UI` = a value of about 8,000 bytes):
  - `test_an_edit_keeps_ui_only_in_new_content`: `UI_WIDGETS_CONTENT_KEY in fitted["m.new_content"]` and not in `fitted`.
  - `test_ui_is_dropped_when_it_would_push_an_edit_over_the_budget`: a 5,000-byte body fits as an edit without the key and not with it; the result has the key nowhere, the same `body`, and `calculate_event_size(fitted) <= EDIT_MESSAGE_SIZE_LIMIT`.
  - `test_ui_stays_when_cutting_reasoning_makes_room`: a small body, the key and 40,000 characters of reasoning; after both fits the key is there and the reasoning is cut.
  - `test_a_reply_too_large_on_its_own_loses_ui_before_the_sidecar`: a 60,000-byte body; no key after the fit, and `prepare_large_message`'s preview has none either.
  - `test_ui_is_dropped_at_the_split_target`: with `size_limit=4000` a 3,000-byte body plus the key loses the key.
  - `test_a_collected_reply_carries_its_widget` (gateway): a non-streamed final for an agent with the switch sends a body with the marker block and the key; without the switch the body is the model's text.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** spec 6.3 in `fit_ui_widgets_to_ordinary_event` (size the candidate without `REASONING_CONTENT_KEY`, with the same `fits` the reasoning fit uses), call it on the content just before each reasoning fit, and render in the final delivery before the interactive formatting, adding the value to `delivery_extra_content`.
- [ ] **Step 4: Run the two files plus `tests/test_large_messages_integration.py`.** Expected: PASS.
- [ ] **Step 5: Commit** (message `feat(ui-widgets): widgets on collected replies; never cause a sidecar or split`).

### Task 6: User docs and the live test

**Files:**
- Create: `docs/widgets.md`
- Modify: `zensical.toml` (nav, after "Interactive Canvases"), `docs/streaming.md`, `docs/interactive.md`, `skills/mindroom-docs/references/` (regenerated by the pre-commit hook)

- [ ] **Step 1: Write `docs/widgets.md`**, at most 30 lines, answering: what a widget is and which two exist; how to turn it on (`ui_widgets: true` on an agent or under `defaults`); what Element and other clients show; the limits (8 series, 200 points, 12 fields, no secrets); that a form's answer is an ordinary message the agent reads; why a widget can show as a text table (other client, very long reply, a reply that resumed after an approval). Add one sentence to `docs/streaming.md` (an unfinished `ui` block is hidden like an unfinished question) and one to `docs/interactive.md` (several answers at once: see widgets).
- [ ] **Step 2: Run `pre-commit run --files docs/widgets.md docs/streaming.md docs/interactive.md zensical.toml`** (after `uv sync --all-extras`). Expected: the skill references regenerate and the hooks pass.
- [ ] **Step 3: Run the fresh live test** of spec section 10 (L1 to L10) with the Chat worktree, on a throwaway deployment, and record pass or fail per scenario in the pull request; rerun any failure without `--backend` to see whether `origin/main` has it too. This step needs the Chat plan finished.
- [ ] **Step 4: Commit** (message `docs: native ui widgets`).

## Estimated size

| Part | Source | Tests, fixtures, docs |
|---|---|---|
| Task 1 schema | 170 | 60 + fixture 130 |
| Task 2 render | 190 | 170 + fixture 120 |
| Task 3 switch and prompt | 75 | 90 + docs 6 |
| Task 4 streaming and frames | 45 | 190 + fixture 40 |
| Task 5 final and size | 55 | 140 |
| Task 6 docs | 0 | 45 |
| **Total** | **about 535** | **about 990** |

Generated files (`config-schema.json`, skill references) are not counted.
