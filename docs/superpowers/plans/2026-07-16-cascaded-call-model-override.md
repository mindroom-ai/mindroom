# Cascaded Call Model Override Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a cascaded call profile select a configured LLM alias that overrides the calls-enabled agent's normal text model.

**Architecture:** Add an optional configured-model alias to `CascadedCallProfile`, validate it against `Config.models`, and carry it through the call responder as an immutable per-turn active model.
The normal `ai_response` preparation path remains authoritative, with the explicit `active_model_name` argument to `Config.resolve_runtime_model` providing the existing highest-precedence override behavior.

**Tech Stack:** Python 3.13, Pydantic, pytest, Agno agent preparation, MatrixRTC call runtime, Markdown documentation.

---

### Task 1: Add and Validate the Cascaded Profile Model Alias

**Files:**

- Modify: `src/mindroom/config/calls.py:30-37`
- Modify: `src/mindroom/config/main.py:34,569-589`
- Test: `tests/test_matrix_rtc_call_manager.py:3252-3276`

- [ ] **Step 1: Write failing configuration tests**

Add tests that prove a cascaded profile accepts a configured model alias and that the root config rejects an unknown alias.

```python
def test_cascaded_calls_accept_optional_model_override() -> None:
    stt = SpeechServiceConfig(model="gpt-4o-transcribe", credentials_service="openai")
    tts = SpeechServiceConfig(model="tts-1", credentials_service="openai")
    config = Config(
        models={"call_fast": ModelConfig(provider="anthropic", id="claude-haiku-4-5")},
        agents={"helper": AgentConfig(display_name="Helper")},
        calls=CallsConfig(
            profiles={
                "voice": CascadedCallProfile(
                    backend="cascaded",
                    model="call_fast",
                    stt=stt,
                    tts=tts,
                ),
            },
            agents={"helper": "voice"},
        ),
    )

    resolved = config.calls.resolve_agent_config("helper")
    assert isinstance(resolved, CascadedCallProfile)
    assert resolved.model == "call_fast"


def test_calls_config_rejects_unknown_cascaded_model() -> None:
    stt = SpeechServiceConfig(model="gpt-4o-transcribe", credentials_service="openai")
    tts = SpeechServiceConfig(model="tts-1", credentials_service="openai")

    with pytest.raises(ValueError, match=r"voice -> missing"):
        Config(
            models={},
            agents={"helper": AgentConfig(display_name="Helper")},
            calls=CallsConfig(
                profiles={
                    "voice": CascadedCallProfile(
                        backend="cascaded",
                        model="missing",
                        stt=stt,
                        tts=tts,
                    ),
                },
                agents={"helper": "voice"},
            ),
        )
```

- [ ] **Step 2: Run the configuration tests and verify failure**

Run:

```bash
uv run pytest tests/test_matrix_rtc_call_manager.py::test_cascaded_calls_accept_optional_model_override tests/test_matrix_rtc_call_manager.py::test_calls_config_rejects_unknown_cascaded_model -v
```

Expected: both tests fail because `CascadedCallProfile` forbids the new `model` field.

- [ ] **Step 3: Add the profile field and root validation**

Add the optional alias to the cascaded profile.

```python
class CascadedCallProfile(BaseModel):
    """One STT, normal agent turn, and TTS call profile."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["cascaded"]
    model: str | None = Field(default=None, description="Configured LLM alias for cascaded agent turns")
    stt: SpeechServiceConfig = Field(description="Speech-to-text service")
    tts: SpeechServiceConfig = Field(description="Text-to-speech service")
```

Import `CascadedCallProfile` beside `CallsConfig` in `config/main.py`, rename the root validator to describe the full call configuration, and reject unknown explicit aliases before room-conflict validation.

```python
invalid_models = sorted(
    f"{profile_name} -> {profile.model}"
    for profile_name, profile in self.calls.profiles.items()
    if isinstance(profile, CascadedCallProfile)
    and profile.model is not None
    and profile.model not in self.models
)
if invalid_models:
    msg = "calls.profiles references unknown cascaded model(s): " + ", ".join(invalid_models)
    raise ValueError(msg)
```

