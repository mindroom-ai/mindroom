# Code Debate: PR #211 — feat: add OpenClaw-style browser tool parity

## Opening

### PR Summary

PR #211 adds a first-class `browser` tool (`src/mindroom/custom_tools/browser.py`, 1010 lines) that provides OpenClaw-style browser control via Playwright, exposes it through `openclaw_compat`, and registers tool metadata. The changes span 8 files with +1169/-1 lines.

### Position: One blocker, otherwise well-executed

**The implementation quality is high.** The code is clean, well-structured, follows existing project patterns, and all 1655 tests pass. Pre-commit hooks pass. However, I identified one blocker:

### Blocker: No tests for `BrowserTools` class

`src/mindroom/custom_tools/browser.py` is 1010 lines of new production code with **zero dedicated tests**. The only test coverage is indirect — `tests/test_openclaw_compat_contract.py:125-128` mocks out the browser tool entirely and only tests the proxy layer:

```python
browser_entrypoint = AsyncMock(return_value='{"status":"ok","action":"status"}')
tool._browser_tool = SimpleNamespace(
    async_functions={"browser": SimpleNamespace(entrypoint=browser_entrypoint)},
    functions={},
)
```

This verifies the openclaw_compat delegation works, but tests **nothing** about the actual browser tool logic. The following are all completely untested:

1. **Action routing** — 16 actions in `BROWSER_ACTIONS` set, dispatched via the giant if-chain in `browser()` (lines 273-402)
2. **Profile lifecycle** — `_ensure_profile`, `_stop_profile` (lines 919-943)
3. **Tab management** — `_open_tab`, `_close_tab`, `_focus_tab`, `_resolve_tab` (lines 478-963)
4. **Snapshot logic** — `_snapshot`, `_SNAPSHOT_JS` evaluation, ref tracking, format selection (lines 652-755)
5. **Act sub-routing** — 11 act kinds (click, type, press, hover, drag, select, fill, resize, wait, evaluate, close) in `_act` (lines 765-905)
6. **Input validation** — `_validate_target`, required-field checks for each action (lines 404-415)
7. **Utility functions** — `_clean_str`, `_resolve_selector`, `_resolve_max_chars` (lines 174-179, 996-1000, 757-763)
8. **Dialog/console handlers** — `_handle_dialog`, `_record_console` (lines 974-994)

The project's `CLAUDE.md` states: *"NEVER claim a task is done without passing all pytest tests"* and the PR review checklist asks whether tests *"cover the changes adequately."* A `tests/test_browser_tool.py` is needed.

**Importantly, these tests don't require a real browser.** All Playwright objects (`Page`, `BrowserContext`, `Browser`, `Playwright`) can be mocked with `AsyncMock`. The tool is designed with clean internal boundaries that make unit testing straightforward.

Recommended minimum test coverage:
- `_clean_str` edge cases (empty string, whitespace-only, non-string)
- `_validate_target` (valid/invalid targets, node constraints)
- `_resolve_selector` (ref lookup hit, ref miss → passthrough, None)
- `_resolve_max_chars` (explicit value, efficient mode, default)
- Action routing — unknown action raises `ValueError`
- At least one representative action (e.g., `open` with mocked Page) verifying the full dispatch path
- `_act` kind routing — unknown kind raises `ValueError`, at least one act (e.g., `click`) with mocked locator

### Non-issues (things I reviewed and found acceptable)

1. **`close()` method** (lines 197-204) — Uses `asyncio.run` fallback for non-async cleanup. Standard pattern for process shutdown.
2. **`docs_url` pointing to OpenClaw repo** — Verified the URL is live and contains relevant documentation.
3. **`type` parameter shadowing builtin** — Suppressed with `# noqa: A002`, justified for API compatibility.
4. **`except Exception` in openclaw_compat** (line 1250) — At the tool-agent boundary, catching broadly and returning structured error is the established pattern (matches `_run_shell` at line 1297).
5. **`None`-filtering in `call_kwargs`** (line 1244) — Correctly uses `is not None`, preserving `False` and `0` values.
6. **Side-effect in `__init__`** (`_output_dir.mkdir`) — Consistent with other tools in the codebase.
7. **Large method signature** — Necessary for OpenClaw API surface compatibility; properly suppressed with `# noqa: PLR0913`.
8. **camelCase parameter names** in `BrowserTools.browser()` — Required for OpenClaw API compatibility; `# noqa: N803` is appropriate.

### Verdict

❌ **CHANGES REQUIRED** — Add tests for `BrowserTools`. Everything else is solid.

*— Agent A (Claude Code), 2026-02-22T05:32:49Z*

---


## Response 1

### Point-by-point reply

1. **Agreed** — The test gap is a blocker.
   - `src/mindroom/custom_tools/browser.py` adds a large new behavior surface without direct unit tests.
   - Current coverage in `tests/test_openclaw_compat_contract.py:125` only mocks the browser entrypoint, so core behavior inside `BrowserTools` is unverified.
   - I agree merge should be gated on adding focused `BrowserTools` tests.

2. **Partially agreed** — Your minimum test list is good, but I would extend it slightly.
   - In addition to the cases you listed, I would add at least one parity-focused target test and one explicit compat-forwarding test that includes `target`/`node` parameters.

3. **Agreed** — Your listed non-issues are correctly non-blocking for this PR.

### Independent findings

- **Finding B1 — same as other agent**
  - Missing direct tests for `BrowserTools` is a real coverage gap and should be addressed before merge.

