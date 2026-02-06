# Unified Tool Call Blocks

## Problem

Tool calls currently produce two separate collapsible blocks each — a `<tool>` block (call) and a `<validation>` block (result). Five tool calls = 10 collapsible blocks. The validation blocks show no useful info beyond "completed in X seconds" — the actual tool output isn't shown.

## Goal

One collapsible block per batch of consecutive tool calls. Each entry shows the call and its actual result. During streaming, new entries appear as tools start/complete.

### Collapsed (default)
```
🔧 5 tool calls (show details)
```

### Expanded
```
🔧 5 tool calls (hide details)
  run_shell_command(args=["pwd"]) → /app
  run_shell_command(args=["uname", "-a"])
    Linux server 6.12.33 x86_64 GNU/Linux
  run_shell_command(args=["python3", "-m", "pip", "list"])
    pip 24.0
    setuptools 69.5.1
    wheel 0.43.0
    ... (12 more lines)
  read_file(file_name=pyproject.toml)
    [name]
    version = "0.3.1"
    ... (truncated, 2.3KB)
  list_files(kwargs={}) → pyproject.toml, uv.lock, .venv/, src/
```

Short single-line results use `→` inline. Multiline results go below, indented.

### Streaming (in-progress tool)
```
🔧 4 tool calls (hide details)
  run_shell_command(args=["pwd"]) → /app
  run_shell_command(args=["uname", "-a"]) → Linux 6.12.33
  run_shell_command(args=["python3", "--version"]) → Python 3.12.12
  run_shell_command(args=["make build"]) ⏳
```

The count in the header increases live as tools start.

---

## Changes

### 1. Backend — `tool_events.py` (single source of truth)

All tool formatting logic lives here. No tool formatting in streaming.py, teams.py, or ai.py.

#### Drop `<validation>` tag

Only emit `<tool>` blocks. Three functions handle all cases:

#### `format_tool_started` — unchanged

Emits pending block during streaming:
```python
return f"\n\n<tool>{safe_display}</tool>\n", trace
```

#### `format_tool_combined` — new

For non-streaming (ai.py) and for replacing pending blocks. Produces a single `<tool>` block with call + result:

```python
def format_tool_combined(
    tool_name: str, tool_args: dict[str, object], result: object | None,
) -> tuple[str, ToolTraceEntry]:
    """Format a complete tool call (call + result) as a single <tool> block."""
    # Build call display: tool_name(args)
    args_preview, truncated = _format_tool_args(tool_args) if tool_args else ("", False)
    call_display = f"{tool_name}({args_preview})" if tool_args else f"{tool_name}()"

    # Build result display
    if result is None or result == "":
        result_display = ""
    else:
        result_text = _to_compact_text(result)
        result_display, result_truncated = _truncate(result_text, MAX_TOOL_RESULT_DISPLAY_CHARS)
        truncated = truncated or result_truncated

    safe_call = escape(_neutralize_mentions(call_display))
    if result_display:
        safe_result = escape(_neutralize_mentions(result_display))
        block = f"\n\n<tool>{safe_call}\n{safe_result}</tool>\n"
    else:
        block = f"\n\n<tool>{safe_call}</tool>\n"

    trace = ToolTraceEntry(
        type="tool_call_completed", tool_name=tool_name,
        args_preview=args_preview or None,
        result_preview=result_display or None,
        truncated=truncated,
    )
    return block, trace
```

#### `complete_pending_tool_block` — new

Replaces a pending `<tool>call</tool>` in accumulated text with `<tool>call\nresult</tool>`. Used by streaming.py and teams.py:

