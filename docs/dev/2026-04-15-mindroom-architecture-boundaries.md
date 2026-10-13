# MindRoom Architecture Boundaries

This record states the intended ownership and import boundaries of the core MindRoom runtime and why they exist.
`tach.toml` is the source of truth for what is enforced; this page records the intent those rules should converge on.

## Why

MindRoom grew many runtime capabilities faster than a shared account of who owns what, and both human contributors and AI agents working in the codebase need that account to be explicit.
The goals are maintainability, debuggability, fewer de facto APIs, and fewer places where policy hides in helper layers.

## Principles

Prefer clear ownership over abstract purity: each domain has one primary reason to change and a small set of obvious public entrypoints.
Internal storage, helper, and wiring modules must not become accidental public APIs through repeated cross-domain imports.
Higher-level code depends on stable facades, protocols, and package-level boundaries where they exist, so low-level modules can change without breaking hidden consumers.
Progress is measured in fewer conceptual hops, even when line count does not fall.
Unit tests of an internal module may use its private types directly, while broader tests prefer public seams so the tests mirror the intended architecture.

## Domain intent

Each domain has a boundary it must keep.

**Runtime and orchestration** is the composition root, so it may construct concrete implementations, but those construction sites must not become the preferred interface for unrelated consumers, and it must not quietly redefine lower-level domain policy.

**Matrix conversation** owns durable event knowledge and conversation semantics, not broader orchestration policy.
Callers above the event journal should not need to know how its storage decomposes into backend, projection, outbox, or membership internals.
Thread and conversation policy should live in a small number of named decision points.

**Tool execution and dispatch** passes explicit runtime and identity contracts to dispatch and hook bridges instead of rebuilding them from partial ambient state, so tool entrypoints are legible from call sites and invalid runtime shapes are hard to construct.

**Agent and team behavior** reaches into runtime or Matrix internals only where a real orchestration boundary requires it.

**Memory and knowledge** keep storage details and prompt-building helpers behind service interfaces.

**User-facing control surfaces** (chat commands, scheduling, REST API, dashboard paths) consume runtime, conversation, and tool contracts; they never own storage or persistence policy or become alternate implementations of those contracts.

**Cross-cutting infrastructure** (attachments, streaming, authorization, logging, background tasks) stays boring and reusable, moving data across domains without becoming a second source of truth for domain policy.

## Enforcement is not convergence

Tach refuses only the imports its declared rules forbid, and the rules describe the import graph as it is, so some of them record debt rather than intent.
Modules that `tach.toml` does not declare are outside its dependency rules entirely.
Tightening a boundary means narrowing a `depends_on` list or adding an interface, then fixing the imports the change rejects.
When this page and `tach.toml` disagree, one of them is wrong, and deciding which is the useful outcome.

## Planned tightening

- Callers depend on runtime contracts rather than on the bot and orchestrator lifecycle shells.
- The event journal exposes a limited public boundary that callers above it use instead of its internal modules.
- Thread-domain policy gets its own boundaries.
- Knowledge boundaries get cleaned up.
- Other domains get selected private-helper import rules.

## Non-goals

This is not a rewrite plan and does not imply enforcing the whole architecture at once.
The boundary decisions here are not final.
Neither more file splitting nor fewer files is a goal in itself.
