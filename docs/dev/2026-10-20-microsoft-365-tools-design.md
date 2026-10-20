# Microsoft 365 Tools Design

This record explains how MindRoom's Microsoft 365 tools reach OneDrive and SharePoint files, Outlook mail and calendar, Microsoft To Do, and Teams through Microsoft Graph.

## Problem

MindRoom ships Google Drive, Docs, Sheets, Calendar, Tasks, and Gmail tools, but nothing for Microsoft 365.
Agno ships no Microsoft toolkit, so every Microsoft tool is MindRoom code.
Microsoft's Work IQ MCP server can read most Microsoft 365 data, but its writes are JSON operations only, and the upload URLs it hands out no longer carry the `tempauth` credential, so an agent cannot save a file through it.

## Decisions

- **One provider per service, one app registration.**
  `microsoft_files`, `outlook_mail`, `outlook_calendar`, `microsoft_todo`, and `microsoft_teams` are separate OAuth providers with their own scopes and tokens, and all of them read the client ID and secret from the shared `microsoft_oauth_client`, as the Google providers share `google_oauth_client`.
  Users connect only the services they use, and administrators consent per permission; a single provider with the union of scopes would ask every user for `Mail.Send` and `ChannelMessage.Send` before they read a file.
- **Requester-scoped tokens.**
  These services hold a person's mail, calendar, chats, and files, so every token is stored for the requester, as GitHub and Atlassian do, and an agent never falls back to a shared or installation-level Microsoft account.
- **Tenant from the environment.**
  The authorize and token endpoints use `MICROSOFT_TENANT_ID`, default `organizations`, resolved per runtime.
  Single-tenant app registrations must set it, and setting it is also how an operator limits sign-in to one organization.
  ID-token verification with email-domain allowlists, as Google has, was rejected: Microsoft ID tokens carry no hosted-domain claim and rarely `email_verified`, and the tenant pin already restricts accounts.
- **Delegated permissions only.**
  Each provider requests the read-write Graph permissions its functions call, plus `User.Read` for the account label on the Connections page and `offline_access` for refresh tokens.
  Graph enforces the signed-in user's own permissions, so an agent can never reach more than the person who connected.
  Application permissions were rejected because they reach every user's data without a signed-in user.
- **Plain HTTP in the primary runtime.**
  The tools call Graph with `httpx`, as the Atlassian tools call their gateway.
  The Microsoft Graph SDK and MSAL were rejected as heavy dependencies that the primary's import graph would have to defer, for a REST surface of a few dozen calls.
  Tokens stay in the primary runtime and are sent only to `graph.microsoft.com`.
- **Uploads follow `file_access`; downloads stay in the workspace.**
  Uploads and outgoing mail attachments read workspace files from the primary runtime and follow the agent's `file_access`, and downloads always write into the workspace, as Google Drive and Gmail do.
  Uploads use Graph's single-request upload, which accepts up to 250 MB; mail attachments use inline attachments, which Graph caps at 3 MB each.
- **Downloads survive the end of `tempauth` links.**
  Graph answers a content download with a redirect to a pre-authenticated SharePoint URL until Microsoft retires those URLs on 2027-04-01 (MC1492958), and with the content itself afterwards.
  The client follows the redirect without the bearer token, so the same code works before and after the change.
- **No credential in logs.**
  `tempauth` joins the query keys that log redaction removes, because a pre-authenticated download URL grants access to the file on its own.
  Graph share IDs (`/shares/u!…`) are the base64 of a sharing link, so the Graph client's request log replaces them.
- **Excel ranges through the files connection.**
  Reading and updating worksheet ranges uses the same `Files.ReadWrite.All` grant, so those functions live in the files toolkit instead of a separate provider that would need its own connection.
- **Optimistic concurrency for replaces.**
  Replacing a file requires the eTag the edit started from and sends it as `If-Match`, so an edit someone made since fails instead of being overwritten; a new upload never overwrites an existing name.
- **No approval rule by default.**
  Like Gmail and Google Drive, these tools run without approval unless the operator adds `tool_approval` rules; the docs give rules for sending and writing.
  Each tool's `write` setting registers or removes its write functions, but scopes stay read-write, because separate read-only providers would double the consent and connection surface.

## Rejected Alternatives

- **Work IQ for everything.**
  It exposes a fixed set of generic operations, is not available in every tenant, and cannot write file contents.
- **One `microsoft_365` toolkit.**
  One connection would request every scope at once, and a single toolkit would put dozens of functions behind one deferred tool name.
- **Graph upload sessions.**
  They handle files over 250 MB, but their upload URLs lose `tempauth` too and need extra resumable-upload state; nothing in normal agent use reaches the single-request limit.
- **Agent Vault bearer injection for Graph.**
  It needs no MindRoom code, but every user would mint and paste a Graph token by hand, and the token would reach the worker.

## Assumptions and Constraints

- Entra returns Graph scopes in their short form (`Files.ReadWrite.All`), which is what the providers request and the stored grants are checked against.
- `ChannelMessage.Read.All` requires administrator consent in every tenant, so reading Teams channels needs an administrator even where users may consent to the other permissions.
- Personal Microsoft accounts (`MICROSOFT_TENANT_ID=common` or `consumers`) reach OneDrive, Outlook, and To Do, but not SharePoint or Teams.
- A `/shares` link resolves only for a user who can open it.

## Delivery

The family lands as a stack: the shared OAuth, Graph client, and file tools first, then Outlook mail, Outlook calendar, To Do, and Teams, each on the shared base.