- [ ] **Step 4: Run the configuration tests and verify success**

Run:

```bash
uv run pytest tests/test_matrix_rtc_call_manager.py::test_cascaded_calls_accept_optional_model_override tests/test_matrix_rtc_call_manager.py::test_calls_config_rejects_unknown_cascaded_model -v
```

Expected: both tests pass.

- [ ] **Step 5: Commit the configuration change**

```bash
git add src/mindroom/config/calls.py src/mindroom/config/main.py tests/test_matrix_rtc_call_manager.py
git commit -m "Add cascaded call model configuration"
```

### Task 2: Make Explicit Per-Turn Models Authoritative in Agent Preparation

**Files:**

- Modify: `src/mindroom/response_turn.py:193-216`
- Modify: `src/mindroom/ai.py:1107-1112`
- Test: `tests/test_history_prepare_integration.py:277-322`

- [ ] **Step 1: Write a failing preparation test**

Import `replace` from `dataclasses`, then add a test proving an explicit response-turn model beats the room model and supplies the correct context window.

```python
@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_prefers_explicit_turn_model_over_room_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", model="default")},
            defaults=DefaultsConfig(tools=[]),
            room_models={"lobby": "large"},
            models={
                "default": ModelConfig(provider="openai", id="default-model"),
                "large": ModelConfig(provider="openai", id="large-model", context_window=48_000),
                "call_fast": ModelConfig(provider="openai", id="fast-model", context_window=16_000),
            },
        ),
        runtime_paths,
    )
    monkeypatch.setattr("mindroom.matrix.state.get_room_alias_from_id", lambda *_args: "lobby")
    live_agent = _agent()
    turn = replace(
        make_turn_context("test_agent", room_id="!room:localhost"),
        active_model_name="call_fast",
    )

    with (
        patch("mindroom.ai.create_agent", return_value=live_agent) as mock_create_agent,
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch(
            "mindroom.execution_preparation.prepare_scope_history",
            new=AsyncMock(return_value=MagicMock()),
        ) as mock_prepare,
        patch(
            "mindroom.execution_preparation.finalize_history_preparation",
            return_value=PreparedHistoryState(),
        ),
    ):
        prepared = await _prepare_agent_and_prompt(
            turn,
            prompt="Current prompt",
            runtime_paths=runtime_paths,
            config=config,
        )

    assert prepared.runtime_model_name == "call_fast"
    assert mock_create_agent.call_args.kwargs["active_model_name"] == "call_fast"
    resolved_inputs = mock_prepare.await_args.kwargs["resolved_inputs"]
    assert resolved_inputs.active_model_name == "call_fast"
    assert resolved_inputs.active_context_window == 16_000
```

- [ ] **Step 2: Run the preparation test and verify failure**

Run:

```bash
uv run pytest tests/test_history_prepare_integration.py::test_prepare_agent_and_prompt_prefers_explicit_turn_model_over_room_model -v
```

Expected: the test fails because `ResponseTurnContext` has no `active_model_name` field.

- [ ] **Step 3: Add the immutable turn field and use it during resolution**

Add a defaulted field to `ResponseTurnContext`.

```python
active_model_name: str | None = None
```

Pass the field into existing model resolution.

```python
runtime_model = config.resolve_runtime_model(
    entity_name=agent_name,
    active_model_name=ctx.active_model_name,
    room_id=ctx.room_id,
    thread_id=ctx.thread_id,
    runtime_paths=runtime_paths,
)
```

- [ ] **Step 4: Run the preparation test and verify success**

Run:

```bash
uv run pytest tests/test_history_prepare_integration.py::test_prepare_agent_and_prompt_prefers_explicit_turn_model_over_room_model -v
```

Expected: the test passes and reports `call_fast` as the prepared runtime model.

- [ ] **Step 5: Commit the execution-context change**

```bash
git add src/mindroom/response_turn.py src/mindroom/ai.py tests/test_history_prepare_integration.py
git commit -m "Support explicit response-turn models"
```

### Task 3: Carry the Cascaded Profile Model Through the Call Runtime

**Files:**

