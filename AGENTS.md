# AGENTS.md

This file provides guidance to coding agents (Claude Code, Codex, and others) when working with code in this repository.
Keep this file under 32 KiB, because Codex reads only the first 32 KiB of project instructions and silently drops the rest; `tests/test_agents_md.py` enforces the limit.
Put reference material that agents read on demand, such as the code map and live-run procedures, in the linked files instead.

## Project Overview

MindRoom - AI agents that live in Matrix and work everywhere via bridges. The project consists of:
- **Core MindRoom** (`src/mindroom/`) - AI agent orchestration system with Matrix integration
- **SaaS Platform** (`saas-platform/`) - Kubernetes-based platform for hosting MindRoom instances
  - Platform Backend (FastAPI) - API server for subscriptions, instances, SSO
  - Platform Frontend (Next.js 16) - Dashboard for managing instances
  - Instance deployment via Helm charts

## 1. Core Philosophy

- **Embrace Change, Avoid Backward Compatibility**: This project has no end-users yet. Prioritize innovation and improvement over maintaining backward compatibility.
- **Simplicity is Key**: Implement the simplest possible solution. Avoid over-engineering or generalizing features prematurely.
- **Focus on the Task**: Implement only the requested feature, without adding extras.
- **Functional Over Classes**: Prefer a functional programming style for Python over complex class hierarchies.
- **Keep it DRY**: Don't Repeat Yourself. Reuse code wherever possible.
- **Be Ruthless with Code Removal**: Aggressively remove any unused code, including functions, imports, and variables.
- **Prefer dataclasses**: Use `dataclasses` that can be typed over dictionaries for better type safety and clarity.
- **Documentation Line Style**: In Markdown docs, write one sentence per line, and never split a single sentence across multiple lines.
- Do not wrap things in try-excepts unless it's necessary. Avoid wrapping things that should not fail.
- NEVER put imports in the function, unless it is to avoid circular imports or to keep a heavy import (for example a provider SDK) out of module import time. Prefer an explicit function-level `from x import Y` with `# noqa: PLC0415` over dynamic `import_module` indirection. Imports should be at the top of the file.
- `tests/test_import_graph.py` pins the complete third-party import surface of slim CLI, config, tool-registry, and sandbox entry points, and bans heavy optional provider, storage, and ML/data dependencies from the primary runtime. If it fails on your change, defer the new import to first use; extend the allowlist only when the dependency is genuinely needed at import time.
- Do not use `getattr()` or `hasattr()` to weaken a typed interface or probe for fields that the declared type should guarantee.
- If mocks or tests break, fix them to use proper typed objects or stricter mocks instead of adding dynamic attribute fallbacks in production code.
- **Merge and forget**: Code you touch should be polished enough to never revisit. Fix rough edges in code you're already changing.

### Documentation Policy

- The primary reader of `docs/` is an AI agent running inside MindRoom, which loads whole pages through the bundled `mindroom-docs` skill to explain, configure, operate, and troubleshoot MindRoom for its user.
  Every sentence on a page costs context on every question that page answers, so a sentence belongs only when that agent needs it.
- **Default to no change in user docs**, meaning the `zensical.toml` nav pages outside `docs/architecture/`, which the `mindroom-docs` skill bundles.
  Bug fixes that restore documented behavior, refactors, and hardening or internal limits that normal use never reaches need none; contributor pages such as the code map, `security-posture.md`, `migrations.md`, and `agno-compatibility.md` follow their own update rules.
  Change docs only when configuration, user-visible behavior, or an operator procedure changes, and then add only the sentences that change an answer to a user question.
- **Name the question before writing a sentence**: keep it only when the agent would answer a realistic user question worse without it, such as how to set something up, what a setting does, or why something did or did not happen.
  If you cannot name that question, the sentence does not belong in user docs.
- Document what a reader can do, configure, observe, or rely on: features and when to use them, behavior and limits that normal use reaches, errors and how to resolve them, and operator procedures such as install, deploy, upgrade, migrate, back up, and recover.
- Document each public config field once, on its owning page, with its type, default, valid values, and any inheritance or prerequisites.
  Show a few examples of realistic tasks instead of one example per field.
