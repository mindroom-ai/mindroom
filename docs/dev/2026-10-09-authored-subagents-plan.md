# Authored Subagents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** baspowers:executing-plans — the user prefers inline implementation with a fresh-context reviewer after each substantial task and a final whole-branch review.

**Goal:** Let an agent run subagents whose entire system prompt, tool subset, model, and mode it authors, inline or from editable workspace profile files, and rebuild Dynamic Workflow participants on the same primitive.

**Architecture:** A typed `SubagentPersona` rides on `DelegationChild` and `ResponseTurnContext`, and `create_agent` applies it as the verbatim `system_message` plus a tool filter while keeping the caller's own principal.
Profiles are `subagents/<name>.md` files read through no-follow confinement.
Workflow `subagent` participants become delegation children with a workflow grant, so they share construction, authorization, records, and the caller tool cap.

**Tech Stack:** Python 3.13, Agno `Agent.system_message`, existing delegation lifecycle, `path_confinement`, `parse_skill_markdown`, pytest.

**Spec:** `docs/dev/2026-10-09-authored-subagents-design.md`

## Global Constraints

- Persona `system_prompt`: 1 byte to 64 KiB UTF-8 (`MAX_PERSONA_PROMPT_BYTES = 64 << 10`), sent byte for byte with `resolve_in_context = False`.
- Profile file: `<workspace>/subagents/<name>.md`, name regex `[a-z0-9][a-z0-9_-]{0,63}`, file at most 64 KiB, at most 256 profiles read, description at most 1,024 characters, listing at most 2,000 characters.
- Profile frontmatter keys: `description` (required), `tools`, `model`, `mode`; any other key is invalid.
- Authoring (`system_prompt`, `tools`, `profile`) is allowed only for the caller itself, and only when its own name is in `delegate_to` (delegate path) or it has `dynamic_workflow` (workflow path).
- Persona `tools` entries are toolkit names or `toolkit.function`, and must be a subset of the caller's toolkits in this execution.
- Exact error copy:
  - `Cannot author a subagent for '<agent_name>': system_prompt, tools, and profile apply only to yourself.`
  - `Cannot delegate: pass either profile or system_prompt and tools, not both.`
  - `Cannot delegate: unknown tool '<entry>'. Your tools: <comma-separated toolkit names>.`
  - `Cannot delegate: subagent profile '<name>' was not found in subagents/.`
  - `Cannot delegate: subagent profile '<name>' is invalid: <reason>.`
  - `Subagent tool '<entry>' is no longer available to you; start a new subagent.`
- Bash description sentence for minimal personas, appended verbatim: `MindRoom tools are callable from Bash through mindroom-agent; run mindroom-agent --help to list them.`
- No in-function imports except cycle breaks marked `# noqa: PLC0415`; no `getattr`/`hasattr` probing; dataclasses over dicts.
- Docs follow one sentence per line and the repository documentation policy.
- Run tests with `TMPDIR=/work/tmp uv run pytest <file> -x -n 0 --no-cov -v`, inside `nix-shell shell.nix` when the host is NixOS.

## Review Focus

- A profile edited or deleted while a standard persona child awaits approval must resume with the frozen persona; tested in Task 4.
- A follow-up after the caller lost a persona tool must fail with the "no longer available" error, never run with a broader or different surface; tested in Task 4.
- An inline persona with `tools=[]` must run with no tools at all, not fall back to every caller tool; tested in Task 1 and Task 2.
- A `subagents/` directory holding uppercase names, non-`.md` files, symlinks, subdirectories, and more than 256 entries must list only valid regular files and never follow a link; tested in Task 1.
- A team member caller on the native delegation path must author a persona for itself, not for the team; tested in Task 4.

---

### Task 1: Persona module

**Files:**
- Create: `src/mindroom/delegation/personas.py`
- Test: `tests/test_subagent_personas.py`
- Modify: `docs/architecture/code-map.md` (one row for `delegation/personas.py`), `tach.toml` (new module entry)