- Modify: `src/mindroom/matrix_rtc/call_manager.py:816-836`
- Modify: `src/mindroom/matrix_rtc/call_tools.py:195-256,347-392`
- Test: `tests/test_matrix_rtc_call_manager.py:275-311,611-656`
- Test: `tests/test_matrix_rtc_call_tools.py:367-484`

- [ ] **Step 1: Write failing call-runtime assertions**

Extend `_cascaded_config` with a `call_model` parameter, add that alias to the test models when supplied, and set it on the cascaded profile.

```python
def _cascaded_config(*, local: bool = False, call_model: str | None = None) -> Config:
    config = _config()
    if call_model is not None:
        config.models[call_model] = ModelConfig(provider="openai", id="fast-call-model")
    if local:
        config.models["default"] = ModelConfig(
            provider="openai",
            id="local-chat-model",
            extra_kwargs={
                "api_key": LOCAL_OPENAI_API_KEY_DEFAULT,
                "base_url": "http://127.0.0.1:9292/v1",
            },
        )
        config.memory = MemoryConfig(backend="none")
    config.calls = CallsConfig(
        enabled=True,
        profiles={
            "cascaded": CascadedCallProfile(
                backend="cascaded",
                model=call_model,
                stt=SpeechServiceConfig(
                    provider="openai_compatible" if local else "openai",
                    model="whisper-large-v3" if local else "gpt-4o-transcribe",
                    api_key=None if local else "stt-key",
                    host="http://127.0.0.1:9000" if local else None,
                    extra_kwargs={"language": "en"},
                ),
                tts=SpeechServiceConfig(
                    provider="openai_compatible",
                    model="tts-1",
                    api_key=None if local else "tts-key",
                    host="http://127.0.0.1:9001",
                    extra_kwargs={"voice": "ash"},
                ),
            ),
        },
        agents={"helper": "cascaded"},
        livekit_service_url=SERVICE_URL,
    )
    return config
```

In `test_manager_selects_cascaded_backend_with_independent_speech_services`, construct `_cascaded_config(call_model="call_fast")` and assert the fake tool builder receives `active_model_name == "call_fast"`.

In `test_cascaded_responder_uses_normal_agent_turn_and_filters_unsafe_functions`, pass `active_model_name="call_fast"` to `build_call_tools` and assert `turn.active_model_name == "call_fast"` beside the existing turn identity assertions.

- [ ] **Step 2: Run the two call-runtime tests and verify failure**

Run:

```bash
uv run pytest tests/test_matrix_rtc_call_manager.py::test_manager_selects_cascaded_backend_with_independent_speech_services tests/test_matrix_rtc_call_tools.py::test_cascaded_responder_uses_normal_agent_turn_and_filters_unsafe_functions -v
```

Expected: tests fail because `build_call_tools` does not accept or propagate `active_model_name`.

- [ ] **Step 3: Pass the selected profile alias into call tooling**

Update `CallManager._build_tooling` to pass the alias only for cascaded calls.

```python
return await build_call_tools(
    agent_name=self._agent_name,
    config=self._config,
    runtime_paths=self._runtime_paths,
    tool_support=self._tool_support,
    room_id=room_id,
    requester_id=requester_id,
    session_id=session_id,
    enable_responder=cascaded,
    voice_instructions=_VOICE_STYLE_ADDENDUM if cascaded else None,
    active_model_name=self._call_config.model if cascaded else None,
)
```

Add `active_model_name: str | None = None` to `build_call_tools`, bind it into the `_run_call_agent` partial, accept it in `_run_call_agent`, and set it on the call's response-turn context.

```python
turn = ResponseTurnContext(
    entity_label=agent_name,
    session_id=session_id,
    run_id=None,
    correlation_id=uuid4().hex,
    reply_to_event_id=None,
    room_id=room_id,
    thread_id=None,
    requester_id=requester_id,
    matrix_run_metadata=None,
    active_model_name=active_model_name,
    system_enrichment_items=enrichment_items,
)
```

- [ ] **Step 4: Run call-runtime and fallback regression tests**

Run:

```bash
uv run pytest tests/test_matrix_rtc_call_manager.py tests/test_matrix_rtc_call_tools.py tests/test_history_prepare_integration.py -q
```