- State guarantees as outcomes, such as "restarts do not produce duplicate replies", not as the mechanism that provides them.
- Leave out implementation mechanics: locks, transactions, journals, caches, retries, internal IDs, module and class names, ordering internals, encoding details, and change history such as "previously" or "now".
  Also leave out hardening limits that normal use never reaches, and behavior on rare failure, cancellation, recovery, and replay paths unless a user would plausibly ask about it.
  Put an invariant contributors need in `docs/architecture/`, a code comment, or a test instead.
  Every `docs/architecture/` page and every page outside the nav, such as `docs/dev/`, is for contributors and operators and may explain mechanisms; user docs name implementation details only when a documented procedure needs them.
- Sentences like these fail the question test:
  - "An `index.json` larger than 8 MiB is rebuilt from the thread files on every pass that reaches its room." states a hardening limit normal use never reaches, with no effect a user sees.
  - "Restart recovery now checks the handled-turn ledger before replaying journal events." narrates a mechanism and its history; the outcome is "restarts do not produce duplicate replies".
  These pass: "Edits to an agent's `instructions` or `model` apply from its next reply without restarting it." answers "do I need to restart?", and the note in `docs/configuration/history.md` that a voice-call reply or a reply resuming after a tool approval can still use a redacted message is a rare path that answers "can the agent still see what I deleted?".
- Each topic has one owning page that states each of its facts once; other pages link to it instead of splitting its rules across pages.

### Refactor Policy

- Default to the smallest correct change.
- Never add a production branch, wrapper, or fallback whose only purpose is to preserve an old test expectation.
- Treat `src/mindroom/bot.py` and `src/mindroom/orchestrator.py` as composition roots and lifecycle shells, not feature implementation modules.
- Default to putting new behavior in focused modules or collaborators, then wire it into `bot.py` or `orchestrator.py` through a small public method or dependency.
- A PR that adds substantial code to `bot.py` or `orchestrator.py` must explain why the code truly belongs at that lifecycle boundary and why a focused module would be worse.
- During PR review, flag growth in `bot.py` or `orchestrator.py` as a design smell unless it is limited to routing, lifecycle coordination, dependency wiring, or calls into extracted collaborators.
- Every 3 review rounds, if reviews still show many issues or new major bug classes, stop patching and reconsider the design before another patch round.
- Use larger refactors when they provide clear immediate maintenance ROI, not hypothetical future value.
- A larger refactor is justified only if it:
  - Removes active duplication in current code paths.
  - Creates a clear source of truth without adding unnecessary abstraction layers.
  - Reduces net complexity (simpler call flow, fewer special cases).
  - Is covered by tests in the same PR.

### Migration Policy

Assume there are no active responses when migrations run.
Design migrations around that assumption rather than adding machinery to coordinate with active responses.

### Legacy Compatibility Policy

- Isolate substantive historical schemas and representations in `legacy_<subject>.py` beside their current owner.
  This includes one-time migrations, recurring old-data readers, and diagnostics for retired fields.
- Keep current-format processing, transactions, locking, validation, authorization, and retries with the current storage or lifecycle owner.
  Small field defaults can stay with their model when extraction would only add indirection.
- Each substantive legacy rule must have a nearby source comment whose first line is `# LEGACY_COMPAT: <short description of the legacy format>`.
  Use one marker per documented rule, including small defaults and compatibility notes kept beside current owners, so `rg -n -F 'LEGACY_COMPAT:' --glob '*.py'` lists them across the repository.
  Follow the marker with these fields:
  - `Legacy format`: the old representation and the condition that selects this rule.
  - `Last legacy release`: the last stable MindRoom release whose native writer or typed model emitted that representation, plus the replacement release and format.
  - `Handling`: what the current reader, migration, or rejection does and which guarantees it preserves.
  - `Coverage`: repository-relative regression test paths, preferably exact test node IDs.
- Verify release provenance from history and tags.
  The last release that accepted old data is not the last native writer release.
  State unreleased, unversioned external input, no tagged native model, or schema-based recovery explicitly when no single release cutoff exists.
- Keep the boundary map in `docs/architecture/migrations.md` aligned with changes; source comments own the exact provenance and test references.