**Interfaces:**
- Produces:
  - `PersonaSourceKind = Literal["inline", "profile", "workflow"]`
  - `@dataclass(frozen=True) class SubagentPersona: source_kind: PersonaSourceKind; source_name: str; system_prompt: str; tools: tuple[str, ...] | None = None` with `to_dict() -> dict[str, object]`, `@classmethod from_dict(data: Mapping[str, object]) -> SubagentPersona`, and `prompt_sha256 -> str` property. `source_name` is `""` for inline, the profile name for profiles, and `"<workflow_id>/<participant_id>"` for workflows.
  - `@dataclass(frozen=True) class PersonaProfile: name: str; description: str; persona: SubagentPersona; model: str | None; mode: AgentMode | None`
  - `@dataclass(frozen=True) class InvalidPersonaProfile: name: str; reason: str`
  - `class PersonaError(ValueError)` whose `str()` is the user-facing reason.
  - `inline_persona(system_prompt: object, tools: object, *, source_kind: PersonaSourceKind = "inline", source_name: str = "") -> SubagentPersona`
  - `parse_profile(name: str, content: str) -> PersonaProfile`
  - `load_profile(workspace_root: Path, name: str) -> PersonaProfile`
  - `list_profiles(workspace_root: Path) -> list[PersonaProfile | InvalidPersonaProfile]`
  - `render_profile_listing(entries: Sequence[PersonaProfile | InvalidPersonaProfile]) -> str`
  - `validate_persona_tools(tools: tuple[str, ...] | None, available_toolkits: Sequence[str]) -> None`
  - `persona_function_filter(persona: SubagentPersona | None) -> Callable[[Function], bool] | None`
  - `persona_disabled_toolkits(persona: SubagentPersona | None, available_toolkits: Sequence[str]) -> frozenset[str]`

- [ ] **Step 1: Write failing tests** in `tests/test_subagent_personas.py`:
  - `test_inline_persona_keeps_prompt_bytes`: prompt `"Use {braces} and {{x}}\n"` round-trips unchanged through `inline_persona` and `SubagentPersona.from_dict(p.to_dict())`.
  - `test_inline_persona_rejects_empty_oversized_and_wrong_types`: `""`, `"x" * (64 * 1024 + 1)`, `tools="file"`, and `tools=[1]` each raise `PersonaError`.
  - `test_parse_profile_reads_frontmatter_and_body`: content with `description`, `tools: [file]`, `model: haiku`, `mode: minimal` yields those values, `source_kind == "profile"`, `source_name == name`, and the stripped body as `system_prompt`.
  - `test_parse_profile_rejects_bad_frontmatter`: missing description, unknown key `role`, `mode: fast`, empty body, and description of 1,025 characters each raise `PersonaError`.
  - `test_list_profiles_skips_unsafe_entries`: a workspace with `subagents/Critic.md`, `subagents/notes.txt`, `subagents/dir.md/`, a symlink `subagents/link.md`, a valid `subagents/critic.md`, and one invalid `subagents/broken.md` returns exactly `[PersonaProfile(name="critic"), InvalidPersonaProfile(name="broken")]`; uppercase and symlinked entries are omitted or invalid but never read through the link.
  - `test_list_profiles_caps_count_and_size`: 300 valid files return 256 entries sorted by name, and a 64 KiB + 1 byte file becomes `InvalidPersonaProfile`.
  - `test_load_profile_missing_raises`: `load_profile(root, "absent")` raises `PersonaError` whose text is the not-found error copy.
  - `test_render_profile_listing_bounds_length`: 256 entries render to a count line naming `subagents/` when the full listing would exceed 2,000 characters.
  - `test_validate_persona_tools_accepts_subset_and_function_entries`: `("file", "gmail.search_emails")` passes for available `["file", "gmail"]` when `gmail` metadata lists `search_emails`.
  - `test_validate_persona_tools_rejects_unknown_toolkit_and_function`: `("shell",)` and `("gmail.send_email_x",)` raise with the unknown-tool error copy listing `file, gmail`.
  - `test_empty_tool_list_means_no_tools`: `inline_persona("P", [])` has `tools == ()`, its filter rejects every function, and `persona_disabled_toolkits` returns every available toolkit.
  - `test_persona_function_filter_matches_owner_and_function`: a filter for `("gmail.search_emails", "file")` accepts `Function(owning_toolkit="file")`, accepts `gmail`/`search_emails`, rejects `gmail`/`send_email`, and rejects `owning_toolkit=None`; `persona_function_filter(None)` is `None`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `TMPDIR=/work/tmp uv run pytest tests/test_subagent_personas.py -x -n 0 --no-cov -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mindroom.delegation.personas'`