- **Finding B2 — new finding**
  - **Parity gap in target support:** OpenClaw advertises `target` as `sandbox|host|node` (`../openclaw/src/agents/tools/browser-tool.ts:239`, `../openclaw/src/agents/tools/browser-tool.ts:248`, `../openclaw/docs/tools/browser.md:557`).
  - MindRoom currently rejects `sandbox`/`node` in `_validate_target` (`src/mindroom/custom_tools/browser.py:412`).
  - `openclaw_compat.browser` forwards `target`/`node` through (`src/mindroom/custom_tools/openclaw_compat.py:1216`), so valid OpenClaw calls fail in MindRoom.
  - **Severity:** blocker for the stated browser-tool parity objective.

*— Agent B (Codex), 2026-02-22T05:36:41Z*

---

## Follow-up 1

### Agreed points (no further discussion)

- **Test gap is a blocker** — both agents agree. Merge is gated on `tests/test_browser_tool.py`.
- **Extended test list** — agreed, adding parity-focused target tests and compat-forwarding tests with `target`/`node` params is good.

### Addressing Finding B2 — target parity gap

**Disagreed** — This is not a blocker for this PR. Here's why:

1. **`sandbox` and `node` targets require infrastructure MindRoom doesn't have.** `sandbox` requires OpenClaw's container sandbox runtime. `node` requires OpenClaw's Gateway/node infrastructure. Neither exists in MindRoom. Implementing them is a separate feature, not part of browser tool parity.

2. **The error handling is explicit and informative.** `_validate_target` at `src/mindroom/custom_tools/browser.py:412-413` raises with the message `"MindRoom browser tool currently supports host target only."` — this clearly tells the agent why the call failed and what's supported.

3. **This is consistent with how other unsupported OpenClaw features are handled.** The `gateway`, `nodes`, and `canvas` tools all return `"not_configured"` status with explanatory messages (`src/mindroom/custom_tools/openclaw_compat.py:1103-1144`). Rejecting unsupported targets follows the same pattern — graceful degradation with clear feedback.

4. **The PR title says "OpenClaw-style browser tool parity", not "full OpenClaw feature parity".** The `.claude/TASK.md` says *"Whatever the name is that OpenClaw uses, it should have the same name and implementation"* — the implementation covers the same API surface (same action names, same parameter shapes) for the `host` target, which is the only target MindRoom can actually run.

5. **The compat layer handles this correctly.** When `openclaw_compat.browser` forwards `target="sandbox"`, the underlying `BrowserTools._validate_target` raises `ValueError`, which is caught by `except Exception as exc` at line 1250 and returned as a structured error payload. The agent gets `{"status": "error", "tool": "browser", "action": "...", "message": "MindRoom browser tool currently supports host target only."}` — a clean, actionable error.

**Proposal:** If Agent B considers it important to document this limitation, the test suite could include a test verifying that `target="sandbox"` and `target="node"` produce clear error messages. This would be part of the `_validate_target` test coverage I already recommended. But it's not a code change blocker.

### Summary of blockers

One blocker remains: **add `tests/test_browser_tool.py`**. Finding B2 is a documentation/test concern, not a code-change blocker.

*— Agent A (Claude Code), 2026-02-22T05:37:13Z*

---

## Response 2

### Point-by-point reply

1. **Agreed** — On scope: `sandbox`/`node` execution paths depend on OpenClaw infrastructure that MindRoom does not currently have.

2. **Partially agreed** — I accept your argument that this is not a merge blocker for this PR, but I still think the parity boundary should be made explicit in tests and messaging.
   - Concretely, I suggest adding `_validate_target` tests that assert clear errors for `target="sandbox"` and `target="node"`.
   - This keeps expectations aligned while preserving the current host-only implementation.

3. **Agreed** — We are converged on the main blocker: add direct tests for `BrowserTools`.

### Independent findings

- **Finding B1 — same as other agent**
  - Missing direct tests for `BrowserTools` remains the single blocking issue.

- **Finding B2 — not reproduced / disagreed**
  - I no longer classify host-only target support as a blocker for this PR.
  - I classify it as a boundary/clarity issue to cover via tests and explicit error assertions.

*— Agent B (Codex), 2026-02-22T05:38:01Z*

---

## CONSENSUS

**Verdict: ❌ CHANGES REQUIRED — one blocker**

### Blocker

**Add `tests/test_browser_tool.py`** with direct unit tests for `BrowserTools` (`src/mindroom/custom_tools/browser.py`). Both agents agree that 1010 lines of new production code without dedicated tests is a merge blocker.

**Required minimum test coverage:**
1. `_clean_str` — empty string, whitespace-only, non-string input
2. `_validate_target` — valid `host` target, rejected `sandbox`/`node` targets with clear error messages, invalid targets
3. `_resolve_selector` — ref lookup hit, ref miss (passthrough), `None` input
4. `_resolve_max_chars` — explicit value, efficient mode, default behavior
5. Action routing — unknown action raises `ValueError`; at least one representative action (e.g., `open`) with mocked Playwright objects verifying the full dispatch path
6. `_act` kind routing — unknown kind raises `ValueError`; at least one act kind (e.g., `click`) with mocked locator
7. Compat-forwarding — at least one test verifying `openclaw_compat.browser` correctly forwards `target`/`node` parameters

All tests can use `AsyncMock`/`MagicMock` for Playwright objects — no real browser required.

### Not a blocker

- **Host-only target support** — `sandbox`/`node` targets require OpenClaw infrastructure MindRoom doesn't have. The current explicit rejection with `"MindRoom browser tool currently supports host target only."` is correct and consistent with how `gateway`/`nodes`/`canvas` handle unsupported features. Boundary behavior should be covered by `_validate_target` tests (item 2 above), not by code changes.

### No other issues found

Both agents reviewed the implementation and found no other problems. The code is clean, follows existing patterns, all 1655 tests pass, and pre-commit hooks pass.

*— Agent A (Claude Code) and Agent B (Codex), 2026-02-22T05:38:12Z*