### Agno Compatibility Policy

- The goal is to identify Agno weaknesses, contribute focused upstream fixes or extension points, and remove local workarounds as those changes ship.
  Judge an extraction by whether it makes that upstream work easier to understand, test, and retire.
- Isolate substantive Agno monkey patches, copied SDK internals, private-API adapters, and upstream bug workarounds in `agno_compat_<subject>.py` beside their owning module.
  Ordinary public-API usage and MindRoom's orchestration, approval, history, and storage policies remain with their current owners.
  Tiny overrides may stay in a cohesive adapter when extraction would only add indirection, but require the same source comment.
- Each distinct workaround must have a nearby source comment whose first line is `# AGNO_COMPAT: <short description of the upstream weakness>`.
  Use one marker per independently removable workaround, including tiny overrides kept beside their owners, so `rg -n -F 'AGNO_COMPAT:' src/mindroom` lists each gap with a useful summary.
  Follow the marker with these fields:
  - `Reason`: the concrete upstream behavior or missing extension point and its effect on MindRoom.
  - `Upstream issue`: a verified issue URL, or an explicit tracking gap and why the boundary is needed.
  - `Upstream PR`: a verified fix or API proposal URL when one exists; state when none is identified or the linked PR covers only part of the workaround.
  - `Remove when`: the exact upstream behavior that allows removal, including any MindRoom behavior that must remain.
  - `Coverage`: repository-relative regression test paths, preferably exact test node IDs.
- Distinguish upstream bugs from missing public extension points and intentional application policy.
  Never invent a tracking link or treat a related PR as a complete fix.
  Separate removal conditions when one module handles multiple upstream gaps.
- Treat an explicit tracking gap as unfinished upstream work.
  Record the concrete failing behavior or required extension point in the inventory, search for existing tracking before opening a new item, and replace the gap with verified links when available.
- For an upstream contribution, reduce the problem to an Agno-only reproducer and regression test where possible.
  Keep MindRoom-specific policy out of the proposed fix and retain local integration coverage for the behavior MindRoom requires.
- Keep patch installation explicit and idempotent, preserve optional-import boundaries, and retain version guards where private signatures or semantics require them.
- Keep the weakness-to-upstream map and boundary inventory in `docs/architecture/agno-compatibility.md` aligned with contributions, extractions, and removals; source comments own exact upstream tracking and test references.
- On each Agno upgrade, inspect these boundaries, verify which fixes the pinned release includes, and run their behavioral tests.
  Remove a workaround only when the relevant tests pass without it; a merged PR alone is insufficient.
  Retain regression coverage for behavior MindRoom still requires and update Tach boundaries with any extraction or removal.

### Security Trust Model

A worker container (worker routing through the sandbox proxy) is the only security boundary between an agent and the MindRoom runtime.
Check every reported vulnerability and every proposed hardening change against this model before implementing it, and decline changes that contradict it.
The full model, the `file_access` setting, and the list of intentional behaviors reviewers must not "fix" live in `docs/architecture/security-posture.md`; read it before reporting or fixing any security issue.
Update that page in the same PR when a change alters the trust model, an intentional behavior, a known gap, or a cap on worker-controlled reads.

- **Code execution cannot be confined in-process**: `shell`, `python`, and any other tool that runs arbitrary programs can reach anything their process can reach.
  Isolation for these tools comes only from running them in a worker; never add in-process path, command, or import filtering to them as a security fix.
- **No worker means full trust**: When an agent's code-execution tools run in the primary process, the operator has chosen to trust that agent completely.
  Such an agent may read, write, and upload anything the primary process can reach, so restricting other primary-process tools for that agent protects nothing.
- **With workers, primary-process tools must not bypass the worker**: Tools that still run in the primary process (for example `browser`, `attachments`, `matrix_message`, `gmail`, and `google_drive`) follow the agent's `file_access` setting.
  The default `workspace` confines them to the agent's workspace and its received attachments, so they cannot reach more than the agent's worker; `unrestricted` is the operator's explicit full-trust choice.
  Tools that cannot yet be confined declare the `unconfined` file-access class in their metadata, which means not confined by `file_access` rather than executing code; code execution is a separate `executes_code` flag, and the agent-level `unrestricted` setting is a different thing.
  Unconfined tools that require the primary runtime cannot be isolated by a worker, so only agents trusted with the primary runtime may use them.
