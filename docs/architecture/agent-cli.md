# Internal turn CLI

`mindroom-agent` is installed by the existing worker package.
Its stdlib client reads `MINDROOM_AGENT_CLI_GATEWAY_URL` and `MINDROOM_AGENT_CLI_TOKEN_PATH` from trusted worker setup.
The token identifies one response turn; arguments cannot select another requester, agent, worker, or credential owner.
Minimal mode is opt-in through `!mode <agent> minimal` after deployment and shell-permission preflight.

Discover tool names and schemas with `tools list`, `tools search`, and `tools describe`.
Listing accepts `--cursor` and `--limit`; schemas are loaded only when described.
Call input is one JSON object supplied with `--json`, `--json-file`, or `--json-stdin`.
These options are exclusive.
Omission means `{}`.

A call returns a live receipt immediately.
Exit 3 means it is pending; shell scripts must preserve that receipt and explicitly wait.
Do not chain the wait with success-only `&&`:

```bash
# TOOLKIT and FUNCTION come from discovery; arguments.json follows describe.
status=0
receipt=$(mindroom-agent tools call "$TOOLKIT" "$FUNCTION" --json-file arguments.json) || status=$?
case "$status" in
  0|3) printf '%s\n' "$receipt" ;;
  *) printf '%s\n' "$receipt"; exit "$status" ;;
esac
call_id=$(printf '%s\n' "$receipt" | jq -r .call_id)
mindroom-agent calls wait "$call_id"
```

Within the same live response, `--call-id <UUID>` permits retrying the same qualified function and canonical arguments.
The retry returns that existing call, including its observed result; changed arguments or function reject with 409.
A lost HTTP reply does not cancel accepted work.
Transport errors retain the submitted call ID; a fresh ID could invoke the operation again.

Receipts contain call ID, toolkit, function, status, parent Bash call ID, and outcome.
They are response-owned memory, not journal rows.
No call timestamp, lease, database generation, or result-reference fields are exposed.
When the response ends or its process dies, its grants expire/revoke and ordinary call polling becomes unavailable.
Queued work and unsaved results are lost; effects of interrupted calls may be unknown.
Live status can be queued, running, waiting, completed, failed, or cancelled.

Approval recovery belongs to the existing native approval continuation owner: waiting/ready confirmations can be recovered, while a claimed attempt settles through the existing final-or-interruption behavior.
The recovery adapter never directly executes a saved Bash command.
Existing response failure and automatic model-resumption policy remain unchanged; fresh model calls are not a semantic deduplication guarantee.
Approval suspension uses the actual provider-batch checkpoint.

Exit codes: 0 completed/read success; 1 tool failure/denial/cancellation; 2 invalid input; 3 queued/running/waiting; 4 unavailable authority or unknown transport/dispatch outcome.
`--help` needs no runtime configuration.

The existing API exposes only `POST /api/agent-cli/operations` and `GET /api/agent-cli/calls/{call_id}` for this capability.
The deployment gateway must restrict forwarding to those routes.
Authentication precedes body parsing; invalid authority and another owner's receipt both return the same 401 response.
The orchestrator owns the registry.
The response owns the worker, call admission, checkpoint and cleanup across model continuations; HTTP never invokes an Agent.
A response lease keeps its worker alive across worker-manager replacement.
Normal completion retires that exact worker.
After a primary crash, old grants are invalid immediately; the existing heartbeat and idle-timeout cleanup stop abandoned workers.

A Bash window stays open while its admitted calls settle, so an admitted nested shell can still submit its child calls after the outer worker returns.
Admission closes atomically when that work is quiescent, before hooks or model control resume.
Calls arriving during drainage may join that Bash; new calls and describe requests after closure are rejected until a later Bash call opens a window.
Revocation and explicit control cancellation fence admission immediately.
An admitted describe request settles when its task ends, including cancellation before the task starts.