- [ ] **Step 3: Implement `src/mindroom/delegation/personas.py`**

Split profile files with `parse_skill_markdown` from `mindroom.tool_system.skills`.
List and read through `open_directory_within_root`, `workspace_entry_names(directory_fd, directories=False)`, and `read_regular_file_within_root(..., max_bytes=64 << 10)`, as `_load_workspace_skills` does.
Resolve `toolkit.function` entries against `TOOL_METADATA[toolkit].function_names` from `mindroom.tool_system.catalog`; an empty `function_names` accepts any function name.
`persona_disabled_toolkits` returns the available toolkits not named by any entry, or an empty set when `tools` is `None`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `TMPDIR=/work/tmp uv run pytest tests/test_subagent_personas.py -x -n 0 --no-cov -v`
Expected: PASS

- [ ] **Step 5: Add the code-map row and the Tach module entry, then run** `uv run tach check --dependencies --interfaces`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/mindroom/delegation/personas.py tests/test_subagent_personas.py docs/architecture/code-map.md tach.toml
git commit -m "feat(delegation): add subagent personas and workspace profile files"
```

### Task 2: Apply a persona when building an agent

**Files:**
- Modify: `src/mindroom/response_turn.py` (`ResponseTurnContext`), `src/mindroom/agents.py` (`create_agent`), `src/mindroom/minimal_agent.py`, `src/mindroom/agent_cli/bash.py`, `src/mindroom/ai.py` (`ai_response` agent build and memory prompt preparation)
- Modify: `src/mindroom/delegation/personas.py` (add `apply_persona`)
- Test: `tests/test_agent_personas.py`

**Interfaces:**
- Consumes: `SubagentPersona`, `persona_function_filter`, `persona_disabled_toolkits` from Task 1.
- Produces:
  - `ResponseTurnContext.persona: SubagentPersona | None = None`
  - `create_agent(..., persona: SubagentPersona | None = None) -> Agent`
  - `apply_persona(agent: Agent, persona: SubagentPersona) -> None` in `personas.py`
  - `MinimalBashTools(..., persona_hint: bool = False)`
  - `MinimalAgent.persona_hint: bool = False`

- [ ] **Step 1: Write failing tests** in `tests/test_agent_personas.py`, building agents with `create_agent` and the repo's existing config fixtures:
  - `test_persona_system_message_is_verbatim`: with persona prompt `"Plain {not_a_var} text"`, the system message Agno builds for a run equals that string exactly, even after `ai_response` appends session preamble and enrichment to `additional_context`.
  - `test_persona_tool_subset_hides_other_functions`: a caller with `file` and `shell` and persona `tools=("file",)` exposes only `file` functions and constructs no `shell` toolkit.
  - `test_empty_persona_tools_build_toolless_agent`: persona `tools=()` produces an agent with no provider-visible functions.
  - `test_persona_disables_learning_and_memory_recall`: `agent.learning` is falsy and `ai_response` with `ctx.persona` set never calls `build_memory_prompt_parts` (patched to raise).
  - `test_minimal_persona_uses_authored_prompt_and_bash_hint`: with `agent_mode="minimal"`, `bootstrap_message` and `system_message` equal the persona prompt, and the Bash function description ends with the Global Constraints sentence.
  - `test_agent_without_persona_is_unchanged`: `create_agent` without a persona produces the same `system_message`, `instructions`, and Bash description as before.

- [ ] **Step 2: Run tests to verify they fail**

Run: `TMPDIR=/work/tmp uv run pytest tests/test_agent_personas.py -x -n 0 --no-cov -v`
Expected: FAIL with `TypeError: create_agent() got an unexpected keyword argument 'persona'`

- [ ] **Step 3: Implement**

In `create_agent`, compose `persona_function_filter(persona)` with `tool_function_filter`, add `persona_disabled_toolkits(persona, get_agent_toolkit_names(agent_name, config, session_id=session_id, delegation_depth=delegation_depth))` to `disabled_tool_names`, force `learning=False` for personas, and call `apply_persona(agent, persona)` after `configure_minimal`.
`apply_persona` sets `system_message`, sets `resolve_in_context = False`, and for a `MinimalAgent` also sets `bootstrap_message` and `persona_hint = True`.
`MinimalAgent` passes `persona_hint` to both `MinimalBashTools` constructions.
`ai_response` passes `persona=ctx.persona` to `create_agent` and uses an empty `MemoryPromptParts()` instead of `build_memory_prompt_parts` when `ctx.persona` is set.

- [ ] **Step 4: Run tests to verify they pass**, plus `tests/test_minimal_agent.py` and `tests/test_minimal_bash.py`

Run: `TMPDIR=/work/tmp uv run pytest tests/test_agent_personas.py tests/test_minimal_agent.py tests/test_minimal_bash.py -x -n 0 --no-cov -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/response_turn.py src/mindroom/agents.py src/mindroom/minimal_agent.py src/mindroom/agent_cli/bash.py src/mindroom/ai.py src/mindroom/delegation/personas.py tests/test_agent_personas.py
git commit -m "feat(agents): build an agent from an authored persona"
```

### Task 3: Persona state, grants, and child runs

**Files:**
- Modify: `src/mindroom/delegation/state.py`, `src/mindroom/delegation/sessions.py`, `src/mindroom/delegation/lifecycle.py`, `src/mindroom/ai.py` (`run_delegated_child_response`), `src/mindroom/delegation/execution.py` (approval-resume `create_agent` call)
- Modify: `docs/architecture/migrations.md`
- Test: `tests/test_delegation_sessions.py`, `tests/test_delegation_execution.py`, `tests/test_delegation_envelopes.py`

**Interfaces:**
- Consumes: `SubagentPersona`, `create_agent(persona=...)`, `ResponseTurnContext.persona`.
- Produces:
  - `DelegationChild.persona: SubagentPersona | None = None`, with a `LEGACY_COMPAT` marker in the `agent_mode` style.
  - `DelegationChild.from_dict(data: Mapping[str, object]) -> DelegationChild`, used by every site that today calls `DelegationChild(**...)` (`state.py:96`, `state.py:102`, `sessions.py:64`, `sessions.py:97`); `asdict` output stores `persona` as `persona.to_dict()` or `None`.
  - `DelegationGrant = Literal["delegate", "dynamic_workflow"]` and `delegation_grant(child: DelegationChild) -> DelegationGrant`, returning `dynamic_workflow` exactly when `child.persona.source_kind == "workflow"`.
  - `authorize_delegation(..., grant: DelegationGrant = "delegate", approval_config: Config | None = None) -> Config | str`: the `delegate` grant keeps today's allowlist rule; `dynamic_workflow` requires `agent_name == caller_name` and `dynamic_workflow` among the caller's toolkit names in the active config; when `approval_config` is given it is returned in place of the active config after all checks pass.
  - `prepare_child_turn(..., persona: SubagentPersona | None = None)`; a follow-up keeps `previous.persona`.
  - `run_delegated_child_response(..., approval_config: Config | None = None)` passes `grant=delegation_grant(child)` and `approval_config` to `authorize_delegation`, and `persona=child.persona` into its `ResponseTurnContext`.

- [ ] **Step 1: Write failing tests**
  - `tests/test_delegation_sessions.py::test_persona_round_trips_through_session_record`: a reserved child with a profile persona reloads through `load_subagent` with an equal `SubagentPersona`.
  - `tests/test_delegation_sessions.py::test_child_snapshot_without_persona_reads_as_configured_child`: a stored payload lacking `persona` loads with `persona is None`; name this node in the `LEGACY_COMPAT` coverage line.
  - `tests/test_delegation_envelopes.py::test_child_turn_context_carries_persona`: `run_delegated_child_response` builds a `ResponseTurnContext` whose `persona` equals `child.persona`.
  - `tests/test_delegation_execution.py::test_approval_resume_rebuilds_child_with_persona`: the resume path calls `create_agent` with `persona=child.persona`.
  - `tests/test_delegation_execution.py::test_workflow_grant_authorizes_without_delegate_to`: a workflow-sourced child of a caller without `delegate_to` but with `dynamic_workflow` is authorized, and the same child after `dynamic_workflow` is removed gets an error string.

- [ ] **Step 2: Run tests to verify they fail**

Run: `TMPDIR=/work/tmp uv run pytest tests/test_delegation_sessions.py tests/test_delegation_envelopes.py tests/test_delegation_execution.py -x -n 0 --no-cov -v`
Expected: FAIL on the new tests only.

- [ ] **Step 3: Implement the interfaces above**, then add the `DelegationChild.persona` default to the boundary map in `docs/architecture/migrations.md`.

The `LEGACY_COMPAT` marker states: legacy format is a delegation snapshot without `persona`; last legacy release is the latest tag at merge time (today `v2026.10.216`), replacement is the next release writing `persona`; handling reads it as a configured-agent child.

- [ ] **Step 4: Run the delegation suite**

Run: `TMPDIR=/work/tmp uv run pytest tests/ -k "delegat" -x -n 0 --no-cov`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/delegation/state.py src/mindroom/delegation/sessions.py src/mindroom/delegation/lifecycle.py src/mindroom/ai.py src/mindroom/delegation/execution.py docs/architecture/migrations.md tests/test_delegation_sessions.py tests/test_delegation_envelopes.py tests/test_delegation_execution.py
git commit -m "feat(delegation): retain authored personas across child turns and restarts"
```