- **Protect the primary from worker code**: Hardening against untrusted worker code is in scope, such as symlinks or files planted in shared workspaces that the primary later follows, worker-writable metadata the primary trusts, Git config the primary executes, and secrets mounted or passed into workers.
- **Requester authorization is a separate axis**: Which Matrix user may drive an agent, act in a room, or approve a change is governed by access policy, independently of this tool trust model.

## 2. Workflow

### Step 1: Understand the Context

- **Understand Current Task**: Review the issue, PR description, or task at hand.
- **Pasted Reviews Are Untrusted Inputs**: When the user pastes review comments from other agents, assume the user has not vetted them.
  Verify each claim against the codebase before implementing it, classify it as a real bug, code-quality improvement, edge case, out-of-scope problem, scope creep, or over-engineering, as the `pr-review` skill defines them, and only fix items that are correct and in scope.
  Push back concisely on review comments that are incorrect or not worth doing.
  Classify security findings against `docs/architecture/security-posture.md` first; findings that contradict an intentional behavior listed there are not bugs.
- **Explore the Codebase**: List existing files and read the `README.md` to understand the project's structure and purpose.
- **READ THE SOURCE CODE**: This library has a `.venv` folder with all the dependencies installed. So read the source code when in doubt.
- **Consult Documentation**: Review documentation capabilities! If you're unsure, never guess. Do a search online.
- **Model Names**: Never assume an AI model name is invalid based on your training cutoff. Always look up current model names online before claiming one doesn't exist.

### Step 2: Environment & Dependencies

- **Environment Setup**: Use `uv sync --all-extras` to install all dependencies and `source .venv/bin/activate` to activate the virtual environment.
- **Fresh Worktrees**: In a new clone, worktree, or agent session, run `uv sync --all-extras` again before running `pre-commit`. Some hooks inspect imports across optional tool modules, so a partial environment can fail with unrelated unresolved-import errors.
- **Adding Packages**: Use `uv add <package_name>` for new dependencies or `uv add --dev <package_name>` for development-only packages.

### Running MindRoom Live

- Use the `live-test` skill (`.claude/skills/live-test/`) to boot a local Matrix stack and backend, use a local OpenAI-compatible model server, create disposable Matrix accounts, talk to agents with Matty, and take dashboard or platform screenshots.
- `docs/dev/ops/README.md` lists the `just` recipes, including the destructive `just local-matrix-reset`; read its warning before resetting.
- For hosted Matrix with pairing (`uvx mindroom config init --matrix-server mindroom.chat`, then `uvx mindroom run`), see `docs/getting-started.md` and `docs/deployment/hosted-matrix.md`.
- SaaS platform deployment, staging values, release deploys, and Supabase migrations are documented in `docs/deployment/saas-platform.md`.
- `docs/cli.md` documents every `mindroom` command, including `mindroom doctor`, `mindroom run --log-level DEBUG`, and `mindroom local-stack-setup`.

### Step 3: Development & Git

- **Check for Changes**: Before starting, review the latest changes from the main branch with `git diff origin/main | cat`. Make sure to use `--no-pager`, or pipe the output to `cat`.
- **Commit Frequently**: Make small, frequent commits.
- **Atomic Commits**: Ensure each commit corresponds to a tested, working state.
- **Preserve Review History**: Do not amend commits or force-push PR branches unless the user explicitly asks for it. Prefer follow-up commits so PR history stays inspectable.
- **Targeted Adds**: **NEVER** use `git add .`. Always add files individually (`git add <filename>`) to prevent committing unrelated changes.
- **No AI Attribution**: **NEVER** add Claude, Codex, or Gemini co-author trailers, "Generated with Claude Code" or "Generated with Codex" footers, Claude Code session or Codex task links, or `@anthropic.com` or `@openai.com` commit identities.
  The `check-commit-attribution` commit-msg hook (installed by `uv run pre-commit install`) and the `commit-attribution` pull request check reject them.

### Step 4: Testing & Quality

