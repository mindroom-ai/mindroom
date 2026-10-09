# Thread Move Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** baspowers:executing-plans — four tightly coupled tasks in one module family; the user prefers inline implementation with a fresh reviewer at the end.

**Goal:** Add a `thread_move` tool whose `move_thread(room_id, thread_id=None)` copies the current thread into another room and points the original to the copy.

**Architecture:** A pure copy planner turns the thread history into ordered posts (poster entity plus allowlisted content); the toolkit checks preconditions, posts the plan through each poster's Matrix client, copies tags, and posts a notice and the `resolved` tag in the source thread.
Other entities' clients come from one new orchestrator wiring method.

**Tech Stack:** Python 3.13, Agno `Toolkit`, mindroom-nio, pytest.

**Spec:** `docs/dev/2026-10-09-thread-move-design.md`

## Global Constraints

- Source is always the current room; the target must differ from it.
- Copied content keys: `msgtype`, `body`, `format`, `formatted_body`, `url`, `file`, `info`, `filename`, `io.mindroom.tool_trace` (`TOOL_TRACE_CONTENT_KEY`).
- Every copy gets `com.mindroom.skip_mentions: True` (`SKIP_MENTIONS_KEY`) and `"m.mentions": {}`.
- Router relays set `com.mindroom.original_sender` (`ORIGINAL_SENDER_KEY`) and never set `com.mindroom.source_kind`.
- Skip `m.notice` and stream statuses `pending`, `streaming`, `approval_pending` (`STREAM_STATUS_PENDING`, `STREAM_STATUS_STREAMING`, `STREAM_STATUS_APPROVAL_PENDING`).
- Encrypted source to unencrypted target is refused.
- No changes to session storage, no durable move journal, no new storage.
- No imports of `mindroom.bot` from the tool; imports at module top.
- Markdown docs: one sentence per line.

## Review Focus

1. A copied agent message that names another agent (`@code please check`) must not wake that agent: covered by the planner's trigger-guard assertions in Task 2 and the ingress test in Task 3.
2. A human's image or file copied by the router must keep its media fields and show the author's name as the caption: Task 2 `test_plan_prefixes_relayed_media_caption`.
3. The acting agent's own in-progress reply in the source thread must not be copied: Task 2 `test_plan_skips_notices_and_in_progress_replies`.
4. An agent from the thread that is not in the target room must not block the move; the router relays its messages with attribution: Task 2 `test_plan_relays_human_and_absent_entity_messages_through_router` and Task 3 `test_move_thread_copies_messages_in_order`.
5. A send failure partway through must leave the source thread untouched and report the partial copy: Task 3 `test_move_thread_reports_partial_copy_on_send_failure`.

---

### Task 1: Orchestrator client lookup

**Files:**
- Modify: `src/mindroom/runtime_protocols.py` (`OrchestratorRuntime`)
- Modify: `src/mindroom/orchestrator.py` (`MultiAgentOrchestrator`)
- Test: `tests/test_orchestrator_runtime.py`

**Interfaces:**
- Produces: `OrchestratorRuntime.running_entity_client(self, entity_name: str) -> nio.AsyncClient | None`.

- [ ] **Step 1: Write the failing test** `test_running_entity_client_returns_only_running_bot_clients`: an orchestrator with `agent_bots` holding a running bot with a client, a stopped bot with a client, and a running bot whose client is `None`; assert the first returns its client, the other two and an unknown name return `None`.
- [ ] **Step 2: Run** `uv run pytest tests/test_orchestrator_runtime.py -k running_entity_client -x -n 0 --no-cov` and expect `AttributeError`.
- [ ] **Step 3: Implement** the protocol method and `MultiAgentOrchestrator.running_entity_client`, returning `bot.client` only when the bot exists and `bot.running`.
- [ ] **Step 4: Run** the test and expect PASS.
- [ ] **Step 5: Commit** `feat: let runtime collaborators look up a running entity's Matrix client`.

### Task 2: Copy planner

