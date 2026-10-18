# Internal turn CLI

This page records why the `mindroom-agent` CLI, used by [minimal mode](../tools/agent-cli.md) and by standard-mode shells, is owned by one response turn, and what that ownership means for recovery.
Commands, exit codes, and deployment requirements are on the user page.

## Ownership

The CLI adds no execution boundary of its own: `mindroom-agent` runs in the agent's own shell, wherever that shell already runs, so the worker remains the only isolation described in the [security posture](security-posture.md).
Its only authority is a grant that identifies one response turn, so no argument can select another requester, agent, worker, or credential owner.
Operations execute inside the response that owns the grant, never in the HTTP handler, so a call has exactly the authority, approvals, and lifetime of the response that made it.

## Recovery

Grants and call receipts exist only in the response's memory.
When the response and its background jobs end, or its process dies, its grant is revoked, call polling becomes unavailable, queued work and unsaved results are lost, and the effects of interrupted calls may be unknown; after a primary crash, old grants are invalid immediately.

Through the native approval continuation, waiting and ready confirmations can be recovered, while an attempt already claimed settles through the ordinary final-or-interruption behavior.
Recovery never directly executes the saved Bash command that requested an approval.
Minimal mode keeps the ordinary response-failure and automatic model-resumption policy, and a fresh model call is not a semantic deduplication guarantee.