### Task 4: `run_subagent` persona parameters on both delegation paths

**Files:**
- Modify: `src/mindroom/delegation/personas.py` (add request resolution), `src/mindroom/custom_tools/delegate.py`, `src/mindroom/delegation/execution.py` (`_DelegationTarget`, `_resolve_delegation_target`, `_validate_child_scope`, `advance_delegation_call`), `src/mindroom/agents.py` (pass `workspace_root` to `DelegateTools`), `src/mindroom/prompts.py` or the template that renders `DELEGATE_TOOLKIT_INSTRUCTIONS_TEMPLATE` if the profile listing needs a slot
- Modify: `docs/tools/agent-orchestration.md` (`delegate` section), `docs/tools/agent-cli.md` (one sentence under minimal subagents), `docs/architecture/security-posture.md` (`subagents/*.md` in the workspace files the primary reads, with its caps)
- Test: `tests/test_delegate_tools.py`, `tests/test_delegation_execution.py`, `tests/test_delegation_minimal_mode.py`

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces:
  - `@dataclass(frozen=True) class PersonaRequest: persona: SubagentPersona | None; model: str | None; agent_mode: AgentMode`
  - `resolve_persona_request(*, caller_name: str, agent_name: str, system_prompt: object, tools: object, profile: object, model: str | None, minimal: bool, workspace_root: Path | None, available_toolkits: Sequence[str]) -> PersonaRequest | str` in `personas.py`; returns an error string using the Global Constraints copy; with a profile, an explicit `model` overrides the profile model and `minimal=True` overrides the profile mode.
  - `DelegateTools(..., workspace_root: Path | None = None)`; `run_subagent(task, agent_name=None, model=None, minimal=False, system_prompt=None, tools=None, profile=None)`.
  - `_DelegationTarget.persona: SubagentPersona | None = None`.

