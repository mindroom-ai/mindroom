# Connected Office Documents

## Problem

Users ask MindRoom agents to create Excel, PowerPoint, and Word files, then want later changes made to those same files in OneDrive or SharePoint.
Today an agent can only produce a complete replacement file, so every change means downloading, re-saving, and re-uploading, and manual edits made in Office drift away from MindRoom's copy.
Assistants embedded in Office are convenient but start a separate conversation without the thread's decisions, sources, and memory.

## Goal

A connected document is one cloud file linked to the MindRoom conversation that works on it.
The agent reads the current cloud content before each change, proposes exact targeted edits, applies them only after a human approves, verifies the result, and never overwrites content that changed after it was read.
MindRoom Chat shows connected documents as cards and renders proposed edits as a before/after review.

## Phases

1. **Connected Excel workbooks** (this design's implementation scope): Microsoft 365 sign-in, connect or save a workbook, outline and range reads, human-approved cell edits with conflict detection and verification, document cards and edit review in MindRoom Chat.
2. **MindRoom for Office**: an Office add-in task pane that opens the same thread inside Excel, sends the current selection with each message, and applies approved edits live through Office.js.
3. **Word and PowerPoint**: format-specific adapters that patch the file and upload with `If-Match`, writing Word edits as tracked changes, plus the matching Office.js adapters.

Phases 2 and 3 reuse the tool surface, card protocol, and review flow defined here.

## Microsoft 365 connection

A built-in `microsoft_365` OAuth provider uses the Microsoft identity platform v2 endpoints.
Credentials are requester-scoped: every Matrix user connects their own Microsoft account, and room membership never grants access to another person's files or lets an agent act with another person's token.
The provider requests `offline_access` and `Files.ReadWrite.All`; the latter reaches files shared with the user and SharePoint document libraries, which `Files.ReadWrite` does not.
Operators store the Entra app registration's `client_id` and `client_secret` in the `microsoft_365_oauth_client` credential service, with an optional `tenant_id`.
The tenant defaults to `organizations` because Microsoft Graph workbook sessions and several workbook features support only work or school accounts.

## Tool surface

The `microsoft_365` tool runs in the primary runtime because it holds OAuth credentials.
Its only local file access, the path uploaded by `save_office_document`, follows the agent's `file_access` setting through `resolve_agent_file`.

| Function | Effect |
| --- | --- |
| `connect_office_document(url)` | Resolve a OneDrive or SharePoint link, return the document ID and workbook outline, and post a document card. |
| `save_office_document(path, folder_url=None, name=None)` | Upload a workspace `.xlsx` as a new file, never replacing an existing one, then connect it. |
| `read_office_document(document_id, range=None)` | Return the outline, or the formulas, values, and number formats of one bounded range. |
| `edit_office_document(document_id, edits, summary, skip_conflicts=False)` | After human approval, write each edit whose current content still equals its `before` value, verify it, and post a receipt card. |

A document ID is `<drive id>:<item id>` from Microsoft Graph.
Both parts are validated against a strict character set before they appear in a request path, and every call uses the requester's token, so Graph enforces that user's own permissions.
MindRoom stores no document registry: the cloud file is authoritative, and the thread's document cards record which documents a conversation uses.

### Reads

Without a range, a read returns worksheet names and used ranges, tables with their worksheet and address, and workbook names.
With a range such as `Assumptions!B4:B8`, it returns `formulas`, `values`, and `numberFormat` for at most 2,000 cells.
Ranges are always sheet-qualified, sheet names resolve to worksheet IDs before use in a path, and addresses accept only bounded A1 cell or cell-range syntax.

### Edits

Each edit names a sheet-qualified range, the `before` formulas the agent read, and the `after` formulas to write, all as rectangular arrays matching the range.
Formulas are Graph's input representation: constants appear as themselves and formulas start with `=`, so comparing them detects human edits even when recalculation changes displayed values.
One call accepts at most 25 edits and 500 cells.

`edit_office_document` always requires human approval, like `update_own_config`, because the model issuing it may have read untrusted document content.
The existing durable tool-approval card therefore is the change review: it shows every range with its before and after values, supports approve, deny, and timed auto-approval, and survives restarts.
Agent-supplied `before` values are not trusted; they are checked against the live workbook at execution time.

Execution, after approval:

1. Read every target range's current formulas.
2. Any range whose formulas differ from `before` is a conflict.
   By default nothing is written and the result lists each conflict with its expected and current content.
   With `skip_conflicts`, only non-conflicting edits are written; this makes undo a checked reversal that preserves later human edits.
3. Write each remaining edit with a range `PATCH` of `formulas`, in order.
4. Compare each response's formulas with `after` to verify it.
5. Stop at the first failed write and report every edit as applied, conflicted, failed, or not attempted.
6. Post a receipt card and return the same receipt to the agent.

Workbook writes have no conditional request, so the check-then-write window is narrow but not locked; the design treats conflict detection as best effort and never claims a transaction.
Undo is another `edit_office_document` call with `before` and `after` swapped and `skip_conflicts` enabled.

### Saving new workbooks

`save_office_document` uploads to the user's OneDrive `MindRoom` folder, or to a folder resolved from `folder_url`.
It checks that the target name is free, uploads with `@microsoft.graph.conflictBehavior=fail`, and on conflict tries numbered names, so an existing file is never replaced.
Uploads are limited to 25 MB and to `.xlsx` in phase 1.

## Document cards

Document cards are Matrix `m.notice` events in the current thread carrying `io.mindroom.document` metadata, with a readable text body for other clients.

```json
{
  "version": 1,
  "event": "connected",
  "document_id": "b!drive:01ITEM",
  "name": "Forecast.xlsx",
  "kind": "xlsx",
  "web_url": "https://contoso.sharepoint.com/sites/finance/Shared%20Documents/FY27/Forecast.xlsx",
  "location": "Finance / FY27",
  "revision": {"etag": "\"{...},12\"", "modified_at": "2026-09-25T10:05:40Z", "modified_by": "Sam Kim"},
  "requester_id": "@sam:example.org",
  "agent_user_id": "@mindroom_analyst:example.org",
  "room_id": "!room:example.org",
  "thread_id": "$thread"
}
```

`event` is `connected`, `saved`, or `edited`.
An `edited` card adds `change` with the summary, per-edit outcomes, the changed cell count, and whether every applied edit verified.
Cards never contain access tokens or Graph preview URLs, because those act with the viewer's own permissions.
The web URL reveals a file's name and location to room members, which is inherent in connecting a document in a shared room; access still requires SharePoint permission.

## MindRoom Chat

- Document cards render with the file type, name, location, revision, change summary, verification state, and **Open in Excel** (`ms-excel:ofe|u|<web_url>`) and **Open in browser** actions.
- Approval cards for `edit_office_document` render a before/after table per range above the existing raw-argument disclosure and approval controls.
- The thread header lists the thread's connected documents from their cards.
- A committed fixture generated from the real backend toolkit pins the card contract, like the Chat UI action fixture.

## Failure handling

Graph errors reduce to a short code and scrubbed message without URLs, headers, or response bodies.
HTTP 401 prompts the requester to reconnect, 403 and 404 report missing access, 409 and 412 report conflicts, and 429 reports throttling with any `Retry-After` delay.
Every request has a byte bound and a deadline, and redirects are never followed with the bearer token.

## Testing

A fake Graph transport covers link resolution, outlines, bounded range reads, approved edits, conflicts, partial completion, verification failures, uploads that never replace files, requester isolation, and error scrubbing.
Provider tests cover tenant endpoints and requester scoping, and the Chat contract fixture comes from the real toolkit.
Live verification requires a Microsoft 365 work or school tenant with an Entra app registration.
