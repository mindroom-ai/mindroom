# Frontend Implementation Prompt: MindRoom Long Messages v2 (Cinny Fork)

You are implementing **MindRoom large-message v2 hydration** in our Cinny fork.

## Repositories / Branches / Commits (authoritative references)

Backend implementation reference:
- Repo path: `/home/basnijholt/nix/src/mindroom`
- Branch: `tool-trace-v2-client`
- Current head: `a3906e6f`
- Relevant commits:
  - `b2c33e80` (`store full oversized matrix content as json sidecar`)
  - `c09ba215`, `db050361`, `a89af883`, `d213045e` (tool-ref v2 + strictness + hidden spacing)

Cinny fork target:
- Repo path: `/home/basnijholt/nix/src/mindroom-cinny`
- Branch: `dev`
- Current head: `f690867e`
- Tool-ref v2 frontend commit already present: `0482f830`

Do **not** assume upstream Cinny behavior; follow this fork’s current files.

---

## Objective

For oversized Matrix events, backend now sends:
- a compact preview event
- plus a sidecar attachment containing the **full original Matrix `content` JSON**

You must make Cinny render from that full sidecar content when available, while keeping preview fallback on error.

This must restore full fidelity for long messages, including:
- `formatted_body`
- `io.mindroom.tool_trace` (needed for tool-ref v2 rendering)
- mentions and other metadata embedded in original content

---

## Backend v2 Contract (exact)

Oversized preview event content contains:
- `msgtype: "m.file"`
- `body`: truncated preview
- `filename: "message-content.json"`
- `info.mimetype: "application/json"`
- `url` (unencrypted) or `file.url` + decryption fields (encrypted)
- `io.mindroom.long_text`:
  - `version: 2`
  - `encoding: "matrix_event_content_json"`
  - `original_event_size`
  - `preview_size`
  - `is_complete_content: true`

Sidecar payload bytes are JSON-serialized **full original content object** (not just plain text).

Backend references:
- `/home/basnijholt/nix/src/mindroom/src/mindroom/matrix/large_messages.py`
- `/home/basnijholt/nix/src/mindroom/src/mindroom/matrix/message_content.py`

---

## Current Cinny State (what is wrong now)

Current long-text code in this fork assumes sidecar is plain text:
- `/home/basnijholt/nix/src/mindroom-cinny/src/app/components/message/mindroomLongText.ts`
- `/home/basnijholt/nix/src/mindroom-cinny/src/app/components/message/MindroomLongTextText.tsx`

Current behavior:
- downloads sidecar as text
- drops preview `formatted_body` unless text looks like `<think|debug|...>` blocks
- does **not** parse JSON sidecar
- does **not** hydrate `io.mindroom.tool_trace` from sidecar

Also note:
- Tool-ref renderer depends on `withMindroomToolTraceMarkerParserOptions(..., content)` using the `content` object passed in.
- In long-text mode today, parser options are created from preview content in `RenderMessageContent.tsx`, so sidecar tool trace cannot be used.

Files:
- `/home/basnijholt/nix/src/mindroom-cinny/src/app/components/RenderMessageContent.tsx`
- `/home/basnijholt/nix/src/mindroom-cinny/src/app/plugins/react-custom-html-parser.tsx`

---

## Required Changes

### 1) Replace plain-text long-text assumption with v2 JSON hydration

Implement helper(s) in:
- `/home/basnijholt/nix/src/mindroom-cinny/src/app/components/message/mindroomLongText.ts`

Must support:
- Detect v2 long-text metadata:
  - `content["io.mindroom.long_text"]?.version === 2`
  - `content["io.mindroom.long_text"]?.encoding === "matrix_event_content_json"`
- Resolve sidecar MXC from preview event:
  - `content.url` (unencrypted) OR `content.file?.url` (encrypted)
- Fetch sidecar bytes and decode JSON to object
- Return hydrated content object (full original `content`)
- On any failure, fallback to original preview content

Keep existing behavior for non-v2 cases if needed by this fork.