- [ ] **Step 1: Write failing tests**
  - `tests/test_delegate_tools.py::test_inline_persona_starts_self_child`: `run_subagent(task=..., system_prompt="P", tools=["file"])` prepares a child whose `child_agent_name` is the caller and whose persona has `source_kind == "inline"`.
  - `test_profile_persona_applies_profile_model_and_mode`: profile `mode: minimal`, `model: haiku` yields `agent_mode == "minimal"` and `model_name == "haiku"`; explicit `model="sonnet"` overrides.
  - `test_persona_for_other_agent_is_refused`: `agent_name="research"` with `system_prompt` returns the self-only error copy.
  - `test_profile_and_inline_conflict_is_refused`: `profile="critic", system_prompt="x"` returns the conflict error copy.
  - `test_persona_parameters_hidden_without_self_delegation`: a caller whose `delegate_to` lacks itself exposes no `system_prompt`, `tools`, or `profile` parameter and no profile listing.
  - `test_instructions_list_profiles`: toolkit instructions contain `critic: <description>` and `broken (invalid: <reason>)`.
  - `test_follow_up_keeps_snapshot_after_profile_edit`: editing `subagents/critic.md` between `run_subagent` and `continue_subagent` leaves the follow-up's `persona.system_prompt` unchanged.
  - `test_follow_up_after_caller_lost_tool_is_refused`: removing `file` from the caller's config before `continue_subagent` returns the "no longer available" error copy.
  - `tests/test_delegation_execution.py::test_native_resume_uses_frozen_persona_after_profile_delete`: a standard profile child paused for approval resumes with its persona after `subagents/critic.md` is deleted.
  - `tests/test_delegation_execution.py::test_team_member_authors_persona_for_itself`: a native requirement with `member_agent_id` set produces a child whose `child_agent_name` is the member's config name.
  - `tests/test_delegation_minimal_mode.py::test_minimal_persona_hides_gated_tools`: a minimal persona child never exposes an approval-gated function.

