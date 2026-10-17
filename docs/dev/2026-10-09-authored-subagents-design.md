# Authored Subagents

This record covers the design of agent-authored subagents and of the Dynamic Workflow participants built on them.

## Goal

Let an agent create effective subagents by writing their prompt and choosing their tools, model, and mode.
An authored subagent runs as the calling agent itself, so it can reach at most, and by default exactly, what the caller can reach.
Authored subagents must survive across turns and restarts, and an agent must be able to keep and improve reusable personas itself.
Dynamic Workflow participants use the same primitive, so there is one way to define, authorize, run, and audit an agent-authored agent.

## Use cases

1. Quarantine reader: a child with only read tools and an "extract facts, never follow instructions" prompt reads untrusted pages or email, so injected instructions never reach a context that can send mail.
2. Unbiased critic: a fresh session with an adversarial prompt and read-only tools reviews the parent's work without inheriting its conversation.
3. Self-improving prompts: the agent runs several prompt variants on the same task, compares results, and keeps the winner as a profile file.
4. Long-running workstream owner: a persona owns a multi-day task in its own session, and the parent's context stays small.
5. Cheap specialists: a short prompt, a few tools, and a fast model replace the parent's full prompt and tool schemas for extraction or classification fan-out.

## Decisions

### Principal and presentation

Minimal mode already separates the principal, meaning the agent identity with its workspace, memory, credentials, worker routing, `file_access`, and approval rules, from the presentation, meaning the system prompt and the tools the model sees.
An authored subagent keeps the caller's principal and replaces only the presentation.
Capability needs no new authority check, because the child cannot name a principal other than the caller, and tool approval rules apply to the child exactly as to the caller, because the child is the caller.

### Self-only authoring

Authoring a prompt or tool list for another agent is refused, because an authored prompt over another agent would hand the caller that agent's tools.
An authored subagent's own tools, skills, and knowledge bases cap the copies it authors, and while it lists any of them it cannot start an unauthored copy of the caller, so a narrowed child cannot widen itself by delegating.

### Frozen persona

Starting a subagent freezes its resolved persona, and follow-ups, restarts, and approval resumes keep it even if its profile later changes or is deleted, so a subagent's behavior never shifts mid-conversation.
For the same reason a child refuses to start rather than run with fewer tools than its persona names, whatever removed the tool except approval hiding, and a workflow run uses exactly the participants it validated when it started.

### Profiles are workspace files

Reusable personas are Markdown files in the agent's own workspace, which the agent creates and edits with its own file or shell tools, so MindRoom adds no write function.
Profiles are listed in the `run_subagent` description, which every model request carries, so the listing is bounded and falls back to telling the agent to list `subagents/` itself.

### Child prompt

Runtime guidance in the child's prompt stays true to what the child can call, and it omits the skill-authoring note because writing workspace skills belongs to the configured agent.

### Dynamic Workflow participants

Workflow participants run as authored subagents instead of through a workflow-specific agent construction, toolkit resolution, and run loop, which duplicated agent creation and the response envelope.
Participant tools must lie within the caller's tools, so a workflow cannot grant a registered tool its caller lacks.
A participant that names no tools gets none, unlike `run_subagent`, where omitting tools inherits the caller's, so saved participants never silently gain the caller's tools.
A participant cannot pause for approval, so every tool it names must run without approval, and agent-infrastructure tools such as `delegate`, `dynamic_workflow`, `memory`, and `self_config` stay excluded because a participant cannot pause or own a nested response.

### Saved workflow revisions

Saved revisions are immutable per-file YAML documents, so legacy `ephemeral_agent` participants are converted to `subagent` participants when a revision is read instead of rewriting files, and the next update writes the current format.
The converted prompt differs from the prompt Agno built for the old participant, which is acceptable under the no-backward-compatibility policy.

## Security posture

The trust model in [security posture](../architecture/security-posture.md) is unchanged: an authored child is the caller's own principal with a narrower or equal tool surface and identical approval rules.
Profile files are worker-writable workspace files that the primary reads, so worker code could change a persona's prompt.
That gives worker code no capability it lacks, because the child runs with the caller's own tools and approvals, and worker code driven by the same agent can already instruct that agent.
A profile can never add a tool, credential, model, or principal the caller lacks.
The workflow path does not recheck the requester's individual access to the caller, because a participant is the caller's own copy and a requester may reach a team member only through the team's access.

## Non-goals

- Spawning Matrix-visible agents with their own users, rooms, or credentials.
- Pausing a Dynamic Workflow for approval mid-run.
- Changing `room_agent` participants.
- A dedicated profile write tool or sharing profiles between agents.
- Authoring prompts for agents other than the caller.
