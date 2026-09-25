# Skills

MindRoom uses Agno's skills system with OpenClaw-compatible metadata. Skills are instruction packs (a `SKILL.md` file) with optional scripts and references that guide agents without adding new code capabilities.

## Skill directory structure

A skill is a directory containing:

```
my-skill/
├── SKILL.md         # Required: instructions; YAML frontmatter is recommended
├── scripts/         # Optional: executable scripts
│   └── audit.sh
└── references/      # Optional: reference documents
    └── examples.md
```

Agents access skills via `get_skill_instructions()`, scripts via `get_skill_script()`, and references via `get_skill_reference()`.

## SKILL.md format (OpenClaw compatible)

```markdown
---
name: repo-quick-audit
description: Quick repository audit checklist
metadata: '{openclaw:{requires:{bins:["git"], env:["GITHUB_TOKEN"]}}}'
---

# Repo Quick Audit

1. Check CI status
2. Review open issues
```

Notes:

- `metadata` can be a JSON5 string (shown above) or a YAML mapping.
- If `name` is omitted, MindRoom falls back to the skill directory name.
- If `description` is omitted or blank, MindRoom falls back to the resolved skill name.
- If YAML frontmatter is omitted entirely, the skill still loads with those same name/description fallbacks. Frontmatter is still recommended for clearer listings and metadata.

## Frontmatter fields

| Field | Type | Description |
| --- | --- | --- |
| `name` | string | Unique skill identifier |
| `description` | string | Brief summary shown to users/models; defaults to the skill name when omitted or blank |
| `metadata` | mapping or JSON5 string | OpenClaw metadata and custom fields |
| `license` | string | Informational only; accepted but not used by the runtime |
| `compatibility` | string | Informational only; accepted but not used by the runtime |
| `allowed-tools` | list | Reserved; accepted in frontmatter but not enforced by the runtime |

## Eligibility gating (OpenClaw metadata)

If `metadata.openclaw` is present, MindRoom filters skills using these rules:

- `os: ["linux", "darwin", "windows"]`
- `always: true` bypasses `requires`, but it does not bypass an OS mismatch
- `requires.env`: env var set or credential key exists
- `requires.config`: config path is truthy (e.g., `agents.code.tools`)
- `requires.bins`: all binaries must exist in PATH
- `requires.anyBins`: at least one binary must exist in PATH

Skills without `metadata.openclaw` are always eligible.

## Installing and managing skills

Install a user skill manually at `~/.mindroom/skills/<name>/SKILL.md`, or use the dashboard Skills page to create, view, edit, and delete user skills.
Bundled and plugin-provided skills are visible but read-only in the dashboard.
Add a bundled, plugin, or user skill name to the agent's `skills:` allowlist before that agent can use it.

## Skill locations and precedence

MindRoom resolves skills for each agent from these locations, in this order:

1. Bundled skills: `skills/` at the repository root (if present)
2. Plugin-provided skill directories (see [Plugins](https://docs.mindroom.chat/plugins/))
3. User skills: `~/.mindroom/skills/`
4. Agent workspace skills: `<resolved workspace>/skills/`

For agents without `private`, this is `<storage>/agents/<agent>/workspace/skills/`.
Private instances use `<storage>/private_instances/<scope-directory>/<agent>/<private.root>/skills/`; see [Private Instances](https://docs.mindroom.chat/configuration/agents/#private-instances).

If multiple skills share the same name, the last one wins (agent workspace > user > plugin > bundled).

Agent workspace skills are only available to the owning agent or private instance at runtime.
They do not appear in the global skills API or dashboard listing because those views are not agent-scoped.
Workspace skills are read through no-follow descriptors because worker code can share the workspace.
Links and special files inside `skills/` are skipped, and hidden entries such as `.usage.json`, `.history/`, and `.archive/` are never loaded as skills.

## Authoring skills as an agent

Agents never need write access to a global skill root to create skills.
The bundled, plugin, and user roots can be read-only at runtime, for example in container or Kubernetes deployments where the image filesystem and `~/.mindroom` are not writable.
An agent with a canonical workspace and authorized workspace-rooted file or shell tools can author skills at `<workspace>/skills/<skill-name>/SKILL.md`, using the same `SKILL.md` format described above.
Workspace-rooted file tools address this location as the relative path `skills/<skill-name>/SKILL.md`.
Agents with the required workspace and authoring access receive this guidance in their system prompt through the `WORKSPACE_SKILL_AUTHORING_PROMPT` built-in prompt, which can be overridden via the root `prompts` block (see [Built-In Prompt Overrides](https://docs.mindroom.chat/configuration/#built-in-prompt-overrides)).
A new or edited workspace skill is picked up on the agent's next run without a config change.

## Configuring skills

Add skills to an agent allowlist in `config.yaml`:

```yaml
agents:
  developer:
    display_name: Developer
    role: A coding assistant
    model: sonnet
    skills:
      - repo-quick-audit
      - code-review
```

The `skills:` list is an allowlist for bundled, plugin, and user skills.
If `skills` is empty or unset, the agent gets no bundled, plugin, or user skills.
Workspace skills under `<resolved workspace>/skills/` are still auto-loaded for that agent or private instance.
This lets an agent create or receive skills in its own workspace without editing `config.yaml`.

Workspace auto-loading is a runtime capability, not a proactive behavior policy.
If you want agents to create skills on their own when they notice reusable workflows, add that guidance to the agent's prompt or instructions, or enable [automatic skill learning](#automatic-skill-learning).

## Using skills at runtime

Agents see available skills in the system prompt and can load details using these tools:

- `get_skill_instructions(skill_name)` - Load the full instructions for a skill
- `get_skill_reference(skill_name, reference_path)` - Access reference documentation
- `get_skill_script(skill_name, script_path, execute=False, args=None, timeout=30)` - Read or execute scripts

Workspace skill scripts can be read with `get_skill_script(..., execute=False)`.
Workspace skill scripts cannot be executed through `get_skill_script(..., execute=True)`.
Agents that have shell or file execution permissions can still read and execute workspace files through their normal authorized tools.

## Skill vs tool

| Aspect | Skills | Tools |
| --- | --- | --- |
| Definition | Markdown + YAML | Python code |
| Location | File system | Code/plugins |
| Filtering | Automatic by requirements | Configured per agent; may be deferred or disabled |
| Instructions | Rich markdown | Docstrings |
| Invocation | Model via skill tools | Model only |

## Hot reloading

MindRoom polls skill directories every second. When a `SKILL.md` file is added, removed, or modified, the skill cache is automatically cleared so agents pick up the new instructions on their next request.
For workspace skills created during an agent turn, assume they become available on the next agent run rather than in the same response.

## Best practices

1. Keep skills focused - one skill per capability
2. Declare dependencies with `metadata.openclaw.requires`
3. Use descriptive names like `code-review`

## Automatic skill learning

Automatic skill learning follows the self-improvement loop of [Hermes Agent](https://github.com/NousResearch/hermes-agent).
After enough work in a conversation, a background review maintains a small library of class-level skills in the agent's workspace, and a curator pass archives the ones nobody uses.
It is opt-in for each agent:

```yaml
agents:
  assistant:
    display_name: Assistant
    skill_learning:
      enabled: true
      model: default
      review_interval: 10
      timeout_seconds: 120
      notify: true
      archive_after_days: 30
```

All fields, defaults, and bounds are listed in the [agent configuration reference](https://docs.mindroom.chat/configuration/agents/#automatic-skill-learning).

### When reviews run

After each successful standalone-agent response to a person in Matrix, including an approved continuation, the background worker counts the model replies stored in that conversation since its last review, counting each tool-calling step and the final answer.
A review runs once that count reaches `review_interval`, and counting then starts after the newest run the review saw.
The count is read from the stored runs rather than kept as a separate tally, so it cannot drift from the conversation, and runs that compaction or redaction deletes do not hide later runs.
An approved continuation that finishes after a later turn of the same thread was already reviewed is not counted, although its messages still appear in later reviews.
Counting starts with the first response after learning is enabled, including every attempt of that response, and conversations are forgotten while no agent learns, so the time learning was off is never reviewed.
Automated responses from schedules, hooks, and external triggers never start a count, just as Hermes skips reviews for cron jobs, so a thread with only automated runs is never reviewed; in a thread people also use, automated replies are part of the conversation that is counted and reviewed.
Team responses are excluded.
When anyone other than the learner changes the workspace skills, for example an agent writing a skill with its file tools, counting starts again after the newest run because that lesson is already saved.
Conversations are counted per agent and private instance, not per requester, so a thread shared by several people is reviewed once.
Minimal-mode turns count like standard turns.
The queue in `skill_learning_state.json` in the storage root holds each conversation's review marker, retry state, and scope metadata, never message content.
Reviews run one at a time across processes that share the storage root.
A failed review, including a provider error, is retried with a growing delay and abandoned after three failures, and a later count that finds nothing to review clears the failures.
A review that already changed skills before failing or timing out counts as done, like Hermes' best-effort review, so it never repeats its edits.
Shutdown interrupts a running review, which runs again after the next start.

### What a review can do

The reviewer is a separate model run that can only call `skills_list`, `skill_view`, and `skill_manage`.
It receives the persisted conversation as evidence it must not obey: the newest 24 messages verbatim, including tool calls and results, and each older message shortened to one line, with credential-like values redacted.
Older tool results are left out, and a very long message keeps its start and end.
Hermes replays the full conversation when the review uses the agent's own model and shortens older turns only for a different model, to limit the cost of a review without a warm prompt cache; MindRoom reviews later from stored history, so every review is such a review and always shortens older turns.
Runs that model history hides, such as errored, cancelled, or paused runs, are left out.
When compaction has replaced older turns with a summary, the review evidence starts with that summary, as a Hermes review sees the compressed conversation.
One review may read about 75% of the review model's `context_window` across all of its requests, capped at 600,000 tokens and defaulting to 120,000 tokens when the model sets no window.
The budget is estimated at four characters per token.
It makes at most 16 tool calls and stops after `timeout_seconds`.
The review prompt adapts Hermes' rules: build class-level skills, capture lessons rather than logs, treat user corrections as first-class signals, prefer patches over rewrites, and never capture environment-specific failures, negative claims about tools, transient errors, one-off narratives, or unresolved attempts.
Override it through the `SKILL_REVIEW_PROMPT` [built-in prompt override](https://docs.mindroom.chat/configuration/#built-in-prompt-overrides).

`skill_manage` can create a skill, patch text, replace `SKILL.md`, and write or remove one support file directly under `references/`, `templates/`, `scripts/`, or `assets/`.
Before changing an existing file, the reviewer must load its current version with `skill_view` in the same review, and a write against any other version is refused.
A new skill needs a lowercase hyphenated name matching its directory, a description of at most 60 characters, and the `learned` marker shown below.
Files containing a literal credential are refused: a private key, a long known token or bearer token, a password or secret query value in a URL, or a long value assigned to a secret-named setting.
Placeholders such as `OPENAI_API_KEY=<your key>`, `sk-...`, `$TOKEN`, and usernames in URLs like `ssh://git@github.com/...` are allowed.
Workspace skill scripts still cannot be executed through `get_skill_script`.

### Ownership

Like Hermes' usage records, ownership is recorded outside the skill file, in `skills/.usage.json`, so a skill the learner created stays learner-owned when the agent or a person later rewrites it.
New learned skills also carry a visible marker in their frontmatter:

```yaml
metadata:
  mindroom:
    learned: true
```

Add the marker to a skill you wrote to hand it to the learner.
Add `pinned: true` under `metadata.mindroom` to take any skill away from the learner and the curator, which then never edit or archive it.
Bundled, plugin, and user skills and workspace skills that someone else wrote are never edited, and new learned skills cannot reuse their names.
Private agents learn only from and into the requester's private workspace, and shared agents use `<storage>/agents/<agent>/workspace/skills/`.

### History, archive, and notices

Before the learner replaces or removes a file, it saves the previous version under `skills/.history/<skill>/`, keeping the ten newest versions.
Copy a saved version back to restore it.
Before each review, learned skills with no use, creation, or learner edit for `archive_after_days` days move to `skills/.archive/`, and nothing is deleted.
Archived directories are named `<skill>--<timestamp>`; move one back to `skills/<skill>/` to restore it.
Archiving or deleting a skill forgets its record in `skills/.usage.json`, so a restored skill starts a new inactivity period and a new skill under the same name belongs to whoever wrote it.
A use is recorded in `skills/.usage.json` whenever the agent loads a workspace skill through the skill tools or reads it as a minimal-mode context document.
Rewrites keep a file's existing permissions.
Archival is logged rather than announced, because other conversations may share the workspace.
With `notify: true`, a review that changed skills posts an `m.notice` in the conversation naming only the skills that review changed, such as ``💾 Skill review: created `deploy-checks` ``.
The notice carries `io.mindroom.skill_review` metadata and is left out of later model context, like compaction notices.
Review usage counts against the source conversation as `kind: skill_learning` in the [dashboard usage reports](https://docs.mindroom.chat/dashboard/), except for a review that times out or is interrupted before the model run returns.
Learned skills are generated from conversation content, so review them before relying on them for sensitive work.