Expected: all tests pass, including existing cascaded profiles that omit `model` and all realtime call tests.

- [ ] **Step 5: Commit the call-runtime plumbing**

```bash
git add src/mindroom/matrix_rtc/call_manager.py src/mindroom/matrix_rtc/call_tools.py tests/test_matrix_rtc_call_manager.py tests/test_matrix_rtc_call_tools.py
git commit -m "Use configured models for cascaded calls"
```

### Task 4: Document the Override and Regenerate Bundled References

**Files:**

- Modify: `docs/voice-calls.md:21-22,48-50,89-116`
- Modify: `docs/configuration/index.md:527-545`
- Regenerate: `skills/mindroom-docs/references/page__voice-calls__index.md`
- Regenerate: `skills/mindroom-docs/references/page__configuration__index.md`
- Regenerate: `skills/mindroom-docs/references/llms-full.txt`

- [ ] **Step 1: Update voice-call behavior and configuration examples**

State that cascaded calls keep normal model resolution when `model` is omitted and use the named top-level model when it is present.

Add a focused example showing a large text model and fast call model.

```yaml
models:
  chat:
    provider: anthropic
    id: claude-opus-4-8
  call-fast:
    provider: anthropic
    id: claude-haiku-4-5

agents:
  assistant:
    model: chat

calls:
  enabled: true
  profiles:
    fast-cascaded:
      backend: cascaded
      model: call-fast
      stt:
        provider: openai
        model: gpt-4o-transcribe
        credentials_service: openai-voice
      tts:
        provider: openai
        model: tts-1
        credentials_service: openai-voice
  agents:
    assistant: fast-cascaded
```

Add `model: call-fast` with an optional-alias comment to the cascaded profile in the full configuration reference.

- [ ] **Step 2: Regenerate the bundled documentation skill references**

Run:

```bash
.venv/bin/python .github/scripts/generate_skill_references.py
```

Expected: the voice-call and configuration page references plus `llms-full.txt` reflect the source documentation.

- [ ] **Step 3: Check Markdown and generated-reference consistency**

Run:

```bash
uv run pre-commit run trailing-whitespace --files docs/voice-calls.md docs/configuration/index.md
uv run pre-commit run generate-skill-references --all-files
```

Expected: both hooks pass without modifying files on the second run.

- [ ] **Step 4: Commit documentation**

```bash
git add docs/voice-calls.md docs/configuration/index.md skills/mindroom-docs/references/page__voice-calls__index.md skills/mindroom-docs/references/page__configuration__index.md skills/mindroom-docs/references/llms-full.txt
git commit -m "Document cascaded call model overrides"
```

### Task 5: Verify and Publish the Pull Request

**Files:**

- Verify all changed files.
- Do not modify unrelated files.

- [ ] **Step 1: Synchronize the complete development environment**

Run:

```bash
uv sync --all-extras
```

Expected: all runtime, optional-tool, call, and development dependencies are installed.

- [ ] **Step 2: Run the full Python test suite**

Run:

```bash
uv run pytest
```

Expected: the full suite passes.

- [ ] **Step 3: Run architectural and pre-commit verification**

Run:

```bash
uv run tach check --dependencies --interfaces
uv run pre-commit run --all-files
```

Expected: Tach and every pre-commit hook pass.

- [ ] **Step 4: Inspect the final branch diff**

Run:

```bash
git status --short --branch
git --no-pager diff --check origin/main...HEAD
git --no-pager diff --stat origin/main...HEAD
git --no-pager log --oneline origin/main..HEAD
```

Expected: the branch is clean, the diff contains only the design, plan, configuration, runtime, tests, and documentation for this feature, and every commit is intentional.

- [ ] **Step 5: Push and open a ready-for-review pull request**

Push `call-model-override`, open a non-draft PR against `main`, and summarize the configuration surface, precedence, fallback behavior, and verification evidence.

- [ ] **Step 6: Monitor CI and AI review**

Wait for GitHub checks and any configured AI review to finish.
Verify every finding against the code, address valid in-scope findings with follow-up commits, push them, and repeat until required checks and review are clear.