**Files:**
- Create: `src/mindroom/custom_tools/thread_move.py` (planner section)
- Test: `tests/test_thread_move_tool.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True, slots=True)
class PlannedCopy:
    poster: str  # entity name that sends this copy; ROUTER_AGENT_NAME for relays
    content: dict[str, Any]  # without m.relates_to
    source_event_id: str

def plan_thread_copy(
    messages: Sequence[ResolvedVisibleMessage],
    *,
    entity_name_for_sender: Callable[[str], str | None],
    target_posters: frozenset[str],
    display_names: Mapping[str, str],
) -> list[PlannedCopy]: ...
```

`entity_name_for_sender` maps a Matrix user ID to a managed entity name (router included) or `None`; `target_posters` holds entity names that are running and joined to the target room.
A sender whose entity is in `target_posters` posts its own copy; every other sender is relayed by `ROUTER_AGENT_NAME`.
Relay name is `display_names.get(sender, sender)`.
Relayed text: `body = f"{name}: {body}"`; when `formatted_body` is present, prefix it with `f"<strong>{html.escape(name)}</strong>: "`.
Relayed media (`m.image`, `m.file`, `m.audio`, `m.video`): `filename = content.get("filename") or body`; caption is the original body when the original had a `filename` different from its body, otherwise the filename; `body = f"{name}: {caption}"`.

- [ ] **Step 1: Write the failing tests** using `ResolvedVisibleMessage.synthetic(...)` inputs:
  - `test_plan_posts_entity_messages_as_their_own_account`: an agent message with `entity_name_for_sender` returning `"code"` and `target_posters={"code", "router"}` yields `poster == "code"`, body unchanged, no `ORIGINAL_SENDER_KEY`.
  - `test_plan_relays_human_and_absent_entity_messages_through_router`: a human message and an agent message whose entity is not in `target_posters` both yield `poster == "router"` with `content[ORIGINAL_SENDER_KEY] == sender`.
  - `test_plan_skips_notices_and_in_progress_replies`: `m.notice` and each of the three in-progress stream statuses are dropped; a `completed` reply is kept.
  - `test_plan_copies_only_allowlisted_keys`: input content with `m.relates_to`, `io.mindroom.ai_run`, `io.mindroom.stream_status`, `com.mindroom.attachment_ids`, `io.mindroom.interactive` yields content whose keys are a subset of the allowlist plus `SKIP_MENTIONS_KEY`, `"m.mentions"`, and (relays only) `ORIGINAL_SENDER_KEY`; `content[SKIP_MENTIONS_KEY] is True` and `content["m.mentions"] == {}`.
  - `test_plan_prefixes_relayed_text`: `body == "Dominic: hi"` and `formatted_body == "<strong>Dominic</strong>: <p>hi</p>"` for `display_names={"@dominic:example.org": "Dominic"}`.
  - `test_plan_prefixes_relayed_media_caption`: an `m.image` with `body="photo.png"`, `url="mxc://example.org/abc"` yields `filename == "photo.png"`, `body == "Dominic: photo.png"`, `url` kept; one with `filename="photo.png"`, `body="look"` yields `body == "Dominic: look"`.
  - `test_plan_name_falls_back_to_user_id`: no display name gives `body == "@dominic:example.org: hi"`.
- [ ] **Step 2: Run** `uv run pytest tests/test_thread_move_tool.py -x -n 0 --no-cov` and expect import failure.
- [ ] **Step 3: Implement** `PlannedCopy` and `plan_thread_copy` in `src/mindroom/custom_tools/thread_move.py`.
- [ ] **Step 4: Run** the tests and expect PASS.
- [ ] **Step 5: Commit** `feat: plan the copies that move a thread to another room`.

### Task 3: `thread_move` toolkit and registration

**Files:**
- Modify: `src/mindroom/custom_tools/thread_move.py` (add `ThreadMoveTools`)
- Create: `src/mindroom/tools/thread_move.py`
- Modify: `src/mindroom/tools/__init__.py`, `pyproject.toml` (extra `thread_move = []`), `src/mindroom/tools_metadata.json` (regenerate with the command in `tests/test_tools_metadata.py`), `tach.toml`
- Test: `tests/test_thread_move_tool.py`, plus one ingress test beside the existing `com.mindroom.skip_mentions` ingress tests