- [ ] **Step 2: Run tests to verify they fail**

Run: `TMPDIR=/work/tmp uv run pytest tests/test_delegate_tools.py tests/test_delegation_execution.py tests/test_delegation_minimal_mode.py -x -n 0 --no-cov -v`
Expected: FAIL on the new tests only.

- [ ] **Step 3: Implement**

Hide the three parameters exactly as `minimal` is hidden today in `DelegateTools.__init__`, deriving the schema once and dropping them when `self._agent_name not in self._delegate_to`.
On the native path, resolve the caller workspace with `resolve_agent_runtime(caller, config, runtime_paths, caller_identity).workspace`, and reuse `retained.persona` whenever a retained child exists so a resumed call never rereads the profile.
Extend `_validate_child_scope` to require `child.persona == target.persona` for fresh calls.
On every follow-up, recheck `validate_persona_tools(child.persona.tools, available_toolkits)` and map its failure to the "no longer available" copy.
Add the description lines for the new parameters only when they are shown, and keep each to one sentence.

- [ ] **Step 4: Run the delegation suite**

Run: `TMPDIR=/work/tmp uv run pytest tests/ -k "delegat or minimal" -x -n 0 --no-cov`
Expected: PASS

- [ ] **Step 5: Update the three docs pages per the spec's Documentation section and commit**

```bash
git add src/mindroom/delegation/personas.py src/mindroom/custom_tools/delegate.py src/mindroom/delegation/execution.py src/mindroom/agents.py docs/tools/agent-orchestration.md docs/tools/agent-cli.md docs/architecture/security-posture.md tests/test_delegate_tools.py tests/test_delegation_execution.py tests/test_delegation_minimal_mode.py
git commit -m "feat(delegate): let agents author subagent prompts, tools, and profiles"
```

### Task 5: Persona audit fields

**Files:**
- Modify: `src/mindroom/delegation/records.py` (`DelegationMetadata`, `run.json` writer, transcript renderer near `records.py:768`), `src/mindroom/delegation/audit.py` (`start_child_record`)
- Test: `tests/test_delegation_records.py`, `tests/test_delegation_direct_audit.py`

**Interfaces:**
- Consumes: `SubagentPersona.prompt_sha256`.
- Produces: `DelegationMetadata.persona: SubagentPersona | None = None`; `run.json` gains `"persona": {"source_kind", "source_name", "tools", "system_prompt_sha256"}` or `null`; `transcript.md` gains a `## System prompt` section containing the redacted prompt when a persona exists.

- [ ] **Step 1: Write failing tests**
  - `tests/test_delegation_records.py::test_run_json_records_persona_digest`: a persona child's `run.json` `persona.system_prompt_sha256` equals `hashlib.sha256(prompt.encode()).hexdigest()` and `persona.tools == ["file"]`.
  - `test_transcript_includes_redacted_system_prompt`: a prompt containing a secret-shaped token renders redacted under `## System prompt`; build the token at runtime by concatenation.
  - `tests/test_delegation_direct_audit.py::test_configured_child_record_has_null_persona`: a configured-agent child's `run.json` has `"persona": null` and its transcript has no `## System prompt` section.

- [ ] **Step 2: Run tests to verify they fail**

Run: `TMPDIR=/work/tmp uv run pytest tests/test_delegation_records.py tests/test_delegation_direct_audit.py -x -n 0 --no-cov -v`
Expected: FAIL on the new tests only.

- [ ] **Step 3: Implement** the fields, reusing the record's existing redaction for the prompt text.

- [ ] **Step 4: Run tests to verify they pass**

