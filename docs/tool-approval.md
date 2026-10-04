---
icon: lucide/shield-check
---

# Tool Approval

<video controls playsinline preload="metadata" aria-label="An agent pauses for approval before it books a meeting" style="width: 100%">
  <source src="https://github.com/user-attachments/assets/6a2033ea-3354-4afd-9d58-fc617b18cd24#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/d62d98e8-c066-4e1f-8a8f-d840da7b0bd1#t=0.1" type="video/mp4">
</video>

Use the top-level `tool_approval` block to gate tool calls behind human approval in Matrix conversations.
This example gates Slack message sending and file uploads, plus shell calls selected by a review script:

```yaml
tool_approval:
  default: auto_approve
  timeout_days: 7
  rules:
    - match: send_message*
      action: require_approval
    - match: upload_file
      action: require_approval
    - match: run_shell_command
      script: ./approval_scripts/shell_review.py
      timeout_days: 3
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `default` | `auto_approve` or `require_approval` | `auto_approve` | Decision for calls that no rule matches |
| `timeout_days` | number | `7` | How long an approval card stays open before it expires |
| `rules` | list | `[]` | Ordered rules; the first matching rule wins |
| `rules[].match` | string | Required | Case-sensitive glob over exposed function names, not toolkit identifiers, so the same function name in another toolkit also matches |
| `rules[].action` | `auto_approve` or `require_approval` | — | Fixed decision; set exactly one of `action` or `script` |
| `rules[].script` | string | — | Config-relative Python file defining `check(tool_name, arguments, agent_name) -> bool`; approval is required only when it returns `True` |
| `rules[].timeout_days` | number | `tool_approval.timeout_days` | Expiry window for this rule |

## Approving and Denying

- React to the approval card with `✅` to approve the tool call.
- Reply to the card to deny the call; the start of the reply is recorded as the denial reason.
- Only the original human requester can approve or deny their pending call.
- Eligible cards also offer auto-approval for 5, 10, or 30 minutes.
  It covers the original call, matching pending calls, and later calls for the same room, thread, requester, agent, and exact tool operation, with any arguments.
  It needs a thread and complete reviewable arguments, and is not offered for native tool-authored confirmations or background-script approvals.
  The requester can stop auto-approval from the originating card, and changes to configured bindings or room membership end matching grants; calls already approved still run.

While approval is pending, the agent stops typing and the conversation can continue.
Pending approvals survive restarts and config reloads, and an approved call resumes the paused response.
The approved call runs only with the exact arguments shown on the card.
Tool calls authored by agents, the system, or configured bridge bots are denied instead of entering the approval flow.
An agent acting on a request relayed by another agent's reply asks the original human for approval.
See [Agents](#when-the-router-is-missing) when approval fails because the router is not in the room.

## Approval Card Contents

Cards show a redacted preview of the tool arguments, and clients can expand the complete redacted arguments when the preview is truncated.
Fields whose names mark them as secret, such as `password`, `credentials`, or an `Authorization` header, are hidden while their field name stays visible.
Inside text, only credentials in known token formats, such as `sk-…`, `ghp_…`, `github_pat_…`, `xoxb-…`, `AIza…`, and JSON Web Tokens, are replaced by numbered placeholders like `⟦secret-1⟧`; equal tokens get the same number.
Other inline secrets, such as `export DB_PASSWORD=hunter2` or an inline `Authorization: Basic …` header, stay visible, so keep secrets in credentials or secret-named fields.
Deny the call when a hidden field or a placeholder where a command, host, path, or delimiter belongs could change what runs.
A card cannot be approved when its complete redacted arguments exceed 2 MB, nest deeper than 32 levels, or fail to upload; approving it then denies the call.

## Tools That Cannot Pause for Approval

The OpenAI-compatible `/v1/chat/completions` API has no approval transport, so it hides every tool function that matches a required-approval rule, including script rules.
Skill, knowledge-search, and learning functions such as `get_skill_script`, `search_knowledge_base`, and `update_user_memory` cannot pause for approval on any channel, so they are hidden when a required-approval rule or `default: require_approval` applies to them.
Add an `auto_approve` rule for such a function to keep it available.

## When the Router Is Missing

Approval-gated tools are stricter than plain ad-hoc chat access.
When approval needs a missing router, the agent can call `invite_router` to invite it into the current room and then retry.
The tool waits briefly for joined membership and, if the invite remains pending, tells the agent to retry only after the router joins.
The router accepts and persists that internal invite when `router.accept_invites` allows the current Matrix transport account's user ID.
For a team execution, that identity is the team's Matrix account rather than the member agent's account.

Every concrete Matrix agent operating in a room also receives a built-in zero-argument `invite_router` recovery tool.
The tool can invite only the persisted router identity and only into the agent's current room.
The router accepts the invite and persists the room only when `router.accept_invites` allows the current Matrix transport account's user ID.
A team member therefore authorizes recovery through the team's Matrix account, not the member agent's account.
The recovery tool waits briefly for joined membership and reports a pending state when the router has not joined yet.
This lets an agent recover router-backed approvals without adding persistent prompt instructions or exposing arbitrary invite targets.
