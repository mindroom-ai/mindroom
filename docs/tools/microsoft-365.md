---
icon: fontawesome/brands/microsoft
---

# Microsoft 365

The Microsoft 365 tools reach OneDrive, SharePoint, and Outlook mail through Microsoft Graph as the person who asked.
Each requester connects their own Microsoft account, so agents see and change only what that person can.
OAuth connection mechanics shared with other providers are on [OAuth Integration Framework](../oauth-framework.md).

| Tool | What it covers | Provider ID | Delegated Graph permissions |
| --- | --- | --- | --- |
| `microsoft_files` | OneDrive and SharePoint files, and Excel ranges in them | `microsoft_files` | `Files.ReadWrite.All` |
| `outlook_mail` | The requester's Outlook mailbox | `outlook_mail` | `Mail.ReadWrite`, `Mail.Send` |

Every provider also requests `User.Read`, to label the connected account, and `offline_access`, to stay connected.

## Set Up The Microsoft Entra App

An administrator registers one app in Microsoft Entra for the MindRoom installation, and every Microsoft 365 tool uses it.

1. In the [Microsoft Entra admin center](https://entra.microsoft.com), open **App registrations** and choose **New registration**.
2. Under **Supported account types**, choose your organization only, any organization's accounts if people from several tenants connect, or also personal Microsoft accounts if people use those.
3. Add a **Web** redirect URI `https://mindroom.example.com/api/oauth/<provider ID>/callback` for each tool you enable, such as `https://mindroom.example.com/api/oauth/outlook_mail/callback`, replacing the origin with your public MindRoom origin.
4. Under **Certificates & secrets**, create a client secret and copy its value.
5. Under **API permissions**, add the **Delegated** Microsoft Graph permissions from the table above for the tools you enable, plus `User.Read` and `offline_access`, and choose **Grant admin consent** if your tenant does not let users consent themselves.
6. On **Overview**, copy the application (client) ID and the directory (tenant) ID.

For a local installation, the redirect URIs are `http://localhost:8765/api/oauth/<provider ID>/callback`, such as `http://localhost:8765/api/oauth/microsoft_files/callback`.
A hosted installation must set `MINDROOM_PUBLIC_URL` or `MINDROOM_BASE_URL`, from which MindRoom derives each redirect URI, and the URIs registered on the app must match exactly.
Do not add application permissions; the tools only use delegated permissions, which act as the signed-in user.

Store the client ID and secret in the `microsoft_oauth_client` credential service through the dashboard credentials page.
For non-interactive deployments, seed it at startup with a [credential seed](../oauth-framework.md#credential-seeds):

```json
[
  {
    "service": "microsoft_oauth_client",
    "credentials": {
      "client_id": {"env": "MICROSOFT_CLIENT_ID"},
      "client_secret": {"env": "MICROSOFT_CLIENT_SECRET"}
    }
  }
]
```

To use a different app for one tool, store its credentials in that tool's `<provider ID>_oauth_client` service, such as `microsoft_files_oauth_client`, which takes precedence over `microsoft_oauth_client`.

Set `MICROSOFT_TENANT_ID` to the directory (tenant) ID or a verified domain of your organization.
It defaults to `organizations`, which accepts any work or school account, and an app registered for your organization only requires the tenant to be set.
Setting it to your tenant ID or domain limits sign-in to that tenant's accounts.
`consumers` or `common` admit personal Microsoft accounts, which reach OneDrive and Outlook mail but not SharePoint.
Changing the tenant makes existing connections ask their users to connect again.

## Configure

Add the tools to an agent:

```yaml
agents:
  assistant:
    tools:
      - microsoft_files
      - outlook_mail
```

| Option | Tool | Type | Default | Notes |
| --- | --- | --- | --- | --- |
| `write` | `microsoft_files` | `boolean` | `true` | Allows replacing and uploading files and writing Excel ranges; with `false`, those functions are not offered. |
| `write` | `outlook_mail` | `boolean` | `true` | Allows creating and sending drafts, sending new mail and replies, marking messages read or unread, and moving messages; with `false`, those functions are not offered. |

The scopes stay the same with `write: false`, so turning writes back on does not need new consent.

## Connect An Account

When a requester has not connected a tool's service yet, each of its functions returns `oauth_connection_required: true` with a `connect_url` for that requester.
The requester opens the link, signs in to Microsoft, approves the requested permissions, and retries the request.
Users can also connect and disconnect from the dashboard Integrations page, where each service is a separate connection.

Tokens belong to the requester and are never shared with other users or stored globally.
A call without a concrete requester always asks for a connection instead of using another account.
Microsoft ends a connection that has not been used for 90 days, and the requester then connects again.

## Files

| Function | Changes data | Purpose |
| --- | --- | --- |
| `microsoft_files_search` | no | Search the OneDrive and SharePoint files the requester can open, by name and content, up to 50 results. |
| `microsoft_files_list_folder` | no | List a folder's files and folders, or the requester's OneDrive root, up to 200 entries. |
| `microsoft_files_get_file` | no | Read a file's details, including its `etag`, web link, size, and last editor. |
| `microsoft_files_download_file` | no | Save a file, up to 100 MB, into the workspace's `microsoft-files-downloads/` directory. |
| `microsoft_files_replace_file` | yes | Replace a file's contents with a workspace file, up to 250 MB, given the `etag` the edit started from. |
| `microsoft_files_upload_file` | yes | Upload a workspace file, up to 250 MB, as a new file in a folder or the requester's OneDrive root. |
| `microsoft_files_excel_list_worksheets` | no | List the worksheets of an Excel workbook. |
| `microsoft_files_excel_read_range` | no | Read a worksheet range, or its used range, up to 10,000 cells. |
| `microsoft_files_excel_update_range` | yes | Write values or formulas into a worksheet range, up to 10,000 cells. |

Functions name a file or folder by its Graph item path, `/drives/{drive-id}/items/{item-id}`, which search, listing, and details return, or by a OneDrive or SharePoint link the requester can open.
Uploads read from the agent's workspace and follow its [`file_access`](../configuration/agents.md) setting, and downloads always land in the workspace.

To edit a document, the agent downloads it, saves its edited copy under a new name outside `microsoft-files-downloads/`, and replaces the original with the `etag` from the download.
The replace then fails with `etag_mismatch` if someone changed the file in the meantime, and the file's version history keeps the replaced version.
A later download of the same name overwrites the copy in `microsoft-files-downloads/`.
Uploads never overwrite an existing file; a name that already exists returns `name_conflict`.

Excel functions work on `.xlsx` workbooks in OneDrive for work or school and SharePoint.
They take a worksheet name and an address such as `B2` or `A1:C10` without a worksheet prefix, and written values must have the shape of the address.
A written string that starts with `=` becomes a formula.

Search uses Microsoft Search, which covers work and school accounts.
Search and folder listings return a `next_page_token` while more results exist; passing it back continues the same search or listing.

## Mail

| Function | Changes data | Purpose |
| --- | --- | --- |
| `outlook_mail_search` | no | List messages newest first, in one folder or across the mailbox, optionally matching an Outlook search query, up to 50 results. |
| `outlook_mail_get_message` | no | Read a message as plain text, up to 20,000 characters, with its recipients and attachment list. |
| `outlook_mail_download_attachment` | no | Save a file attachment, up to 50 MB, into the workspace's `outlook-mail-attachments/` directory. |
| `outlook_mail_create_draft` | yes | Save a new plain-text draft, optionally with workspace files attached, without sending it. |
| `outlook_mail_send_draft` | yes, sends mail | Send an existing draft. |
| `outlook_mail_send` | yes, sends mail | Send a new plain-text message at once, optionally with workspace files attached. |
| `outlook_mail_reply` | yes, sends mail | Reply to the sender, or to everyone with `reply_all`, above the quoted original; the original's reply-to addresses, when set, take the sender's place. |
| `outlook_mail_mark_read` | yes | Mark a message read or unread. |
| `outlook_mail_move` | yes | Move a message to a folder. |

Search takes Outlook search syntax, such as `from:sam@example.com subject:"quarterly plan" hasAttachments:true`.
Search returns a `next_page_token` while more messages exist; passing it back continues the same listing or search.
Folders are named by a well-known name, `inbox`, `sentitems`, `drafts`, `archive`, `deleteditems`, or `junkemail`, or by a folder ID, such as a message's `parent_folder_id`.
Messages and replies are plain text, and sent mail is saved in the requester's Sent Items.
A new message takes at most 50 email addresses across To and Cc.
To review a message before it goes out, ask for a draft; its `web_link` opens it in Outlook.

Attachments are workspace files and follow the agent's [`file_access`](../configuration/agents.md) setting.
Each attached file may be at most 3 MB, and all attachments of one message together about 3 MB; share larger files through OneDrive and send a link instead.
Only file attachments can be downloaded, not attached Outlook items such as forwarded messages or invitations, and not cloud links.
A later download of the same attachment name overwrites the copy in `outlook-mail-attachments/`.

## Approvals For Writes

The tools run without approval unless you add [tool approval](../tool-approval.md) rules, which show a Matrix approval card before each change:

```yaml
tool_approval:
  rules:
    - match: microsoft_files_replace_file
      action: require_approval
    - match: microsoft_files_upload_file
      action: require_approval
    - match: microsoft_files_excel_update_range
      action: require_approval
    - match: outlook_mail_send
      action: require_approval
    - match: outlook_mail_send_draft
      action: require_approval
    - match: outlook_mail_reply
      action: require_approval
```

Sent mail cannot be recalled, so require approval at least for `outlook_mail_send`, `outlook_mail_send_draft`, and `outlook_mail_reply`.
`outlook_mail_create_draft`, `outlook_mail_mark_read`, and `outlook_mail_move` change only the requester's own mailbox, where they can be undone.

To give an agent read access only, set `write: false` on the tool.

## Troubleshooting

| Result | Cause and fix |
| --- | --- |
| `AADSTS50011` on the Microsoft sign-in page | The redirect URI is not registered on the app; add the exact URI from [Set Up The Microsoft Entra App](#set-up-the-microsoft-entra-app). |
| `AADSTS65001` or a consent prompt the requester cannot approve | The tenant requires administrator consent; an administrator grants it on the app's **API permissions** page. |
| `AADSTS50194` | The app is registered for one organization; set `MICROSOFT_TENANT_ID`. |
| `invalid_tenant` | `MICROSOFT_TENANT_ID` is not a tenant ID, a verified domain, `organizations`, `common`, or `consumers`. |
| `forbidden` | The connected account lacks access to that item, which reconnecting does not fix. |
| `not_found` | The item does not exist or the connected account cannot see it. |
| `invalid_file` | The value is neither a Graph item path nor a OneDrive or SharePoint link. |
| `invalid_argument` | An ID, folder, email address, or other argument is malformed, or a message names more than 50 recipients. |
| `invalid_local_path` | An upload or attachment path is not an existing file the agent's `file_access` allows. |
| `not_a_file_attachment` | The mail attachment is an attached Outlook item or a cloud link, which cannot be downloaded. |
| `etag_mismatch` | The file changed since the `etag` was read; download it again and redo the edit. |
| `locked` | The file is checked out or locked; try again later. |
| `name_conflict` | A file with that name already exists in the folder; replace it or choose another name. |
| `too_large` | The file, range, attachment, or message exceeds a limit above; send a OneDrive link instead of a large attachment. |
| `throttled` | Microsoft Graph is limiting requests; retry after `retry_after_seconds`. |
| `download_rejected` | SharePoint refused the download link Microsoft Graph returned; retry the download. |

When Microsoft rejects the connection's access token, the result includes a reconnect link.

## Security Notes

- The tools run in the primary MindRoom runtime, so worker containers never receive the app client configuration or user tokens.
- Tokens are sent only to Microsoft Graph, never to another host, including the SharePoint hosts that serve downloads.
- Request logs never contain tokens or pre-authenticated download links.
- Error results never contain tokens or raw response bodies.

## Related Docs

- [OAuth Integration Framework](../oauth-framework.md)
- [Tool Approval](../tool-approval.md)
