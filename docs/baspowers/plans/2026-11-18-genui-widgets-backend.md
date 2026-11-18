# Native `ui` widgets: backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** baspowers:subagent-driven-development (eight tasks that share one wire contract with a second repo and cross the reply lifecycle; a missed write path ships raw JSON or a widget that only breaks in Chat, so each task gets its own review).

**Goal:** Stage 1: turn valid `chart` blocks in an opted-in agent's reply into a readable fallback block plus `io.mindroom.ui`, on every write of the reply and on live frames. Stage 2 (form) is outlined at the end and built later.

**Architecture:** A new `mindroom.ui_widgets` package validates blocks (pydantic port of the eval's round-2 schema) and renders the visible text and the content value in one pure, idempotent function. It runs wherever a reply body becomes Matrix content: `streaming._visible_text` (both stream constructors) and the gateway's `send_text` and `edit_text` for reply rows. The value rides in the event content and in `LiveDocs.ui`. The edit builder, a size step next to the reasoning fit, the progress throttle and the frame publisher keep the key from ever causing a sidecar, a split, a slow edit or a stopped live stream.

**Tech Stack:** Python 3.13, pydantic v2, markdown-it-py, pytest, nix-shell + uv.

**Spec:** `docs/baspowers/specs/2026-11-18-genui-widgets-design.md` (sections 4, 5, 6, 8, 9 and 11 are the contract; appendix A says why; read it first).

## Global Constraints

- Stage 1 knows `chart` only. A `form` block, like any unknown `type`, stays a code block. Nothing in Stage 1 may make Stage 2 a format change.
- Content key `io.mindroom.ui`, constant `UI_WIDGETS_CONTENT_KEY` in `src/mindroom/constants.py` next to `UI_ACTION_CONTENT_KEY`.
- Value shape exactly `{"version": 1, "widgets": [{"id", "json"}]}`; no `pending`; no key when there is no entry.
- Marker: fenced block with info string `mindroom-ui-<id>`, a blank line on each side. `id` = first 10 hex of SHA-256 of the canonical JSON (sorted keys, `(",", ":")` separators, `ensure_ascii=False`, whole numbers below 2^53 without a fraction, absent optionals left out); an id a literal marker block in the text already uses is skipped with the `#1`, `#2` suffix rule of spec 5.1.
- Only a fence that starts in column 0 with first info word `ui` can be a widget.
- Limits, each private to the module that enforces it (privata rejects a public name nothing else imports): `_MAX_BLOCK_BYTES = 16_384`, `_MAX_SERIES = 8`, `_MAX_POINTS = 200` in `models.py`; `_MAX_WIDGETS = 8`, `_MAX_UI_BYTES = 16_384` (UTF-8 bytes over all `json` strings), `_FALLBACK_MAX_ROWS = 40` in `render.py`.
- The event content must hold no float anywhere: widgets travel as JSON text.
- A reply text with no column-0 `ui` fence, and any reply of an agent without `ui_widgets`, is returned and delivered byte-identical to today.
- No change to what the model sees unless `ui_widgets` is on (prompt cache stays byte-identical for every other agent).
- `ui_widgets` config field: `agents.<name>.ui_widgets: bool | None`, `defaults.ui_widgets: bool = False`.
- `UI_WIDGETS_PROMPT` in Stage 1 is the chart-only text of spec 6.5, byte for byte.
- Logs never contain a block's text, a title or a label; errors and sizes only.
- Source to port: the eval's `schema.py` (v3, round 2) and `test_schema.py`, local files in the DevAgent workspace under `genui-eval/`, not in this repo; the spec's sections 4 and 8 restate every rule.
- Fixtures shared with mindroom-chat, byte-identical (`sha256sum` after any change): `tests/ui_widgets/fixtures/ui-widgets-v1.json` and `tests/live_frames/fixtures/live-frames-v1.json`. Tasks 1 to 3 complete both; Chat starts after Task 3.
- Tests: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest <files> -x -n 0 --no-cov'`; run each new test file once with the repo's default addopts.
- Docs: one sentence per line; user docs get only sentences that answer a user question. Never `git add .`.

## Review Focus

- Stop, restart and approval pause render from the reply's records, which hold raw text: every such write must show the chart's fallback and carry its entry, never the raw `ui` JSON (Task 7 proves it from persisted records).
- A Stop while a `ui` fence is open: no partial JSON, and the note is outside any code block (Tasks 5 and 7).
- A reply row prepared before its create was acknowledged becomes an edit without a second fit: no outer key, and the key is dropped if the edit would be over budget (Task 6).
- An unencrypted room: the final content serializes with no float even for `180.5` (Task 5 walks the content).
- A title holding backticks, newlines or a literal `mindroom-ui-` id must not close the fence, add lines or make an old marker block draw (Task 2).

---

### Task 1: Chart schema and the fixture's `widgets` section

**Files:**
- Create: `src/mindroom/ui_widgets/__init__.py` (docstring only), `src/mindroom/ui_widgets/models.py`
- Create: `tests/ui_widgets/__init__.py`, `tests/ui_widgets/test_models.py`, `tests/ui_widgets/fixtures/ui-widgets-v1.json`

**Interfaces:**
- Produces: models `Series`, `Chart` (strict, `extra="forbid"`); `parse_widget(json_text: str) -> tuple[Chart | None, list[str]]`, which never raises and never puts input text in an error.

- [ ] **Step 1: Write the fixture's `widgets` section.** File shape: `{"version": 1, "widgets": [...], "wire_invalid": [...], "renders": [], "responses": []}`; each widget case `{"name", "stage": 1, "json", "valid"}` and, when valid, `"canonical"` and `"id"` (filled in Task 2). Base: `CHART = {"type":"chart","kind":"bar","title":"Orders","labels":["Q1","Q2"],"series":[{"name":"Orders","data":[120,180.5]}]}`.

| Valid | Invalid |
|---|---|
| `CHART`; `CHART` + `x`,`y`; `CHART` + `"x": null`; 200 labels and points; `labels: [2021, 2022.0]` (canonical `["2021","2022"]`); a title of 80 emoji; data `[1e-320, 1.5e300, -1e308]`; data `[1e21]` (canonical keeps the exponent); the spec's 5.4 chart | `CHART` + `"color":"red"`; one label for two values; `kind: scatter`; data `["1","2"]`; data `[true,2]`; pie with two series; pie with `[-1,2]`; pie with `[0,0]`; two series named alike; 201 points; 9 series; `NaN` in data; `1e400` in data; an integer of 400 digits in data; `labels: [true,null]`; a title of 81 emoji; `[1]`; `{oops`; `{"type":"map"}`; the spec's 5.6 form (Stage 1: unknown type); a chart padded with spaces to 16,385 bytes |

  `wire_invalid` (client only): the 5.4 canonical text with `"labels":[1,2,3]`; the same with `"x":null`.
- [ ] **Step 2: Write the failing test** in `tests/ui_widgets/test_models.py`:

```python
@pytest.mark.parametrize("case", _STAGE_1, ids=[c["name"] for c in _STAGE_1])
def test_parse_widget_agrees_with_the_shared_fixture(case: dict[str, object]) -> None:
    widget, errors = parse_widget(case["json"])
    assert (widget is not None) is case["valid"], errors
    assert bool(errors) is not case["valid"]

def test_parse_widget_never_raises_and_never_echoes_input() -> None:
    for text in ("", "null", "[" * 100_000, '{"type":"chart","series":7}', "\ud800", '{"type":"chart","title":"SECRET-TITLE","kind":"x"}'):
        widget, errors = parse_widget(text)
        assert widget is None
        assert "SECRET-TITLE" not in " ".join(errors)
```

- [ ] **Step 3: Run it.** Expected: FAIL, `ModuleNotFoundError: mindroom.ui_widgets`.
- [ ] **Step 4: Implement `models.py`** by porting the eval's chart models, the pre-pass that stringifies numeric labels, the constant rejection, and `parse` as `parse_widget`. The eval's int-to-float pre-pass is not ported: strict pydantic already takes an int for a float and reports an integer too large for a double as `float_type`. Measure the byte limit so that a lone surrogate cannot raise. Build error strings from locations and error types only: the cross-field rules raise `PydanticCustomError` whose type names the rule, and the location of an unknown key leaves the key out. Leave the form models, `scan`, `normalize` and `validate` out.
- [ ] **Step 5: Run it.** Expected: PASS. Also run `just check-module-privacy`.
- [ ] **Step 6: Commit** (message `feat(ui-widgets): strict chart schema`).

### Task 2: Canonical JSON, ids, fallback text and `render_ui_widgets`

**Files:**
- Create: `src/mindroom/ui_widgets/render.py`, `tests/ui_widgets/test_render.py`
- Modify: `src/mindroom/constants.py`, `tests/ui_widgets/fixtures/ui-widgets-v1.json`

**Interfaces:**
- Consumes: `parse_widget`, `Chart`.
- Produces:
  - `UI_WIDGETS_CONTENT_KEY = "io.mindroom.ui"`
  - `_canonical_widget_json(widget: Chart) -> str`, `_widget_id(canonical: str, taken: Collection[str] = ()) -> str`
  - `_fallback_block(widget: Chart, widget_id: str) -> str` (the whole fenced block, no trailing newline)
  - These three are private (only `render.py` and its tests use them; privata rejects a public name nothing else imports), so the test snippets below call them with the underscore.
  - `@dataclass(frozen=True, slots=True) class UiRender: text: str; content: dict[str, object] | None`
  - `render_ui_widgets(text: str, *, in_progress: bool) -> UiRender`
  - `without_open_ui_fence(text: str) -> str` (the text cut where an open column-0 `ui` fence begins, else the same object)

- [ ] **Step 1: Write the failing tests.**

```python
CHART = '{"type":"chart","kind":"line","title":"Signups","labels":["Jan","Feb","Mar"],"series":[{"name":"Signups","data":[120,180.5,150]}],"y":"users"}'
BLOCK = "```mindroom-ui-df4d729a5d\nSignups (line chart, users)\n     Signups\nJan      120\nFeb    180.5\nMar      150\n```"

def test_canonical_json_and_id_match_the_spec() -> None:
    widget, _ = parse_widget(CHART)
    canonical = canonical_widget_json(widget)
    assert canonical == '{"kind":"line","labels":["Jan","Feb","Mar"],"series":[{"data":[120,180.5,150],"name":"Signups"}],"title":"Signups","type":"chart","y":"users"}'
    assert widget_id(canonical) == "df4d729a5d"
    assert widget_id(canonical, taken={"df4d729a5d"}) == hashlib.sha256((canonical + "#1").encode()).hexdigest()[:10]

def test_chart_fallback_is_the_spec_table() -> None:
    assert fallback_block(parse_widget(CHART)[0], "df4d729a5d") == BLOCK

def test_rendering_is_idempotent_and_keeps_the_widget_only_for_raw_blocks() -> None:
    raw = f"Before.\n\n```ui\n{CHART}\n```\n\nAfter."
    once = render_ui_widgets(raw, in_progress=False)
    assert once.text == f"Before.\n\n{BLOCK}\n\nAfter."
    twice = render_ui_widgets(once.text, in_progress=False)
    assert twice.text is once.text and twice.content is None

def test_a_copied_marker_block_stays_inert_beside_an_identical_new_widget() -> None:
    render = render_ui_widgets(f"{BLOCK}\n\n```ui\n{CHART}\n```", in_progress=False)
    (entry,) = render.content["widgets"]
    assert entry["id"] != "df4d729a5d"
    assert render.text.startswith(BLOCK) and f"mindroom-ui-{entry['id']}" in render.text

def test_text_without_a_ui_fence_is_returned_as_is() -> None:
    text = "Hello\r\n\n```python\nx = '```ui'\n```\n"
    render = render_ui_widgets(text, in_progress=False)
    assert render.text is text and render.content is None
```

  Plus: `test_a_title_of_backticks_gets_a_longer_fence` (one `<pre>` from `markdown_to_html`); `test_strings_are_collapsed_to_one_line` (label `"a\n```\nb"`); `test_a_45_point_chart_shows_40_rows` (line 24 of the block is `... 5 more rows ...`); `test_large_whole_numbers_keep_their_exponent` (`1e300` prints `1e+300` in the canonical JSON and the table); `test_entries_stop_at_8_and_at_16384_bytes` (9 small charts: 9 blocks, 8 entries; two 9 KB charts: two blocks, one entry); `test_identical_blocks_share_one_entry`; `test_only_column_0_fences_count` (a fence indented two spaces under a list item, one in a blockquote and one inside a four-backtick fence are untouched; a `~~~ui` block is replaced); `test_a_block_glued_to_a_paragraph_gets_blank_lines`; `test_an_open_fence_is_cut_only_in_progress` (in progress: text ends before the fence and no content; final: unchanged; a last line of only ```` ``` ```` or ```` ```u ```` is not cut); `test_without_open_ui_fence`; `test_block_results_are_memoized` (two renders of growing text parse the closed block once, counted with a patched `parse_widget`).
- [ ] **Step 2: Add the fixture's `renders` cases** (`{"name", "stage", "markdown", "in_progress", "text", "ui", "html"}`, `ui` null when there is no value) and `test_render_agrees_with_the_shared_fixture`, asserting `text`, `content` and `markdown_to_html(text) == html`. Cases: the 5.4 reply (hand-pinned from the spec); an unknown `type`; nested, list-indented, quoted and tilde fences in one text; two identical charts; an open chart fence in progress; a closed chart followed by an open fence in progress; the open fence with `in_progress: false`; only an open fence (text `""`); a literal marker block beside a new identical chart; and one `"stage": 2` case hand-pinned from spec 5.3 and 5.6: `text` with the form's marker block and `ui` with the form entry (Python skips it in Stage 1; Chat uses it for "an entry it cannot draw").
- [ ] **Step 3: Add the fixture's `responses` case** exactly as spec 5.6 with `"stage": 2` (no backend code reads it).
- [ ] **Step 4: Run.** Expected: FAIL, `ImportError: render`.
- [ ] **Step 5: Implement `render.py`.** One pass over the lines tracks the open fence (any column-0 or indented fence opens a code block; only column-0 `ui` fences are candidates). Collect the ids of literal `mindroom-ui-` fences first. Replace valid closed blocks in place with blank-line separation; in progress, cut at an open `ui` fence whose opening line is complete. Memoize `(canonical, fallback lines)` by block text with `functools.lru_cache(maxsize=64)`. With `in_progress=False`, log one warning per closed `ui` block that failed validation, with its error list only. Fallback rules are spec 5.3.
- [ ] **Step 6: Fill `canonical` and `id` for every valid `widgets` case** from the implementation, check the spec's values by eye, and assert them in `test_render.py` (`test_canonical_json_and_id_agree_with_the_shared_fixture`), next to the functions that produce them.
- [ ] **Step 7: Run both test files.** Expected: PASS.
- [ ] **Step 8: Commit** (message `feat(ui-widgets): fallback text, ids and the idempotent render`).

### Task 3: The live-frame field

**Files:**
- Modify: `src/mindroom/live_frames/frames.py` (`LiveDocs`, `to_json`, `live_delta`), `src/mindroom/live_frames/publisher.py` (`_send_next`, `_fitted_snapshot`)
- Modify: `tests/live_frames/fixtures/live-frames-v1.json`
- Test: `tests/live_frames/test_frames.py`, `tests/live_frames/test_live_streaming.py`

**Interfaces:**
- Produces: `LiveDocs.ui: dict[str, object] | None = None`; `to_json()` has `"ui"` only when it is not `None`; `live_delta` sets `delta["ui"] = after.ui` (so `None` becomes JSON `null`) when it changed.

- [ ] **Step 1: Add three fixture cases**: a delta that changes `md` and sets `ui` to the 5.4 value; a delta `{"ui": null}`; a snapshot whose `after` has `ui`. Existing cases stay untouched; make `test_frames_match_the_shared_fixture` build `LiveDocs` with `ui` when the case has it.
- [ ] **Step 2: Write the failing tests.**

```python
def test_live_frames_carry_ui_in_snapshot_and_delta() -> None:
    value = {"version": 1, "widgets": [{"id": "df4d729a5d", "json": "{}"}]}
    before = LiveDocs(md="a", reasoning="", tools=(), status="streaming", activity="")
    after = replace(before, ui=value)
    assert "ui" not in before.to_json()
    assert after.to_json()["ui"] == value
    assert live_delta(before, after) == {"ui": value}
    assert live_delta(after, before) == {"ui": None}
```

  - `test_a_frame_too_big_with_ui_goes_out_without_it_and_restores_it`: with the frame limit patched low, a snapshot that fits only without `ui` is sent without it and frames keep running; `last_sent.ui is None`; once the limit allows, the next frame is a delta that sets `ui`.
  - `test_ui_is_shed_before_tool_previews`: the same misfit with tools present leaves `bare` unchanged.
- [ ] **Step 3: Run.** Expected: FAIL.
- [ ] **Step 4: Implement.** In the publisher, when the frame to send does not fit and the documents have `ui`, retry with `replace(docs, ui=None)` before the tool-preview fitting.
- [ ] **Step 5: Run `tests/live_frames`.** Expected: PASS.
- [ ] **Step 6: Commit** (message `feat(ui-widgets): optional ui field on live frames`). Both fixtures are now complete for Chat.

### Task 4: The `ui_widgets` switch and the prompt

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
  - `test_the_widget_prompt_follows_the_interactive_prompt_when_enabled`: `instructions[i + 1] == config.get_prompt("UI_WIDGETS_PROMPT")` where `instructions[i]` is the interactive prompt; with the switch off, or `include_interactive_questions=False`, the instructions equal today's.
  - `test_the_stage_1_prompt_is_the_spec_text`: the prompt starts with `To show a native widget`, contains the Signups example and `since some clients show only text`, and contains neither `form` nor `interactive`.
  - `test_a_minimal_agent_reads_the_widget_prompt_as_a_context_document`: `minimal_context_documents(build)["ui"]` equals the prompt and the bootstrap contains ``Charts in replies: read `mindroom-agent context read ui` first.``; with the switch off the document is `""` and the bootstrap equals today's.
- [ ] **Step 2: Run.** Expected: FAIL (`ui_widgets` is not a config field).
- [ ] **Step 3: Implement.** `ui_widgets()` reads only `config.agents`; `ResolvedAgent.ui_widgets` is set in `resolve_agent`; the prompt is appended right after the interactive prompt under `include_interactive_questions and agent.ui_widgets`; the minimal surface sets `ui_prompt` under the same condition; the bootstrap line goes after the skills and memory guidance lines; the `context` help becomes `list, read NAME (read interactive before emitting an interactive question, ui before a chart)`.
- [ ] **Step 4: Run the three test files and `tests/test_config_schema.py`.** Expected: PASS.
- [ ] **Step 5: Commit** (message `feat(ui-widgets): per-agent ui_widgets switch and prompt`).

### Task 5: Streamed writes

**Files:**
- Modify: `src/mindroom/streaming.py` (`_visible_text`, `_StreamingDeliverySnapshot`, `_prepare_delivery_from_snapshot`, `StreamingResponse`, `_delivery_snapshot`, `live_state`, `send_streaming_response`, `stream_progress_edits`)
- Modify: `src/mindroom/delivery_gateway.py` (the `send_streaming_response` call, the `stream_progress_edits` call, and `shown()` in the reply stream hooks)
- Test: `tests/live_frames/test_live_streaming.py`, `tests/test_streaming_edits.py`, `tests/test_response_delivery_gateway.py`

**Interfaces:**
- Consumes: `render_ui_widgets`, `without_open_ui_fence`, `UI_WIDGETS_CONTENT_KEY`, `ui_widgets()`, `LiveDocs.ui`.
- Produces:
  - `_visible_text(accumulated_text: str, *, in_progress: bool, ui_widgets: bool) -> tuple[str, interactive.InteractiveMetadata | None, dict[str, object] | None]`
  - `StreamingResponse.ui_widgets: bool = False`, `_StreamingDeliverySnapshot.ui_widgets: bool`, `send_streaming_response(..., ui_widgets: bool = False)`, `stream_progress_edits(..., ui_widgets: bool = False)`

- [ ] **Step 1: Write the failing tests.**
  - `test_an_open_ui_fence_is_hidden_without_a_signal` (live streaming): after the chunks `"Signups peaked.\n\n```ui\n{\"type\":\"chart\","` the frame's `md` is `Signups peaked.` and the frame has no `ui`.
  - `test_a_closed_ui_block_becomes_its_widget_at_once`: after the block closes and before any durable edit, a frame's `md` holds the `mindroom-ui-df4d729a5d` block and `ui["widgets"][0]["id"] == "df4d729a5d"`; the final event's `m.new_content` has the same value and its `body` has no `{"type"`.
  - `test_without_the_switch_a_ui_block_is_delivered_as_written`: the body still holds the `ui` fence and no content has the key.
  - `test_a_reply_that_ends_inside_a_ui_fence_shows_the_partial_block`: a completed stream's final body ends with the partial JSON in a code block.
  - `test_the_recorded_progress_has_no_open_ui_fence`: the presentation the stream hooks record for the open-fence chunks ends with `Signups peaked.`
  - `test_progress_edits_of_a_resumed_reply_render_widgets`: text published through `stream_progress_edits(..., ui_widgets=True)` holding a `ui` block is edited with the marker block and the key.
  - `test_widget_content_holds_no_float`: walk the final content of a reply with `180.5`; no value is a `float`.
  - `test_the_ui_render_runs_after_the_interactive_formatting`: a reply with an `interactive` block and a chart titled `` ```interactive `` yields the question, the chart entry and no stray text removed.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** In `_visible_text`, after the interactive formatting and the in-progress cuts, when `ui_widgets`: `render = render_ui_widgets(display_text, in_progress=in_progress)`, text `render.text or _PROGRESS_PLACEHOLDER`. `_prepare_delivery_from_snapshot` sets `extra_content[UI_WIDGETS_CONTENT_KEY]` when the value is not `None`; `live_state` passes it as `LiveDocs.ui`. The gateway passes `ui_widgets=ui_widgets(config, self.deps.agent_name)` to both constructors, and `shown()` builds the presentation from `without_open_ui_fence(progress.text)` when the switch is on.
- [ ] **Step 4: Run the three files plus `tests/test_streaming.py` and `tests/test_interactive.py`.** Expected: PASS.
- [ ] **Step 5: Commit** (message `feat(ui-widgets): widgets on streamed edits and live frames`).

### Task 6: Every other reply write, and size

**Files:**
- Modify: `src/mindroom/delivery_gateway.py` (`send_text`, `edit_text`, the late-bound row in `prepare()`), `src/mindroom/matrix/message_builder.py` (`build_matrix_edit_content`), `src/mindroom/matrix/large_messages.py`, `src/mindroom/matrix/client_delivery.py` (both `fit_reasoning_to_ordinary_event` calls)
- Test: `tests/test_message_builder.py`, `tests/test_large_messages.py`, `tests/test_response_delivery_gateway.py`

**Interfaces:**
- Produces: `fit_ui_widgets_to_ordinary_event(client: nio.AsyncClient, room_id: str, content: dict[str, Any], *, room_encrypted: bool, size_limit: int | None = None) -> dict[str, Any]`, the reasoning fit's signature, returning `content` itself when it has no key.

- [ ] **Step 1: Write the failing tests.** Each size test builds its content, then asserts with `calculate_event_size` that it is over the limit with the key and under it without, before calling the code under test.
  - `test_an_edit_envelope_has_ui_only_in_new_content` (`build_matrix_edit_content`).
  - `test_ui_is_dropped_when_it_alone_pushes_an_edit_over_the_budget`: the result has the key nowhere, the same `body`, and is within `EDIT_MESSAGE_SIZE_LIMIT`.
  - `test_ui_stays_when_cutting_reasoning_makes_room`.
  - `test_a_reply_too_large_on_its_own_loses_ui_before_the_sidecar`: no key after the fit, none on `prepare_large_message`'s preview.
  - `test_ui_is_dropped_at_the_split_target` (`size_limit` given).
  - `test_ui_made_of_non_ascii_text_is_sized_as_sent` (a title of 80 emoji near the limit, encrypted and plain).
  - `test_a_droppable_ui_key_does_not_throttle_a_progress_edit`: `should_send_oversized_nonterminal_streaming_edit` is true for an edit that is ordinary without the key.
  - `test_a_collected_reply_carries_its_widget` (gateway): a non-streamed final for an agent with the switch sends the marker block and the key; without the switch, or for a message with no `reply_write`, the text is untouched.
  - `test_a_row_prepared_before_its_create_is_fitted_as_the_edit_it_becomes`: a final prepared with no event id whose payload fits 55,000 bytes but whose edit would not fit 27,000 is stored without the key; one that fits keeps it, and the envelope `_reply_row_wire_content` builds has it only in `m.new_content`; a retry sends identical bytes.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** spec 6.3. In `send_text` and `edit_text`, after `_reply_body` and before `format_message_with_mentions`, for a request with a `reply_write` and the switch on: render with `in_progress=False`, replace the text and add the value to the extra content. The fit sizes its candidate without `REASONING_CONTENT_KEY`, with the `fits` the reasoning fit uses, and runs just before each reasoning fit. For the late-bound row, run the fit on `build_edit_event_content(event_id="$" + "0" * 43, ...)` of the payload and drop the key from the payload when it does not survive. The throttle removes the key from its candidate when the edit is ordinary without it.
- [ ] **Step 4: Run the three files plus `tests/test_large_messages_integration.py`.** Expected: PASS.
- [ ] **Step 5: Commit** (message `feat(ui-widgets): widgets on every reply write; never cause a sidecar or split`).

### Task 7: Lifecycle proofs from persisted records

**Files:**
- Test: `tests/test_user_stop_convergence.py`, `tests/test_approval_turn_continuation.py`, `tests/test_edit_after_restart.py` (one new test each, with the files' existing harnesses)
- Modify: whatever Tasks 5 and 6 touched, only if a test here fails

The tests assert the Matrix content that was sent (body and `io.mindroom.ui`), read from the fake client or the outbox, never a hand-built `resumed_text`.

- [ ] **Step 1: Write the tests.**
  - Stop, frames on and off: a stream that closed the 5.4 chart and is inside a second, open `ui` fence is stopped. The terminal content's body holds the first chart's marker block, no `{"type"`, and the cancellation note as its last paragraph outside any fence; `m.new_content` carries the first chart's entry.
  - Approval: chart, then a tool call that pauses; the pause write, the resumed progress, a second pause and the final each carry the chart's entry and its marker block; the id is the same in all four.
  - Restart: a reply interrupted after the chart closed gets its restart note on the next start; that write carries the entry, and the reply that continues below it keeps the block.
  - Regeneration: the same turn regenerated into prose sends a final edit without the key.
- [ ] **Step 2: Run.** Expected: PASS if Tasks 5 and 6 covered every path; otherwise fix the path the failing test names and add nothing else.
- [ ] **Step 3: Commit** (message `test(ui-widgets): widgets across stop, approval and restart`).

### Task 8: User docs and the live gate

**Files:**
- Create: `docs/widgets.md`
- Modify: `zensical.toml` (nav, after "Interactive Canvases"), `docs/streaming.md`, `docs/interactive.md`, `skills/mindroom-docs/references/` (regenerated by the pre-commit hook)

- [ ] **Step 1: Write `docs/widgets.md`**, at most 30 lines, answering: what a chart widget is; how to turn it on (`ui_widgets: true` on an agent or under `defaults`); what Element and other clients show; the limits (8 series, 200 points); why a chart can show as a text table (another client, a very long reply); that Chat draws widgets for `mindroom_` accounts on the viewer's homeserver, so the deployment must reserve that namespace. Add one sentence to `docs/streaming.md` (an unfinished `ui` block is hidden like an unfinished question) and one to `docs/interactive.md`.
- [ ] **Step 2: Run `pre-commit run --files docs/widgets.md docs/streaming.md docs/interactive.md zensical.toml`** (after `uv sync --all-extras`). Expected: the skill references regenerate and the hooks pass.
- [ ] **Step 3: Run the fresh live test** of spec section 11 (L1 to L12) with the Chat worktree, on a throwaway deployment, and record pass or fail per scenario and both commit ids in the pull request; rerun any failure without `--backend`. It is a release gate: the pull request is not ready with a failing or skipped scenario. This step needs the Chat plan's Stage 1 finished.
- [ ] **Step 4: Commit** (message `docs: native chart widgets`).

## Stage 1 estimated size

| Part | Source | Tests, fixtures, docs |
|---|---|---|
| Task 1 chart schema | 80 | 50 + fixture 80 |
| Task 2 render | 190 | 200 + fixture 110 |
| Task 3 live frames | 30 | 80 + fixture 40 |
| Task 4 switch and prompt | 75 | 100 + docs 6 |
| Task 5 streamed writes | 50 | 170 |
| Task 6 other writes and size | 85 | 210 |
| Task 7 lifecycle proofs | 10 | 170 |
| Task 8 docs | 0 | 45 |
| **Total** | **about 520** | **about 1,260** |

Generated files (`config-schema.json`, skill references) are not counted.

## Stage 2 (form): later, its own pull request

Not built now; written here so Stage 1 leaves room for it. No wire change.

- **Schema:** add the form models to `models.py` from the eval's round-2 `schema.py`, with `asks_for_secret` (the negated filter) as it is; `parse_widget` returns `Chart | Form`. Fixture: flip the 5.6 form case to valid, add the form cases of `test_schema.py`, paired negated and unnegated labels, `null` optionals, dates in year 0001 and 0000, slider and date boundaries.
- **Fallback:** the form text of spec 5.3 in `fallback_block`; the Stage 2 `renders` case becomes a Python test.
- **Prompt:** `UI_WIDGETS_PROMPT` becomes `genui-eval/prompt_fenced.md` (v4) verbatim; the bootstrap line and help text say "charts and forms"; `docs/widgets.md` gains the form paragraph.
- **Nothing else:** every write path, the size rules and the frames already carry any entry.
- **Estimate:** about 170 source, about 260 tests and fixtures.
