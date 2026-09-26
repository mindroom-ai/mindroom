# Microsoft 365

The `microsoft_365` tool connects OneDrive and SharePoint Excel workbooks to a conversation and edits them in place.
Each requester connects their own work or school Microsoft account, so agents see and change only the files that person can open.
Every edit is reviewed and approved by a human before it is written, and the tool refuses to overwrite cells that changed after the agent read them.
MindRoom owns the OAuth state, callback, token refresh, and credential storage through its [OAuth framework](https://docs.mindroom.chat/oauth-framework/).

## What It Does

| Function | Changes data | Purpose |
| --- | --- | --- |
| `connect_office_document` | no | Resolve a OneDrive or SharePoint workbook link, return its outline, and post a document card in the conversation. |
| `save_office_document` | yes | Upload a workspace `.xlsx` as a new file in the requester's OneDrive `MindRoom` folder or a linked folder, then connect it. |
| `read_office_document` | no | Return a workbook outline, or the formulas, values, and number formats of one range. |
| `edit_office_document` | yes | After human approval, write cell edits whose ranges still hold the content the agent read, and post a receipt card. |

Phase 1 supports Excel `.xlsx` workbooks only.
Every result is a JSON object with `status` set to `ok` or `error`.

### Connected documents

`connect_office_document` accepts any OneDrive or SharePoint link to a workbook, such as one copied from Excel's **Share** or **Copy link** command.
It returns a `document_id` of the form `<drive id>:<item id>` that later calls use.
The document card it posts carries the same ID, so the agent can keep working on the same file later in the thread.
MindRoom stores no copy of the workbook: every read and edit goes to the live cloud file, so manual changes made in Excel are always visible to the next request.

An outline lists up to 100 worksheets with the used range of up to 20 visible sheets, up to 20 tables with their ranges, and up to 100 visible workbook names.
A range read returns at most 2,000 cells from a sheet-qualified A1 range such as `Assumptions!B4:B8` or `'Summary Sheet'!A1:D20`.

### Reviewed edits

Each edit names a range, its `before` formulas as read, its `after` formulas, and an optional `number_format`, all as grids matching the range:

```json
{
  "document_id": "b!abc:01XYZ",
  "summary": "Raise growth to 12%",
  "edits": [
    {"range": "Assumptions!B4", "before": [[0.08]], "after": [[0.12]], "number_format": [["0%"]]}
  ]
}
```

`edit_office_document` always requires approval, even when `tool_approval.default` is `auto_approve`, because the agent issuing it may have read untrusted document content.
The approval card shows every range with its before and after content, and each call needs its own approval.
In MindRoom Chat, the card renders the target document ID and a before/after table of changed cells; for very large edits whose arguments move to a separate attachment, Chat shows the raw arguments instead.
A replayed approval whose edits had already landed posts no second card.
Where native approval is unavailable, such as the OpenAI-compatible API, the edit function is not offered.

After approval, the tool reads every range again.
A range whose content differs from `before` is a conflict: nothing is written, and the result shows the current content so the agent can read again and propose new edits.
With `skip_conflicts: true`, only ranges that still match are written, which makes undo safe: swap `before` and `after` of the applied edits, and cells a person changed since are kept.
A range that already holds `after` counts as applied without writing again, so a replayed approval never writes twice.
Writes run one at a time, stop at the first failure, and verify each write against the content Graph reports it stored.
After a failed or timed-out write, the tool reads the range again; when that read also fails, the result reports an unknown outcome instead of claiming nothing was written.

Formulas start with `=`, and constants are written as themselves.
Excel parses written text as if typed, so text such as `0042`, `12%`, `1/2`, `June 2024`, or `TRUE` would silently become a number, date, or boolean.
The tool rejects such text unless that cell's `number_format` is `@`; write numbers as JSON numbers, and dates as serial numbers with a date format.
In `number_format`, `null` keeps a cell's current format.
Integers beyond 2^53 are rejected because Excel stores numbers as doubles; write long identifiers as text.
Use `""` to clear a cell.
Formulas compare without regard to case outside string literals, because Excel stores `=sum(a1:a2)` as `=SUM(A1:A2)`.

Microsoft Graph offers no conditional write for workbook ranges, so conflict detection narrows the window for a simultaneous change but cannot close it.

| Limit | Value |
| --- | --- |
| Cells per read | 2,000 |
| Edits per call | 25, non-overlapping |
| Cells per call | 500 |
| Cell text per call | 200,000 characters |
| Upload size | 25 MB |

### Saving new workbooks

`save_office_document` uploads a workbook the agent created in its workspace, for example with Python.
The path follows the agent's [`file_access`](https://docs.mindroom.chat/architecture/security-posture/) setting.
By default the file lands in a `MindRoom` folder in the requester's OneDrive, created when missing; `folder_url` selects another OneDrive or SharePoint folder.
An existing file is never replaced: the tool picks the first free name among `Name.xlsx`, `Name (2).xlsx`, and so on, and uploads through a Graph upload session whose `fail` conflict behavior refuses a name taken in the meantime, moving on to the next candidate.
The upload session URL is preauthenticated by Microsoft and never receives the bearer token.

## Set Up The Entra App

An administrator registers one Microsoft Entra app for the MindRoom installation.

1. In the [Microsoft Entra admin center](https://entra.microsoft.com/), open **App registrations** and choose **New registration**.
2. Choose **Accounts in any organizational directory** to let several organizations connect, or **Accounts in this organizational directory only** for one organization.
3. Add a **Web** redirect URI `https://mindroom.example.com/api/oauth/microsoft_365/callback`, replacing the origin with your public MindRoom origin.
4. Under **Certificates & secrets**, create a client secret and copy its value.
5. Under **API permissions**, add the delegated Microsoft Graph permissions `Files.ReadWrite.All` and `offline_access`.
6. Choose **Grant admin consent**, or have each customer tenant's administrator consent once, because many tenants block user consent for `Files.ReadWrite.All`.

For a local installation, the redirect URI is `http://localhost:8765/api/oauth/microsoft_365/callback`.
MindRoom derives callback URLs from `MINDROOM_PUBLIC_URL` or `MINDROOM_BASE_URL`, so set one of them for a hosted installation.

Store the application (client) ID and secret in the `microsoft_365_oauth_client` credential service through the dashboard credentials page.
For non-interactive deployments, seed it at startup with a [credential seed](https://docs.mindroom.chat/configuration/#credential-seeds):

```json
[
  {
    "service": "microsoft_365_oauth_client",
    "credentials": {
      "client_id": {"env": "MICROSOFT_365_CLIENT_ID"},
      "client_secret": {"env": "MICROSOFT_365_CLIENT_SECRET"}
    }
  }
]
```

MindRoom signs in through the `organizations` endpoint, which requires the multi-tenant option.
For a single-tenant app, set `MICROSOFT_365_TENANT_ID` to the directory (tenant) ID or a verified domain such as `contoso.onmicrosoft.com`.
Personal Microsoft accounts are not supported, because the Excel REST API works only with work or school storage.

## Configure

Add the tool to an agent:

```yaml
agents:
  analyst:
    tools:
      - microsoft_365
```

The tool has no options.
It runs in the primary runtime, because it holds OAuth credentials.

## Connect An Account

When a requester has not connected Microsoft 365 yet, every function returns a structured result with `oauth_connection_required: true` and a `connect_url` for that requester.
The requester opens the link, signs in, approves the requested permissions, and retries the request.
Users can also connect and disconnect from the dashboard Integrations page.

Tokens are stored per requester in that person's `user` credential scope and never in a shared or global scope.
A call without a concrete requester always asks for a connection instead of using another account.
When Microsoft requires a new sign-in, for example after a Conditional Access policy asks for fresh multi-factor authentication, the tool asks the requester to reconnect.

## Document Cards

The tool posts document cards as `m.notice` events in the current thread, with a readable text body for any Matrix client.
The `io.mindroom.document` metadata identifies the document, its location, and the revision Microsoft Graph reported:

```json
{
  "version": 1,
  "event": "edited",
  "document_id": "b!abc:01XYZ",
  "name": "Forecast.xlsx",
  "kind": "xlsx",
  "web_url": "https://contoso.sharepoint.com/sites/finance/_layouts/15/Doc.aspx?sourcedoc=...",
  "file_url": "https://contoso.sharepoint.com/sites/finance/Shared%20Documents/FY27/Forecast.xlsx",
  "location": "FY27",
  "revision": {"etag": "\"{...},4\"", "modified_at": "2026-09-25T10:06:12Z", "modified_by": "Sam Kim"},
  "requester_id": "@sam:example.org",
  "agent_user_id": "@mindroom_analyst:example.org",
  "room_id": "!room:example.org",
  "thread_id": "$thread",
  "change": {
    "summary": "Raise growth to 12%",
    "status": "applied",
    "verified": true,
    "cells_changed": 1,
    "edits": [{"range": "Assumptions!B4", "outcome": "applied", "cells_changed": 1, "verified": true}]
  }
}
```

`event` is `connected`, `saved`, or `edited`; only `edited` cards carry `change`.
Cards list each edit's range and outcome without cell contents; the agent's result carries the current and written cells.
`web_url` opens the browser editor, and `file_url` is the path Office desktop apps open, such as `ms-excel:ofe|u|<file_url>`.
MindRoom Chat renders these cards with **Open in Excel** and **Open in browser** actions.

## Privacy And Security

- Connecting a document in a shared room shows its name and location to room members, and edit approval cards show the cells being changed; SharePoint permissions still govern who can open the file.
- Approval previews redact values that look like secrets, and MindRoom Chat marks redacted cells.
- Cards never carry access tokens or Graph preview links.
- The bearer token is sent only to `https://graph.microsoft.com`, and redirects are never followed.
- Graph errors are reduced to a code and a message without URLs or response bodies.
