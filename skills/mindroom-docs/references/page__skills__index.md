# Skills

Skills are instruction packs: a `SKILL.md` file with optional scripts and reference documents that guide an agent through a task without adding new code capabilities.
MindRoom uses Agno's skills system with OpenClaw-compatible metadata.
This page covers writing, installing, and allowlisting skills, skills agents keep in their own workspace, and opt-in automatic skill learning.

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

| Field | Type | Description |
| --- | --- | --- |
| `name` | string | Unique skill identifier; defaults to the skill directory name |
| `description` | string | Brief summary shown to users and models; defaults to the skill name when omitted or blank |
| `metadata` | mapping or JSON5 string | OpenClaw metadata (see [Eligibility gating](#eligibility-gating-openclaw-metadata)) and custom fields |
| `license` | string | Informational only; accepted but not used by the runtime |
| `compatibility` | string | Informational only; accepted but not used by the runtime |
| `allowed-tools` | list | Reserved; accepted but not enforced by the runtime |

A skill without any frontmatter still loads with the name and description fallbacks, but frontmatter gives clearer listings.
Keep each skill focused on one capability, give it a descriptive name such as `code-review`, and declare its dependencies with `metadata.openclaw.requires`.

Workspace skill frontmatter must not use YAML aliases or unusually large or deeply nested structures.
A workspace skill that breaks these limits is skipped with a warning, while bundled, plugin, and `~/.mindroom/skills` skills are parsed like any YAML.

## Installing and managing skills

Install a user skill at `~/.mindroom/skills/<name>/SKILL.md`, or use the dashboard Skills page to create, view, edit, and delete user skills.
Bundled and plugin-provided skills are visible but read-only in the dashboard.

An agent can use a bundled, plugin, or user skill only after you add its name to the agent's `skills:` allowlist in `config.yaml`:

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

If `skills` is empty or unset, the agent gets no bundled, plugin, or user skills.
Skills in the agent's own workspace load without being listed; see [Workspace skills](#workspace-skills).

## Skill locations and precedence

MindRoom resolves skills for each agent from these locations, in this order:

1. Bundled skills shipped with MindRoom
2. Plugin-provided skill directories (see [Plugins](https://docs.mindroom.chat/plugins/))
3. User skills: `~/.mindroom/skills/`
4. Agent workspace skills: `<resolved workspace>/skills/`

For agents without `private`, the workspace skills directory is `<storage>/agents/<agent>/workspace/skills/`.
Private instances use `<storage>/private_instances/<scope-directory>/<agent>/<private.root>/skills/`; see [Private Instances](https://docs.mindroom.chat/configuration/agents/#private-instances).

If several skills share a name, the later location wins: agent workspace over user, user over plugin, and plugin over bundled.

## Workspace skills

Workspace skills are available only to the owning agent or private instance.
They do not appear in the dashboard Skills page or the skills API, because those views are not agent-scoped.
Hidden entries such as `.usage.json`, `.history/`, and `.archive/` are never loaded as skills.

Agents never need write access to a global skill root to create skills, so the bundled, plugin, and user roots can stay read-only, for example in container or Kubernetes deployments.
An agent with a workspace and workspace-rooted file or shell tools can write a skill to `<workspace>/skills/<skill-name>/SKILL.md`, which its file tools address as `skills/<skill-name>/SKILL.md`.
Such agents are told how in their system prompt through the `WORKSPACE_SKILL_AUTHORING_PROMPT` built-in prompt, which you can change through [Built-In Prompt Overrides](https://docs.mindroom.chat/configuration/#built-in-prompt-overrides).
A new or edited workspace skill is picked up on the agent's next run without a config change.

Being able to write workspace skills does not make an agent create them.
To have agents save reusable workflows on their own, add that guidance to the agent's instructions, or enable [automatic skill learning](#automatic-skill-learning).

MindRoom loads at most 256 workspace skills per agent, skips a skill whose `SKILL.md` exceeds 1 MiB or whose name exceeds 64 characters, and truncates descriptions to 1,024 characters, logging a warning in each case.

## Eligibility gating (OpenClaw metadata)

If `metadata.openclaw` is present, MindRoom loads the skill only when these rules pass:

- `os: ["linux", "darwin", "windows"]`: the current OS must be listed
- `always: true` bypasses `requires`, but not an OS mismatch
- `requires.env`: each environment variable is set, or a stored credential with that key exists
- `requires.config`: each config path is truthy, for example `agents.code.tools`
- `requires.bins`: all binaries exist in `PATH`
- `requires.anyBins`: at least one binary exists in `PATH`

Skills without `metadata.openclaw` are always eligible.

## Using skills at runtime

Agents see their available skills in the system prompt and load details with these tools:

- `get_skill_instructions(skill_name)` loads the full instructions for a skill
- `get_skill_reference(skill_name, reference_path)` reads a reference document
- `get_skill_script(skill_name, script_path, execute=False, args=None, timeout=30)` reads or executes a script

Workspace skill scripts can be read with `get_skill_script`, but not executed through it.
Agents with shell or file execution tools can still run workspace files through those tools.

Added, removed, or edited `SKILL.md` files take effect on the agent's next request, within about a second.
A workspace skill created during a turn becomes available on the next agent run, not in the same response.

## Automatic skill learning

Automatic skill learning follows the self-improvement loop of [Hermes Agent](https://github.com/NousResearch/hermes-agent).
After enough work in a conversation, a background review maintains a small library of class-level skills in the agent's workspace, and unused learned skills are archived.
It is opt-in for each agent:

```yaml
agents:
  assistant:
    display_name: Assistant
    skill_learning:
      enabled: true
      model: null  # null reviews on the response's own model and reuses its prompt cache
      review_interval: 10
      timeout_seconds: 120
      notify: true
      archive_after_days: 30
```

Hosted MindRoom instances enable it for the agents of a newly provisioned instance; set `agents.<name>.skill_learning.enabled: false` to turn it off.
Self-hosted configurations from `mindroom config init` leave it off.
Reviews incur additional model usage, reported under `kind: skill_learning` in [usage tracking](https://docs.mindroom.chat/usage/#token-usage).
Learned skills are generated from conversation content, so review them before relying on them for sensitive work.

### Settings

All `agents.<name>.skill_learning` fields are optional, and unknown fields are rejected.

| Field | Type | Default | Bounds and behavior |
|---|---|---|---|
| `enabled` | boolean | `false` | Count completed standalone responses, review conversations that reach the interval, and offer the `skill_manage` tool. |
| `model` | string or null | `null` | Review model alias from `models`; null reviews on the model the response used and reuses its prompt cache, while another model replays a digest of the conversation. |
| `review_interval` | integer | `10` | 1–1000 model replies per conversation between reviews, counting each tool-calling step. |
| `timeout_seconds` | integer | `120` | 10–900 seconds per review. |
| `notify` | boolean | `true` | Post an `m.notice` in the conversation when a review changes skills. |
| `archive_after_days` | integer | `30` | 0–3650 days with no use, creation, or `skill_manage` edit before a learned skill is archived; `0` keeps learned skills indefinitely. |

### When reviews run

Each conversation keeps a count of model replies: one for each tool-calling step and one for the final answer.
Only successful standalone-agent responses to a person in Matrix count.
Team responses, responses another agent asked for, scheduled, hook, and external-trigger responses, responses that resume after an approval, and runs resumed after a restart never count.
A response that calls `skill_manage` restarts the count from its last call, except in minimal mode, where `skill_manage` runs through the command line.
Once the count reaches `review_interval`, it restarts and the review starts in the background, so it never delays a reply.
A new response in the same conversation stops a running review.
Counts are per agent or private instance and conversation, not per requester, so a thread shared by several people is reviewed once.
Counts live in memory, so a restart starts every count over, and a review that fails or is stopped is not retried.

### What a review sees

By default, the review resends the final model request of the response that made the conversation due, on the same model with the review prompt appended.
The provider can serve it from its prompt cache, and the review sees the conversation verbatim, including tool calls and results.
This request is sent unredacted, because it is the request the same provider just received.

The review instead replays a digest of the stored conversation on a separate request when `skill_learning.model` names another model, the agent runs in minimal mode, or the final request cannot be reused or is too large for the review's input budget.
A digest shows the newest 24 messages verbatim and older messages shortened, leaves out older tool results, and redacts credential-like values on a best-effort basis.
Without its own `model`, a digest replay runs on the model the response used.

A review's input budget scales with the review model's `context_window`.
Set `context_window` on the agent's model so long conversations can still be reviewed verbatim; without it, a final request over about 30,000 input tokens is replayed as a digest.
A review makes a bounded number of tool calls and stops after `timeout_seconds`.

Only the skill tools run during a review; any other tool the agent has answers that it is not available, and scripts never run.
Provider-hosted tools, such as a provider's web search, stay available because the provider runs them.
The review prompt asks for class-level skills that capture lessons rather than logs, treats user corrections as first-class signals, prefers patches over rewrites, and excludes environment-specific failures, transient errors, one-off narratives, and unresolved attempts.
Override it through the `SKILL_REVIEW_PROMPT` [built-in prompt override](https://docs.mindroom.chat/configuration/#built-in-prompt-overrides).

### The skill_manage tool

Agents with skill learning enabled get a `skill_manage` tool, and the review writes skills with the same tool.
Other agents can list `skill_manage` in `tools` to save skills from chat.
Its actions are `create` a skill from a full `SKILL.md`, `patch` text in `SKILL.md` or a support file, `edit` to replace `SKILL.md`, and `write_file` or `remove_file` for one support file directly under `references/` or `scripts/`.
It cannot delete a skill; unused learned skills are archived, and you remove any skill by deleting its directory.

In chat, `skill_manage` can change any workspace skill, and a skill it creates belongs to the person, not the learner.
Configured bundled, plugin, and user skills are read-only.
Tool approval rules apply to chat calls as to any tool.
`skill_manage` works only on skills in their own `skills/<name>/` directories, so a workspace whose `skills/SKILL.md` makes the whole directory one skill gets no new skills or edits until that file moves into `skills/<name>/`.

A new skill needs a lowercase hyphenated name matching its directory and a description of at most 60 characters.
Files that contain a likely literal credential are refused with the offending line named: a quoted API key, token, secret, or password of at least 20 characters that does not name an environment variable, a private key header, or a GitHub, OpenAI, Anthropic, AWS, or GitLab token.
This check is a heuristic for common formats, not a guarantee.

### Ownership

The review edits only learned skills, never bundled, plugin, or user skills or workspace skills someone else wrote, and its new skills cannot reuse their names.
Ownership is recorded in `skills/.usage.json`, so a learned skill stays learner-owned when the agent or a person later rewrites it.
Skills the review creates also carry a visible marker in their frontmatter:

```yaml
metadata:
  mindroom:
    learned: true
```

Add this marker to a skill you wrote to hand it to the learner.
Add `pinned: true` under `metadata.mindroom` to any skill to keep the review and archival from ever editing or archiving it.
Private agents learn only from and into the requester's private workspace, and shared agents use `<storage>/agents/<agent>/workspace/skills/`.

### History, archive, and notices

Before `skill_manage` replaces or removes a file, in chat or in a review, it saves the previous version under `skills/.history/<skill>/`, keeping the ten newest versions.
Copy a saved version back to restore it.

Before each review, learned skills with no use, creation, or `skill_manage` edit for `archive_after_days` days move to `skills/.archive/<skill>--<timestamp>`; nothing is deleted.
Move a directory back to `skills/<skill>/` to restore it.
Archiving a skill forgets its ownership record, and a deleted skill's record is forgotten at the next review, so a skill restored or recreated after that starts a new inactivity period.
A restored learned skill still carries `learned: true` and stays learner-owned; add `pinned: true` to keep the learner from editing or archiving it.
Loading a workspace skill or one of its files through the skill tools counts as a use, also for agents without skill learning.
Minimal-mode agents read skills through their command line, which records no use, so a learned skill used only in minimal mode is archived after `archive_after_days`.
Archival is logged rather than announced, because other conversations may share the workspace.

With `notify: true`, a review that changed skills posts an `m.notice` in the conversation naming those skills, such as ``💾 Skill review: created `deploy-checks` ``.
The notice is also posted when a new response stopped the review after its changes landed, and it is left out of later model context.
Review log lines carry `kind: skill_learning`, so you can filter logs for them.