- **Test Before Committing**: **NEVER** claim a task is complete without running `pytest` to ensure all tests pass.
- **Test at the Owning Seam**: New behavior tests target the collaborator seam that owns the behavior (`ResponseRunner`, `DeliveryGateway`, `TurnPolicy`, `TurnController`, `CoalescingGate`, `IngressValidator`, `EditRegenerator`), not the `AgentBot`/`TeamBot` facade.
  Fake collaborators through the conftest seam installers (`install_generate_response_mock`, `install_send_response_mock`, `replace_edit_regenerator_deps`) or the `*Deps` dataclasses, never by assigning mocks onto bot attributes.
  A test needing more than 3 patches is a smell that it is testing through the wrong seam.
  Genuinely end-to-end tests belong in the slim integration files (`test_multi_agent_bot.py`, `test_threading_error.py`), which stay small by design.
- **NixOS Test Shell**: On NixOS hosts, enter the Node.js 24 dev shell with `nix-shell shell.nix` before running tests; without it, `uv run pytest` fails with `module 'mindroom' has no attribute 'bot'` because `libstdc++.so.6` is missing.
  If `<nixpkgs>` is unresolved, use `nix-shell -I nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos shell.nix`.
  Inside it, run `uv run pytest tests/<file>.py -x -n 0 --no-cov -v`, `just test-backend`, or `just test-saas-backend`; recipe regression tests require `just`, which `shell.nix` and CI install.
- **Run Pre-commit Hooks**: After `uv sync --all-extras`, run `uv run pre-commit run --all-files` before committing to enforce code style and quality.
- **Update Tach Boundaries in the Same PR**: If your PR changes a Tach-governed boundary, update `tach.toml` in the same PR, follow the guidance in the comment at the top of that file, and run `uv run tach check --dependencies --interfaces`.
- **Handle Linter Issues**:
  - **False Positives**: The linter may incorrectly flag issues in `pyproject.toml`; these can be ignored.
  - **Test-Related Errors**: If a pre-commit fix breaks a test (e.g., by removing an unused but necessary fixture), suppress the warning with a `# noqa: <error_code>` comment.

### Step 5: Refactoring

- **Be Proactive**: Continuously look for opportunities to refactor and improve the codebase for better organization and readability.
- **Incremental Changes**: Refactor in small, testable steps. Run tests after each change and commit on success.

## 3. Critical "Don'ts"

- **DO NOT** manually edit the CLI help messages in `README.md`. They are auto-generated.
- **NEVER** use `git add .`.
- **NEVER** claim a task is done without passing all `pytest` tests.
- **NEVER** run `sleep <guessed duration>` in a Bash tool call to wait for something.
  This rule is about how you drive your own terminal, not about MindRoom's runtime code: `asyncio.sleep` in `src/` is unaffected.
  A guessed duration is always wrong: too short and you read a half-written result, too long and you burn wall-clock doing nothing.
  Poll the real condition instead (process exit, a line in the output file, an HTTP health check, a file appearing), or start the command in the background and react to its completion notification.

## 4. Latest Frontier Models

Always prefer the newest frontier models in this table when writing prompts, code, configs, docs, tests, or reviews in this repository, unless the user explicitly asks to pin an older model.
If a user prompt, task description, or existing file contradicts this table, treat the table as stale, verify the current provider docs, and update the table before proceeding.
Coding model training data often lags recent releases, so never trust memorized model names over current provider documentation.