### 2) Add encrypted sidecar support

In `MindroomLongTextText.tsx`, use existing utilities:
- `downloadMedia`, `downloadEncryptedMedia`, `decryptFile`, `mxcUrlToHttp`
- encrypted sidecar path must use `content.file` decryption info

Do not add ad-hoc crypto; reuse existing attachment decrypt helpers used by file/image/video components.

### 3) Ensure tool-ref parser sees hydrated content

Current parser options are created once from preview `content` in `RenderMessageContent.tsx`.
This breaks tool-ref metadata lookup after hydration.

Fix by ensuring `withMindroomToolTraceMarkerParserOptions(...)` receives the **resolved/hydrated** content when rendering long-text messages.

Acceptable approaches:
- Build parser options inside `MindroomLongTextText` using hydrated content, or
- Pass a parser-options factory into `MindroomLongTextText` and call it with resolved content.

Do not keep parser options bound to stale preview content for long-text path.

### 4) Add a small cache

Cache hydrated sidecar by MXC URI to avoid refetching/parsing on rerender:
- in-memory map is enough
- key: MXC URI
- value: parsed hydrated content object

On cache hit, skip network/decrypt/JSON parse.

### 5) Edit events

Handle events where large-message fallback is inside `m.new_content`.

Depending on this fork’s message normalization, the long-text component may receive either:
- top-level content already normalized, or
- raw wrapper with nested `m.new_content`.

Hydration logic must work for the actual content object being rendered and preserve existing edit rendering behavior.

---

## Concrete File Targets (Cinny fork)

Primary:
- `/home/basnijholt/nix/src/mindroom-cinny/src/app/components/message/mindroomLongText.ts`
- `/home/basnijholt/nix/src/mindroom-cinny/src/app/components/message/MindroomLongTextText.tsx`
- `/home/basnijholt/nix/src/mindroom-cinny/src/app/components/RenderMessageContent.tsx`

Tests:
- `/home/basnijholt/nix/src/mindroom-cinny/src/app/components/message/mindroomLongText.test.ts`
- `/home/basnijholt/nix/src/mindroom-cinny/src/app/components/message/mindroomPipeline.test.ts`
- add/update parser integration test where needed:
  - `/home/basnijholt/nix/src/mindroom-cinny/src/app/plugins/react-custom-html-parser.test.ts`

---

## Test Requirements (must pass)

Add/adjust tests for:

1. v2 unencrypted hydration
- preview content has long_text v2 + `url`
- downloaded JSON parsed
- resolved content includes sidecar `formatted_body` and sidecar `io.mindroom.tool_trace`

2. v2 encrypted hydration
- preview content has long_text v2 + `file.url` + decryption fields
- decrypt path invoked
- resolved content parsed from decrypted JSON

3. parser integration with hydrated tool trace
- hydrated content has marker `🔧 <code>tool</code> [1]`
- hydrated content has `io.mindroom.tool_trace.version=2`
- parser resolves tool block metadata from hydrated trace (not preview trace)

4. fallback on fetch failure
- network/decrypt failure -> render preview content without crash

5. fallback on parse failure
- invalid JSON -> render preview content

6. cache behavior
- repeated render with same MXC URI should not refetch

---

## Non-Goals / Constraints

- Do not reintroduce legacy `<tool>` / `<tool-group>` compatibility.
- Do not add generic abstraction layers beyond what is needed for this path.
- Keep changes localized to long-message/hydration and parser-option wiring.

---

## Definition of Done

- Oversized MindRoom messages render from sidecar full content object.
- Tool-ref v2 blocks for oversized messages render with correct metadata.
- Encrypted and unencrypted sidecars both work.
- Preview fallback remains reliable on errors.
- Tests cover hydration + parser integration + fallback + cache.

---

## Deliverables

1. Code changes in `/home/basnijholt/nix/src/mindroom-cinny`.
2. Test updates/additions (paths above).
3. Final report:
- files changed
- hydration flow
- encrypted handling
- parser wiring for hydrated content
- test command(s) and results