**Interfaces:**
- Consumes: `running_entity_client` (Task 1), `plan_thread_copy` / `PlannedCopy` (Task 2), `resolve_requested_room_id`, `room_access_allowed`, `resolve_canonical_tool_thread_target` (`custom_tools/attachment_helpers.py`), `resolve_thread_root_event_id_for_client`, `complete_thread_history`, `resolve_room_encryption_outcome`, `send_message_result`, `cached_room`, `get_room_members`, `room_member_display_names`, `entity_identity_registry`, `build_thread_relation`, `get_thread_tags`, `set_thread_tag`, `RESOLVED_THREAD_TAG`, `ThreadTagsError`, `custom_tool_payload`.
- Produces: `ThreadMoveTools(Toolkit)` named `thread_move` with `async def move_thread(self, room_id: str, thread_id: str | None = None) -> str`; registration `thread_move` with `file_access=ToolFileAccess.NONE`, `display_name="Thread Move"`, `description="Move a conversation thread into another room"`, `category=ToolCategory.COMMUNICATION`, `icon="Share2"`, `icon_color="text-sky-500"`, `function_names=("move_thread",)`, `requires_room_context=True`, other fields as in `tools/thread_resolution.py`.

Error messages, in check order:

| Condition | `message` |
|---|---|
| No runtime context | `Thread move tool context is unavailable in this runtime path.` |
| Thread target error | the `resolve_canonical_tool_thread_target` error |
| Room ID invalid | the `resolve_requested_room_id` error |
| Target is current room | `The thread is already in this room.` |
| Access denied | `Not authorized to access the target room.` |
| History incomplete | `This thread is too long to move.` |
| Encrypted to unencrypted | `Cannot move a thread from an encrypted room into an unencrypted room.` |
| Encryption state unknown | `Could not determine whether the source and target rooms are encrypted.` |
| Router not running or not joined | `The router must be in the target room to move a thread.` |
| Acting agent not joined | `Invite {agent_name} to the target room before moving a thread there.` |
| Empty plan | `This thread has no messages to move.` |
| Send failure | `Copied {copied} of {total} messages before a send failed.` plus `link` of the partial thread when one exists |

Execution order follows the spec: post the plan (first post is the root, later posts get `build_thread_relation(new_root_id, latest_thread_event_id=previous_event_id)`), copy tags with `set_by=context.requester_id` and the source tag's `note` and `data`, post the `m.notice` `Moved to {link}` in the source thread (relation to the source root with `latest_thread_event_id` set to the last history message, plus the trigger-guard keys), then `set_thread_tag(..., RESOLVED_THREAD_TAG, set_by=context.requester_id)`.
`ThreadTagsError` and a failed notice send each append a string to `warnings`.
Permalink: `https://matrix.to/#/{quote(room_id, safe='')}/{quote(event_id, safe='')}?via={server}` with `server` from `context.client.user_id`.
Success payload: `status="ok"`, `room_id`, `thread_id`, `link`, `copied`, `skipped`, `tags_copied` (sorted tag names), `warnings`.

