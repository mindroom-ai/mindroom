---
icon: lucide/shield-check
---

# Tool Approval

<video controls playsinline preload="metadata" aria-label="An agent pauses for approval before it books a meeting" style="width: 100%" poster="https://github.com/user-attachments/assets/444fee69-b77f-4c48-b7d1-b92043159bb3" data-poster-light="https://github.com/user-attachments/assets/444fee69-b77f-4c48-b7d1-b92043159bb3" data-poster-dark="https://github.com/user-attachments/assets/94181525-4b2b-43b4-b366-d85aa18f59b8">
  <source src="https://github.com/user-attachments/assets/f3792ec9-ea73-4169-97ee-d09146cd65cc" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/cf813bb1-5f9d-43b2-a197-b4749696a86e" type="video/mp4">
</video>

Use the top-level `tool_approval` block to make chosen tool calls wait for a person to approve them in the Matrix conversation.
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
| `timeout_days` | number, greater than 0 and at most 36500 | `7` | How long an approval card stays open before it expires |
| `rules` | list | `[]` | Ordered rules; the first matching rule wins |
| `rules[].match` | string | Required | Case-sensitive glob over exposed function names, not toolkit identifiers, so the same function name in another toolkit also matches |
| `rules[].action` | `auto_approve` or `require_approval` | — | Fixed decision; set exactly one of `action` or `script` |
| `rules[].script` | string | — | Config-relative Python file defining `check(tool_name, arguments, agent_name) -> bool`, which may be `async`; approval is required only when it returns `True` |
| `rules[].timeout_days` | number, greater than 0 and at most 36500 | `tool_approval.timeout_days` | Expiry window for this rule |
| `scheduled_any_arguments` | boolean | `true` | Let a requester approve a scheduled tool call for any arguments to the same tool, not only the exact arguments; see [Pre-Approved Tool Calls](scheduling.md#pre-approved-tool-calls) |

## Approving and Denying

- React to the approval card with `✅` to approve the tool call.
- Reply to the card to deny the call; the start of the reply is recorded as the denial reason.
- Only the original human requester can approve or deny their pending call.
  When an agent acts on a request relayed by another agent's reply, the original human is asked.
- Tool calls requested by agents, the system, or configured bridge bots are denied instead of waiting for approval.
- An unanswered card expires after its `timeout_days` and the call is denied.
- A gated call can also be approved once, ahead of time, when an agent schedules it; see [Pre-Approved Tool Calls](scheduling.md#pre-approved-tool-calls).

While approval is pending, the agent stops typing and the conversation can continue.
Pending approvals survive restarts and config reloads, and an approved call resumes the paused response.
The approved call runs only with the exact arguments shown on the card.

### Auto-Approval for a Few Minutes

Eligible cards also offer auto-approval for 5, 10, or 30 minutes.
It covers the original call, matching pending calls, and later calls for the same room, thread, requester, agent, and exact tool operation, with any arguments.
It is offered only in a thread and when the card shows the complete arguments, and never for tools that request confirmation themselves, background-script approvals, or scheduled calls.
The requester can stop auto-approval from the originating card, and any config change or room membership change ends matching grants; calls already approved still run.

## Approval Card Contents

Cards show a redacted preview of the tool arguments, and clients can expand the complete redacted arguments when the preview is truncated.
Fields whose names mark them as secret, such as `password`, `credentials`, or an `Authorization` header, are hidden while their field name stays visible.
Inside text, only credentials in known token formats, such as `sk-…`, `ghp_…`, `github_pat_…`, `xoxb-…`, `AIza…`, and JSON Web Tokens, are replaced by numbered placeholders like `⟦secret-1⟧`; equal tokens get the same number.
Other inline secrets, such as `export DB_PASSWORD=hunter2` or an inline `Authorization: Basic …` header, stay visible, so keep secrets in credentials or secret-named fields.
Deny the call when a hidden field or a placeholder where a command, host, path, or delimiter belongs could change what runs.

A card cannot be approved when its arguments exceed 2 MB, nest deeper than 32 levels, or fail to upload; approving it denies the call with `Cannot approve: the tool arguments cannot be shown in full`.
Have the agent retry with a smaller payload, such as saving large content to a workspace file or sending it as an attachment, or auto-approve the tool with a script rule.

## Tools That Cannot Pause for Approval

The [OpenAI-compatible API](openai-api.md) has no approval cards, so it hides every tool function that matches a required-approval rule, including script rules.
Skill, knowledge-search, and learning functions such as `get_skill_script`, `search_knowledge_base`, and `update_user_memory` cannot pause for approval on any channel, so they are hidden when a required-approval rule or `default: require_approval` applies to them.
Add an `auto_approve` rule for such a function to keep it available.

## When the Router Is Missing

Approval cards need the router in the room, so a gated call in a room without the router fails with `Tool approval needs the router in this room.`
Every agent in a room has a built-in `invite_router` tool that invites the router into the current room, waits briefly for it to join, and reports when the invite is still pending so the agent retries after the router joins.
The router accepts that invite only when its [`router.accept_invites`](configuration/router.md) allows the inviting agent's Matrix account; for a team, that is the team's account rather than the member agent's.
Otherwise enable that setting or add the router to the room manually, then retry.