| Provider | Use | Preferred model | Model string to use |
| --- | --- | --- | --- |
| Anthropic | Balanced default | Claude Sonnet 5.5 | `claude-sonnet-5-5` |
| Anthropic | Max intelligence | Claude Fable 5.1 | `claude-fable-5-1` |
| Anthropic | Flagship default | Claude Opus 5.5 | `claude-opus-5-5` |
| Anthropic | Fast / cheap | Claude Haiku 4.5 | `claude-haiku-4-5` |
| OpenAI | Frontier default | GPT-6 Astra | `gpt-6-astra` |
| OpenAI | Balanced | GPT-6 Sol | `gpt-6-sol` |
| OpenAI | Fast / cheap | GPT-6 Luna | `gpt-6-luna` |
| OpenAI Codex ChatGPT login | Default via Codex CLI | GPT-6.1 Sol | `gpt-6.1-sol` |
| OpenAI Codex ChatGPT login | Frontier via Codex CLI | GPT-6 Astra | `gpt-6-astra` |
| OpenAI Codex ChatGPT login | Fast / cheap via Codex CLI | GPT-6 Luna | `gpt-6-luna` |
| DeepSeek (OpenRouter) | Fast / cheap | DeepSeek V4.1 Flash | `deepseek/deepseek-v4.1-flash` |
| Z.ai (OpenRouter) | Flagship | GLM-5.3 | `z-ai/glm-5.3` |
| OpenAI | Image generation / editing | GPT Image 2.5 Sunburst | `gpt-image-2.5-sunburst` |
| OpenAI | File transcription | GPT Transcribe | `gpt-transcribe` |
| Google (Vertex AI) | Video generation | Veo 3.1 | `veo-3.1-generate-001` |
| Qwen | Local 27B | Qwen3.8-27B | `qwen3.8:27b` (Ollama), `unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL` (llama.cpp) |
| Moonshot Kimi Code login | Frontier via Kimi Code CLI | Kimi K3 | `k3` |
| Google (Gemini API) | Max intelligence | Gemini 3.1 Pro Preview | `gemini-3.1-pro-preview` |
| Google (Gemini API) | Standard text / coding | Gemini 3.8 Flash | `gemini-3.8-flash` |
| Google (Gemini API) | Fast / cheap text | Gemini 3.5 Flash-Lite | `gemini-3.5-flash-lite` |
| Google (Gemini API) | Image generation / editing | Nano Banana 2 | `gemini-3.1-flash-image` |
| Google (Gemini API) | Embeddings for `google` | Gemini Embedding 2 | `gemini-embedding-2` |