- [ ] **Step 1: Write the failing tests** in `tests/test_thread_move_tool.py`, with the context built as in `tests/test_thread_resolution_tool.py`, a small fake orchestrator exposing `running_entity_client`, and the Matrix helpers patched at `mindroom.custom_tools.thread_move.*`:
  - `test_thread_move_tool_registered`: `TOOL_METADATA["thread_move"].function_names == ("move_thread",)` and `get_tool_by_name("thread_move", ...)` builds the toolkit.
  - `test_move_thread_without_context_returns_error`.
  - `test_move_thread_requires_a_thread`: no active thread and no `thread_id`.
  - `test_move_thread_rejects_current_room`.
  - `test_move_thread_rejects_unauthorized_target` (`room_access_allowed` patched to `False`).
  - `test_move_thread_rejects_incomplete_history` (`is_full_history=False`); assert no send.
  - `test_move_thread_refuses_encrypted_to_unencrypted`; assert no send.
  - `test_move_thread_requires_router_and_agent_in_target`: parametrized over router missing from members, router not running, acting agent missing.
  - `test_move_thread_rejects_thread_with_only_skipped_messages`.
  - `test_move_thread_copies_messages_in_order`: human root, agent reply from a joined agent, reply from an agent missing from the target; assert send order, the client used for each send, no `m.relates_to` on the root, `m.thread` relation to the new root with `m.in_reply_to` the previous copy on later posts.
  - `test_move_thread_copies_tags_posts_notice_and_resolves_source`: `set_thread_tag` awaited for each source tag on the new root with `set_by` the requester, notice sent with `context.client` into the source room with body `Moved to https://matrix.to/#/...`, then `resolved` set on the source root; payload `tags_copied` and empty `warnings`.
  - `test_move_thread_reports_partial_copy_on_send_failure`: second send returns `None`; payload is an error with `Copied 1 of 3 messages before a send failed.` and a `link`; no tag, notice, or resolve calls.
  - `test_move_thread_reports_tag_failure_as_warning`: `set_thread_tag` raises `ThreadTagsError`; status stays `ok` and `warnings` is non-empty.
  - Ingress: `test_moved_router_copy_does_not_dispatch` in the file that holds the existing skip-mentions ingress tests: a router-sent message with `ORIGINAL_SENDER_KEY`, `SKIP_MENTIONS_KEY`, and an `@agent` mention in its body, in a thread the agent took part in, is not dispatched.
- [ ] **Step 2: Run** `uv run pytest tests/test_thread_move_tool.py -x -n 0 --no-cov` and expect failures for the missing toolkit.
- [ ] **Step 3: Implement** `ThreadMoveTools`, the registration module, the `tools/__init__.py` import and `__all__` entry, the `pyproject.toml` extra, and a `tach.toml` module entry for `mindroom.custom_tools.thread_move`; extend `visibility` lists that `uv run tach check --dependencies --interfaces` reports.
- [ ] **Step 4: Regenerate** `src/mindroom/tools_metadata.json` with the command printed by `tests/test_tools_metadata.py`.
- [ ] **Step 5: Run** `uv run pytest tests/test_thread_move_tool.py tests/test_tools_metadata.py tests/test_orchestrator_runtime.py -x -n 0 --no-cov` and `uv run tach check --dependencies --interfaces`; expect PASS.
- [ ] **Step 6: Commit** `feat: add thread_move tool to move a thread into another room`.

### Task 4: Docs

**Files:**
- Modify: `docs/tools/matrix-and-attachments.md` (tools list, a `## [\`thread_move\`]` section after `thread_resolution`, link reference)
- Modify: `docs/tools/index.md` (Matrix & Attachments blurb), `docs/dev/agent_configuration.md` (Communication Tools list), `docs/architecture/code-map.md` (row for `custom_tools/thread_move.py`)

The `thread_move` section answers: how to enable it (YAML example), what `move_thread` does, who can move a thread (requester joined to the target room; router and the agent in the target room), why a move was refused (encrypted to unencrypted, too long), what the original thread shows, and what does not move (saved tool-call results, per-thread model choices, agent modes, todos, scheduled tasks, pending approvals; copies have new timestamps and lose reactions and edit history).

- [ ] **Step 1: Write** the docs.
- [ ] **Step 2: Run** `uv run pytest tests/test_docs_links.py tests/test_docs_frontmatter.py -x -n 0 --no-cov` and `uv run pre-commit run --files <changed docs>`; expect PASS (the hook regenerates `skills/mindroom-docs/references/`; add those files).
- [ ] **Step 3: Commit** `docs: document the thread_move tool`.

### Task 5: Full verification and live test

- [ ] **Step 1: Run** `uv run pre-commit run --all-files` and the full backend suite (`just test-backend` inside `nix-shell shell.nix`); expect PASS.
- [ ] **Step 2: Live test** with the `live-test` skill: two rooms, one human, two agents with `thread_move` enabled; build a thread with a human message, two agent replies (one mentioning the other agent), and an image; ask an agent to move it.
  Confirm no agent replies to the copies, copies show the right senders and names, the source thread shows the link and `resolved`, and an untagged follow-up in the new thread gets an answer that uses the earlier conversation.
- [ ] **Step 3: Commit** any fixes the live test required, each with its regression test.