```python
def complete_pending_tool_block(
    accumulated_text: str, tool_name: str, result: object | None,
) -> tuple[str, ToolTraceEntry]:
    """Find the last pending <tool> block for tool_name and inject the result.

    Returns (updated_text, trace_entry).
    If no pending block is found, appends a new combined block instead.
    """
    result_display = ""
    truncated = False
    if result is not None and result != "":
        result_text = _to_compact_text(result)
        result_display, truncated = _truncate(result_text, MAX_TOOL_RESULT_DISPLAY_CHARS)

    safe_result = escape(_neutralize_mentions(result_display)) if result_display else ""

    # Search backwards for the last <tool>...tool_name(...</tool> without a newline
    # (a newline inside means it already has a result)
    pattern = re.compile(
        rf"<tool>({re.escape(escape(_neutralize_mentions(tool_name)))}[^<]*)</tool>",
    )
    matches = list(pattern.finditer(accumulated_text))

    updated = accumulated_text
    for match in reversed(matches):
        inner = match.group(1)
        # Pending blocks have no newline (just the call). Completed blocks have \n.
        if "\n" not in inner:
            if safe_result:
                replacement = f"<tool>{inner}\n{safe_result}</tool>"
            else:
                replacement = match.group(0)  # No result, keep as-is
            updated = updated[: match.start()] + replacement + updated[match.end() :]
            break
    else:
        # No pending block found — append a standalone completed block
        if safe_result:
            safe_name = escape(_neutralize_mentions(tool_name))
            updated += f"<tool>{safe_name}\n{safe_result}</tool>\n"

    trace = ToolTraceEntry(
        type="tool_call_completed", tool_name=tool_name,
        result_preview=result_display or None, truncated=truncated,
    )
    return updated, trace
```

#### `format_tool_completed` — remove

No longer needed. Replaced by `complete_pending_tool_block` (streaming) and `format_tool_combined` (non-streaming).

#### Result truncation

Add `MAX_TOOL_RESULT_DISPLAY_CHARS = 500` for in-message results (separate from the 4000-char metadata limit). When truncated, append `… (truncated, {original_size})`.

### 2. Backend — `ai.py`

#### `_extract_response_content` (non-streaming path)

Use `format_tool_combined` for each tool — one block per tool with call + result:

```python
# Current:
started, _ = format_tool_started(tool_name, tool_args)
completed, _ = format_tool_completed(tool_name, tool.result)
tool_sections.append(started.strip())
tool_sections.append(completed.strip())

# New:
combined, _ = format_tool_combined(tool_name, tool_args, tool.result)
tool_sections.append(combined.strip())
```

#### Streaming path (lines 386-408)

No changes needed. It yields `ToolCallStartedEvent` and `ToolCallCompletedEvent` to the stream consumer (`streaming.py`), which handles formatting.

### 3. Backend — `streaming.py`

Replace the `ToolCallCompletedEvent` handler to use `complete_pending_tool_block`:

```python
elif isinstance(chunk, ToolCallCompletedEvent):
    tool = getattr(chunk, "tool", None)
    tool_name = getattr(tool, "tool_name", None) or "tool"
    result = getattr(chunk, "content", None) or getattr(tool, "result", None)

    streaming.accumulated_text, trace_entry = complete_pending_tool_block(
        streaming.accumulated_text, tool_name, result,
    )
    if trace_entry:
        streaming.tool_trace.append(trace_entry)

    # Force an update to push the completed tool to the client
    await streaming._send_or_edit_message(client)
    streaming.last_update = time.time()
    continue  # Skip the update_content path since we modified accumulated_text directly
```

The `ToolCallStartedEvent` handler stays as-is — it appends a pending `<tool>` block via `format_tool_started_event`.

### 4. Backend — `teams.py`

Same pattern. Replace tool completion handling to use `complete_pending_tool_block`:

```python
# Agent tool call completed
elif isinstance(event, AgentToolCallCompletedEvent):
    agent_name = event.agent_name
    tool = getattr(event, "tool", None)
    tool_name = getattr(tool, "tool_name", None) or "tool"
    result = getattr(event, "content", None) or getattr(tool, "result", None)

    if agent_name:
        if agent_name not in per_member:
            per_member[agent_name] = ""
        per_member[agent_name], trace_entry = complete_pending_tool_block(
            per_member[agent_name], tool_name, result,
        )
    if trace_entry:
        tool_trace.append(trace_entry)
```

Same for `TeamToolCallCompletedEvent` operating on `consensus` text.

### 5. Frontend — `collapsible.tsx`

#### Merge consecutive `<tool>` blocks

After the regex matches individual `<tool>` blocks into `parts[]`, post-process to merge consecutive tool blocks:

```typescript
function mergeConsecutiveToolBlocks(parts: (string | JSX.Element)[]): (string | JSX.Element)[] {
    const merged: (string | JSX.Element)[] = [];
    let toolGroup: JSX.Element[] = [];

    const flushGroup = () => {
        if (toolGroup.length === 0) return;
        if (toolGroup.length === 1) {
            merged.push(toolGroup[0]);
        } else {
            merged.push(
                <CollapsibleBlock config={{...toolConfig, label: `${toolGroup.length} tool calls`}}>
                    {toolGroup.map(block => block.props.children)}
                </CollapsibleBlock>
            );
        }
        toolGroup = [];
    };

    for (const part of parts) {
        if (isToolBlock(part)) {
            toolGroup.push(part);
        } else {
            flushGroup();
            merged.push(part);
        }
    }
    flushGroup();
    return merged;
}
```

#### Render each entry with call + result

Parse each `<tool>` block's content (first line = call, remaining = result):
- No `\n` in content → pending call, show with ⏳ indicator
- Has `\n`, result is single-line and < ~80 chars → show inline: `call → result`
- Has `\n`, result is multiline or long → show result below, indented

#### Update legacy markdown normalization

Update `normalizeMarkdownCollapsibleBlocks` to combine consecutive call+result pairs:

```
🔧 **Tool Call:** `save_file(file_name=a.py)`
✅ **`save_file` result:**
ok
```

Becomes:
```
<tool>save_file(file_name=a.py)\nok</tool>
```

Instead of the current separate `<tool>` + `<validation>` tags.

### 6. Frontend — `CollapsibleBlock.tsx`

Add optional `count` prop for dynamic label:

```typescript
interface IProps {
    children: ReactNode;
    config: CollapsibleBlockConfig;
    count?: number;
}
```

Label renders as: `count > 1` → "5 tool calls", otherwise uses `config.label`.

### 7. Frontend — `collapsibleBlocks.ts`

Keep `validation` config for backward compatibility with existing messages. New messages won't emit it.

### 8. Tests

#### Backend — `tests/test_tool_events.py`
- Remove `format_tool_completed` tests
- Add `format_tool_combined` tests (call+result in single `<tool>` block)
- Add `complete_pending_tool_block` tests (finds and replaces pending block, fallback append)
- Test display truncation at `MAX_TOOL_RESULT_DISPLAY_CHARS`

#### Backend — `tests/test_mentions.py`
- Update test that references `format_tool_completed` / `<validation>` if any

#### Frontend — `test/unit-tests/renderer/collapsible-test.tsx`
- Add test: consecutive `<tool>` blocks merge into one collapsible with count
- Add test: inline result display (short, single-line)
- Add test: multiline result display (below, indented)
- Add test: pending tool block (no result) shows ⏳
- Update legacy normalization tests for combined format
- Keep a test for standalone `<validation>` blocks (backward compat)

---

## Backward Compatibility

- **New messages**: Combined `<tool>` blocks only (no `<validation>`)
- **Old messages with `<validation>`**: Element keeps rendering them (config stays in `collapsibleBlocks.ts`)
- **Old messages with legacy markdown**: Normalizer converts to combined `<tool>` format
- **Old messages with separate `<tool>` + `<validation>`**: Element renders both; the merge logic only groups consecutive same-tag blocks, so they appear as separate collapsibles (acceptable degradation)

---

## Implementation Order

1. **`tool_events.py`**: Add `format_tool_combined`, `complete_pending_tool_block`, `MAX_TOOL_RESULT_DISPLAY_CHARS`. Remove `format_tool_completed`.
2. **`ai.py`**: Update `_extract_response_content` to use `format_tool_combined`
3. **`streaming.py`**: Use `complete_pending_tool_block` for `ToolCallCompletedEvent`
4. **`teams.py`**: Use `complete_pending_tool_block` for agent and team tool completions
5. **Backend tests**: Update all test assertions
6. **Frontend `collapsible.tsx`**: Add `mergeConsecutiveToolBlocks`, update legacy normalizer
7. **Frontend `CollapsibleBlock.tsx`**: Add `count` prop
8. **Frontend tests**: Update and add tests