Model IDs were checked against provider catalogs on September 28, 2026, and the Codex rows against the Codex model catalog on October 1, 2026.
OpenRouter uses `anthropic/claude-fable-5.1`, Bedrock uses `anthropic.claude-fable-5-1`, and the direct Anthropic and Vertex APIs use `claude-fable-5-1`.
Likewise, OpenRouter uses `anthropic/claude-opus-5.5` and `anthropic/claude-sonnet-5.5`, and Bedrock uses `anthropic.claude-opus-5-5` and `anthropic.claude-sonnet-5-5`.
For the direct DeepSeek API, prefer `deepseek-flash` for V4.1 Flash and `deepseek-v4-pro` for Pro; do not substitute the OpenRouter V4.1 ID on the direct API.
The older `deepseek-v4-flash` name remains accepted as a [temporary compatibility route to V4.1 Flash](https://api-docs.deepseek.com/updates/#date-2026-09-10).

For `anthropic`, prefer `claude-sonnet-5-5`, `claude-opus-5-5`, and `claude-haiku-4-5` unless you intentionally need a pinned snapshot ID.
Use `claude-fable-5-1` when you need Anthropic's highest available capability.
Claude Fable 5.1 is generally available on the direct Anthropic API and the documented cloud platforms.
For `vertexai_claude`, use the current Vertex AI request name from the provider docs instead of assuming the Anthropic API ID carries over unchanged.
Current Google Cloud docs list bare Vertex IDs for `claude-fable-5-1`, `claude-opus-5-5`, `claude-sonnet-5-5`, and [`claude-haiku-4-5`](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude/haiku-4-5).
Do not assume `@default` or dated `@...` suffixes are universally required for Vertex AI Claude.
For Gemini API text and coding work, prefer `gemini-3.8-flash` as the standard stable model unless you intentionally need the cheaper `gemini-3.5-flash-lite` tier.
Use `gemini-3.1-pro-preview` only when you need the highest Gemini API intelligence tier and accept a preview model.
The Google rows above are for the Gemini API / AI Studio `google` provider, not for Vertex AI.
For `vertexai`, verify the current Vertex AI docs instead of assuming Gemini API names or defaults carry over unchanged.
Current [Vertex AI image docs](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/image-generation) document `gemini-3-pro-image`, `gemini-3.1-flash-image`, and `gemini-3.1-flash-lite-image`; choose the tier that fits the task.
For Google image work, use the official product name from the docs for the provider surface you are editing.
Gemini API docs call `gemini-3.1-flash-image` Nano Banana 2, while Vertex AI docs use their own product naming and model tables.

## 5. Architecture

### Core MindRoom (`src/mindroom/`)

**MultiAgentOrchestrator** (`orchestrator.py`) is the heart of the system - it boots every configured entity (router, agents, teams), provisions Matrix users, and keeps sync loops alive with hot-reload support when `config.yaml` changes.

**Entity types**:
- `router`: Built-in traffic director that greets rooms and decides which agent or team should answer
- **Agents**: Single-specialty actors defined under `agents:` in `config.yaml`
- **Teams**: Collaborative bundles of agents that coordinate or parallelize work

**Code map**: `docs/architecture/code-map.md` holds the inbound turn pipeline, a one-line purpose for each key module under `src/mindroom/`, and where persistent state lives.
Read it to locate code, and update its rows when you add, rename, or remove a key module.
Turn handling is described in `docs/architecture/bot-runtime.md`, and minimal-mode ownership and recovery in `docs/architecture/agent-cli.md`.

### SaaS Platform (`saas-platform/`)
- **Platform Backend**: Modular FastAPI app with routes in `saas-platform/platform-backend/src/backend/routes/`
- **Platform Frontend**: Next.js 16 with centralized API client in `saas-platform/platform-frontend/src/lib/api.ts`
- **Authentication**: Host-only platform cookie on the API host; instance dashboards exchange single-use, instance-signed tickets for host-only instance sessions
- **Deployment**: Kubernetes with Helm charts, dual-mode support (platform/standalone)
- **Database**: Supabase with comprehensive RLS policies

### Repo Layout

| Path | Purpose |
|------|---------|
| `src/mindroom/` | Core agent runtime (Matrix orchestrator, routing, memory, tools) |
| `frontend/` | Core MindRoom dashboard (Vite + React) |
| `saas-platform/platform-backend/` | SaaS control-plane API (FastAPI) |
| `saas-platform/platform-frontend/` | SaaS portal UI (Next.js 16) |
| `saas-platform/supabase/` | Supabase migrations, policies, seeds |
| `cluster/` | Terraform + Helm for hosted deployments |
| `local/` | Docker Compose helpers for local dev stacks |

### Ecosystem Repositories

MindRoom also maintains related repositories under `github.com/mindroom-ai`, many of them cloned in the parent directory (`../`) of this dev environment:
- `synapse` - Synapse fork with optional compact-edit collapsing for superseded `m.replace` events, advertised in `/versions` as `org.mindroom.compact_edits` (see `README.md` and `FORK_CHANGES.md`).
- `mindroom-librechat` - LibreChat fork that renders MindRoom inline `<tool>` / `<tool-group>` tags as native `ToolCall` cards (see `README.md` and `.mindroom/fork-context.md` and `.mindroom/tool-tag-rendering.md`).
- `mindroom-chat` - MindRoom Chat, the Cinny-based Matrix client for MindRoom on the web, iOS, and Android (see `README.md` and `FORK_CHANGES.md`).
- `mindroom-stack` - Docker Compose reference stack with the published MindRoom backend and frontend, a Tuwunel homeserver, and MindRoom Chat.

### Configuration Model

The authoritative config is `config.yaml`, loaded via Pydantic models in `src/mindroom/config/` (root model in `src/mindroom/config/main.py`); `docs/configuration/index.md` shows a minimal example and lists every top-level section with its owning page.
`config.yaml` changes are watched at runtime: the orchestrator diffs configs, applies what it can in place, and restarts only the affected entities without bringing down the stack.

## 6. Releases

Pushes to `main` publish a CalVer release automatically through `.github/workflows/calver-auto-release.yml`.
Read the Releases section of `docs/dev/ops/README.md` before retrying a failed publication.

# Important Instruction Reminders
Do what has been asked; nothing more, nothing less.
NEVER create files unless they're absolutely necessary for achieving your goal.
ALWAYS prefer editing an existing file to creating a new one.