Run: `TMPDIR=/work/tmp uv run pytest tests/ -k "delegation_records or delegation_audit or direct_audit" -x -n 0 --no-cov`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/delegation/records.py src/mindroom/delegation/audit.py tests/test_delegation_records.py tests/test_delegation_direct_audit.py
git commit -m "feat(delegation): record each authored subagent's persona in its audit record"
```

### Task 6: Workflow subagent participants on the delegation primitive

Schema, legacy reader, and execution change together, because a schema without its executor leaves no working intermediate commit.

**Files:**
- Create: `src/mindroom/dynamic_workflows/legacy_participants.py`
- Modify: `src/mindroom/dynamic_workflows/validation.py` (`_PARTICIPANT_KINDS`, participant keys, `_validate_participant`, replace `_validate_ephemeral_agent_participant`), `src/mindroom/dynamic_workflows/store.py` (`_load_revision`), `src/mindroom/dynamic_workflows/runner.py` (`_DynamicWorkflowStepResult`, executor protocols), `src/mindroom/custom_tools/dynamic_workflow.py` (replace `_execute_ephemeral_agent_participant`, `_aexecute_ephemeral_agent_participant`, `_resolve_participant_toolkits`, `_participant_instructions`, and the caller-active-model rule in `_validate_workflow_policy_for_context`)
- Modify: `docs/tools/agent-orchestration.md` (`dynamic_workflow` participants section), `docs/architecture/migrations.md`, `tach.toml`
- Test: `tests/test_dynamic_workflow_validation.py`, `tests/test_dynamic_workflows.py`

**Interfaces:**
- Consumes: `inline_persona`, `load_profile`, `validate_persona_tools`, `prepare_child_turn`, `reserve_child_turn`, `start_child_turn`, `child_run_context`, `run_delegated_child_response(approval_config=...)`, `finish_child_turn`, `subagent_liveness`.
- Produces:
  - `_PARTICIPANT_KINDS = frozenset({"subagent", "room_agent"})`; default kind `subagent`.
  - `subagent` keys: `id`, `kind`, `description`, and either `profile` or the inline set `system_prompt`, `tools`, `model`, `mode`.
  - `upgrade_legacy_participants(spec: dict[str, object]) -> dict[str, object]` in `legacy_participants.py`, returning a new spec where each `ephemeral_agent` participant is a `subagent` participant with `system_prompt = render_legacy_participant_prompt(name, role, instructions)` and the same `id`, `description`, `model`, and `tools`.
  - `render_legacy_participant_prompt(name: str | None, role: str | None, instructions: str | list[str] | None) -> str`: `"You are {name}."` when `name` is set, then `role` as its own paragraph, then each instruction as a `- ` line, paragraphs joined by a blank line; empty parts are skipped; an all-empty input yields `"You are a Dynamic Workflow participant."`.
  - `@dataclass(frozen=True) class ParticipantOutput: content: object; delegation_id: str | None = None` in `runner.py`; both executor protocols return it.
  - `_DynamicWorkflowStepResult.delegation_id: str | None = None`, serialized as `"delegation_id"` in `to_json`.
  - One `DelegationChild` per `(run_scope, participant_id)`, kept in the executor closure; the first step prepares a fresh child and later steps prepare follow-ups with `previous=child`.

- [ ] **Step 1: Write failing tests**
  - `tests/test_dynamic_workflow_validation.py::test_subagent_participant_accepts_inline_and_profile`: both example participants from the spec validate.
  - `test_subagent_participant_rejects_profile_with_inline_fields`: `profile` plus `system_prompt` fails.
  - `test_ephemeral_agent_kind_is_rejected_for_new_specs`: `create_workflow` with `kind: ephemeral_agent` fails validation.
  - `tests/test_dynamic_workflows.py::test_legacy_revision_loads_as_subagent`: a revision YAML written with an `ephemeral_agent` participant (`name: Writer`, `role: Writes`, `instructions: [Cite sources]`) loads as `subagent` with `system_prompt == "You are Writer.\n\nWrites\n\n- Cite sources"`.
  - `test_update_of_legacy_revision_writes_current_format`: `update_workflow` on that workflow writes a revision containing no `ephemeral_agent`, `role`, `name`, or `instructions` participant keys.
  - `test_participant_tools_must_be_caller_tools`: a caller without `gmail` running a participant with `tools: [gmail]` fails with the unknown-tool error copy.
  - `test_participant_steps_write_delegation_records`: a two-step run with one participant writes two delegation records with equal `subagent_id`, and `step_outputs.json` lists both `delegation_id` values.
  - `test_participant_may_use_any_configured_model`: `model: haiku` runs when the caller's active model is `sonnet`.
  - `test_unapproved_gated_participant_tool_is_rejected`: a participant naming a toolkit with an approval-gated function not in `allowed_tools` fails validation.
  - `test_null_tools_participant_hides_gated_and_infrastructure_tools`: a participant without `tools` exposes neither the gated function nor `delegate`, `dynamic_workflow`, `memory`, or `self_config`.
  - `test_profile_participant_runs_workspace_profile`: `profile: critic` runs with the profile's prompt as its system message.
  - Update existing execution tests that build `ephemeral_agent` participants to `subagent` participants; the existing `room_agent` tests stay unmodified and must still pass.

- [ ] **Step 2: Run tests to verify they fail**

Run: `TMPDIR=/work/tmp uv run pytest tests/test_dynamic_workflow_validation.py tests/test_dynamic_workflows.py -x -n 0 --no-cov -v`
Expected: FAIL on the new tests only.

- [ ] **Step 3: Implement the schema and legacy reader**

Call `upgrade_legacy_participants` inside `_load_revision`, with a `LEGACY_COMPAT` marker whose last legacy release is the latest tag at merge time and whose coverage names the two legacy store tests; add the boundary to `docs/architecture/migrations.md`.

- [ ] **Step 4: Implement execution**

Build `SubagentPersona(source_kind="workflow", source_name=f"{workflow_id}/{participant_id}", ...)` from inline fields or the profile.
Compute available toolkits as the caller's toolkit names minus `_WORKFLOW_RESTRICTED_TOOLS`; when `tools` is `None`, narrow the persona to those toolkits so infrastructure tools stay out and gated functions stay hidden.
Pass `build_automation_approval_config(...)` as `approval_config`, so pre-approved functions run and unapproved gated ones are rejected with today's `_reject_nonresumable_toolkits` semantics, applied to the persona's toolkits.
Catch `ResponsePausedForApproval` from a child, settle it as failed with `finish_child_turn`, and raise `DynamicWorkflowExecutionError("Dynamic Workflow participant '<id>' required approval and cannot pause.")`.
Delete the now-unused ephemeral construction helpers.

- [ ] **Step 5: Run tests and boundaries**

Run: `TMPDIR=/work/tmp uv run pytest tests/ -k "workflow or delegat" -x -n 0 --no-cov && uv run tach check --dependencies --interfaces`
Expected: PASS and no Tach errors.

- [ ] **Step 6: Update the workflow docs section and commit**

```bash
git add src/mindroom/dynamic_workflows/legacy_participants.py src/mindroom/dynamic_workflows/validation.py src/mindroom/dynamic_workflows/store.py src/mindroom/dynamic_workflows/runner.py src/mindroom/custom_tools/dynamic_workflow.py docs/tools/agent-orchestration.md docs/architecture/migrations.md tach.toml tests/test_dynamic_workflow_validation.py tests/test_dynamic_workflows.py
git commit -m "refactor(workflows): run subagent participants through the delegation lifecycle"
```

### Task 7: Whole-branch verification

**Files:** none new.

- [ ] **Step 1: Full suite**

Run: `TMPDIR=/work/tmp uv run pytest -n auto --no-cov -q`
Expected: all pass.

- [ ] **Step 2: Hooks**

Run: `TMPDIR=/work/tmp uv run pre-commit run --all-files`
Expected: all hooks pass except failures already present on `origin/main`, which are reported, not fixed here.

- [ ] **Step 3: Live test** with the `live-test` skill against two real providers through Matty: a quarantine-reader inline persona with `tools: [duckduckgo]`, a `critic` profile created by the agent with its own file tools and continued once, a minimal persona, and one workflow with two `subagent` participants; confirm each delegation record shows the persona digest.

- [ ] **Step 4: Reviews**: one fresh-context Opus review of the whole branch, plus read-only GPT-6 Astra and GPT-6.1 Sol reviews; fix confirmed findings in follow-up commits and repeat until all three approve.
