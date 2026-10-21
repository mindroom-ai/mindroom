---
icon: fontawesome/brands/microsoft
---

# Microsoft 365

The Microsoft 365 tools reach OneDrive, SharePoint, Outlook mail and calendars, Microsoft To Do, Teams, OneNote, and the organization directory through Microsoft Graph as the person who asked.
Each requester connects their own Microsoft account, so agents see and change only what that person can.
OAuth connection mechanics shared with other providers are on [OAuth Integration Framework](../oauth-framework.md).

| Tool | What it covers | Provider ID | Delegated Graph permissions |
| --- | --- | --- | --- |
| `microsoft_files` | OneDrive and SharePoint files, and Excel ranges in them | `microsoft_files` | `Files.ReadWrite.All` |
| `outlook_mail` | The requester's Outlook mailbox | `outlook_mail` | `Mail.ReadWrite`, `Mail.Send` |
| `outlook_calendar` | The requester's Outlook calendars and events | `outlook_calendar` | `Calendars.ReadWrite` |
| `microsoft_todo` | The requester's Microsoft To Do lists and tasks | `microsoft_todo` | `Tasks.ReadWrite` |
| `microsoft_teams` | Teams channels and chats the requester belongs to | `microsoft_teams` | `Team.ReadBasic.All`, `Channel.ReadBasic.All`, `ChannelMessage.Read.All`, `ChannelMessage.Send`, `Chat.Read`, `ChatMessage.Send` |
| `microsoft_meetings` | Transcripts of Teams meetings the requester organized or was invited to | `microsoft_meetings` | `OnlineMeetings.Read`, `OnlineMeetingTranscript.Read.All` |
| `microsoft_onenote` | The requester's OneNote notebooks, sections, and pages | `microsoft_onenote` | `Notes.ReadWrite.All` |
| `microsoft_people` | People, managers, and direct reports in the requester's organization directory | `microsoft_people` | `User.Read.All` |

Every provider also requests `User.Read`, to label the connected account, and `offline_access`, to stay connected.

## Set Up The Microsoft Entra App

An administrator registers one app in Microsoft Entra for the MindRoom installation, and every Microsoft 365 tool uses it.

1. In the [Microsoft Entra admin center](https://entra.microsoft.com), open **App registrations** and choose **New registration**.
2. Under **Supported account types**, choose your organization only, any organization's accounts if people from several tenants connect, or also personal Microsoft accounts if people use those.
3. Add a **Web** redirect URI `https://mindroom.example.com/api/oauth/<provider ID>/callback` for each tool you enable, such as `https://mindroom.example.com/api/oauth/outlook_mail/callback`, replacing the origin with your public MindRoom origin.
4. Under **Certificates & secrets**, create a client secret and copy its value.
5. Under **API permissions**, add the **Delegated** Microsoft Graph permissions from the table above for the tools you enable, plus `User.Read` and `offline_access`, and choose **Grant admin consent** if your tenant does not let users consent themselves.
6. On **Overview**, copy the application (client) ID and the directory (tenant) ID.

`ChannelMessage.Read.All`, which `microsoft_teams` requests, always needs administrator consent, so nobody can connect Teams until an administrator chooses **Grant admin consent** on the app's **API permissions** page.
`OnlineMeetingTranscript.Read.All`, which `microsoft_meetings` requests, likewise always needs administrator consent.
`User.Read.All`, which `microsoft_people` requests, likewise always needs administrator consent before anyone can connect the people directory.

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
`consumers` or `common` admit personal Microsoft accounts, which reach OneDrive, Outlook mail and calendars, and Microsoft To Do but not SharePoint, Teams, the people directory, or OneNote, whose `Notes.ReadWrite.All` personal accounts cannot grant.
Changing the tenant makes existing connections ask their users to connect again.

## Configure

Add the tools to an agent:

```yaml
agents:
  assistant:
    tools:
      - microsoft_files
      - outlook_mail
      - outlook_calendar
      - microsoft_todo
      - microsoft_teams
      - microsoft_meetings
      - microsoft_onenote
      - microsoft_people
```

| Option | Tool | Type | Default | Notes |
| --- | --- | --- | --- | --- |
| `write` | `microsoft_files` | `boolean` | `true` | Allows replacing and uploading files and writing Excel ranges; with `false`, those functions are not offered. |
| `write` | `outlook_mail` | `boolean` | `true` | Allows creating and sending drafts, sending new mail and replies, marking messages read or unread, and moving messages; with `false`, those functions are not offered. |
| `write` | `outlook_calendar` | `boolean` | `true` | Allows creating, changing, deleting, and answering events; with `false`, those functions are not offered. |
| `write` | `microsoft_todo` | `boolean` | `true` | Allows creating, updating, completing, and deleting tasks; with `false`, those functions are not offered. |
| `write` | `microsoft_teams` | `boolean` | `true` | Allows sending channel posts, channel replies, and chat messages; with `false`, those functions are not offered. |
| `write` | `microsoft_onenote` | `boolean` | `true` | Allows creating pages and appending text to pages; with `false`, those functions are not offered. |

The scopes stay the same with `write: false`, so turning writes back on does not need new consent.
`microsoft_meetings` and `microsoft_people` only read, so they have no `write` option.

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
Replacing a file, uploading into a folder, and writing a range also take the target's `web_url` from its details, download, or folder listing (search results show another form) as `expected_web_url`, so an approval card shows where the write lands, and fail with `target_mismatch`, naming the target's `web_url`, when it differs.
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
A search with `query` stops after its first 1,000 matches, so a narrower query finds the rest.
Folders are named by a well-known name, `inbox`, `sentitems`, `drafts`, `archive`, `deleteditems`, or `junkemail`, or by a folder ID, such as a message's `parent_folder_id`.
Messages and replies are plain text, and sent mail is saved in the requester's Sent Items.
A new message takes at most 50 email addresses across To and Cc.
To review a message before it goes out, ask for a draft; its `web_link` opens it in Outlook.

Attachments are workspace files and follow the agent's [`file_access`](../configuration/agents.md) setting.
Each attached file may be at most 3 MB, and all attachments of one message together about 3 MB; share larger files through OneDrive and send a link instead.
Only file attachments can be downloaded, not attached Outlook items such as forwarded messages or invitations, and not cloud links.
A later download of the same attachment name overwrites the copy in `outlook-mail-attachments/`.

## Calendar

| Function | Changes data | Purpose |
| --- | --- | --- |
| `outlook_calendar_list_calendars` | no | List the requester's calendars, whether the requester can edit each, and which one is the default. |
| `outlook_calendar_list_events` | no | List events between two moments in the default calendar or another, in start order, up to 50, with attendees and their responses and the online meeting link. |
| `outlook_calendar_get_event` | no | Read an event with its description as plain text, up to 20,000 characters. |
| `outlook_calendar_get_free_busy` | no | Look up when up to 20 colleagues, distribution lists, or rooms are busy, with their working hours, to find a time to meet. |
| `outlook_calendar_create_event` | yes | Create an event, optionally with attendees, a location, a description, and an online meeting; Outlook emails each attendee an invitation. |
| `outlook_calendar_update_event` | yes | Change an event's subject, start, end, location, or description; attendees of a meeting the requester organizes receive the update. |
| `outlook_calendar_delete_event` | yes | Delete an event; for a meeting the requester organizes, Outlook sends every attendee a cancellation. |
| `outlook_calendar_respond_to_event` | yes | Accept, tentatively accept, or decline a meeting invitation, optionally with a comment to the organizer. |

Event times in event results are in UTC, and the agent converts them to the requester's time zone.
A listing window may span at most 366 days.
Calendar and event listings return a `next_page_token` while more results exist; passing it back continues the same listing.
New and changed events take local times plus a time zone, either a Windows name such as `W. Europe Standard Time` or an IANA name Outlook accepts such as `Europe/Berlin`.
Free/busy lookups take local times plus a time zone, like new events, and return times in that zone.
A free/busy window must be shorter than 62 days, and each member of a distribution list counts toward the 20.
Free/busy shows another person's event subjects and locations only when their calendar sharing allows it, and it needs a work or school account.
Each occurrence of a recurring meeting has its own ID, and changing or deleting an occurrence affects only that occurrence.
Responding to the series master, whose ID each occurrence carries as `series_master_id`, answers the whole series.
Changing or deleting the series master changes or deletes every occurrence.
Deleting a meeting the requester only attends removes it from their calendar without telling the organizer; declining it tells the organizer.
A new event can invite at most 100 attendees.
The description of an online meeting cannot be replaced, because that would remove its join link; edit it in Outlook instead.

## To Do

| Function | Changes data | Purpose |
| --- | --- | --- |
| `microsoft_todo_list_lists` | no | List the requester's task lists, including lists shared with them, and whether the requester owns each. |
| `microsoft_todo_list_tasks` | no | List up to 50 open tasks of one list, or completed ones too when asked, with notes up to 2,000 characters each. |
| `microsoft_todo_create_task` | yes | Create a task with a title and, optionally, a due date, notes, and importance. |
| `microsoft_todo_update_task` | yes | Change a task's title, due date, notes, or importance, or mark it completed or not started again. |
| `microsoft_todo_delete_task` | yes | Delete a task. |

The lists include the requester's default Tasks list and the list of flagged Outlook messages.
New and changed due dates take a calendar date and become midnight in the `time_zone` the agent passes, which should be the requester's zone and defaults to UTC.
Listed, created, and updated tasks return the due date, time, and zone Graph reports; a due date is midnight in the zone of whoever set it, which Graph usually reports in UTC, so the agent converts it to the requester's zone before naming the day.
List and task listings return a `next_page_token` while more results exist; passing it back continues the same listing.
Updating a task's notes replaces them completely.

## Teams

| Function | Changes data | Purpose |
| --- | --- | --- |
| `microsoft_teams_list_teams` | no | List the teams the requester is a member of, and whether each is archived. |
| `microsoft_teams_list_channels` | no | List the channels of one team that the requester can see, and whether each is archived. |
| `microsoft_teams_list_channel_messages` | no | List up to 50 top-level posts of one channel, without their replies, most recently active thread first. |
| `microsoft_teams_list_channel_message_replies` | no | List up to 50 replies in the thread of one channel post. |
| `microsoft_teams_list_chats` | no | List up to 50 of the requester's one-on-one, group, and meeting chats, with member names, most recent message first. |
| `microsoft_teams_list_chat_messages` | no | List up to 50 messages of one chat, newest first. |
| `microsoft_teams_send_channel_message` | yes, posts to the channel | Post a new message in a channel. |
| `microsoft_teams_reply_to_channel_message` | yes, posts to the channel | Reply in the thread of a channel post. |
| `microsoft_teams_send_chat_message` | yes, posts to the chat | Send a message in an existing chat. |

Messages come back as plain text, up to 4,000 characters each.
Deleted messages and system events, such as members joining, come back labeled, without text.
Channel, chat, and message listings return a `next_page_token` while more results exist; passing it back continues the same listing.
Sent messages appear under the requester's name and keep their line breaks, and HTML in the text shows literally instead of as formatting.
A sent message holds up to 28,000 bytes once its text is HTML-escaped.
The tool cannot start a new chat, and it sees private and shared channels only when the requester is a member.
Shared channels from a team the requester is not a member of are not reachable.

## Teams Meetings

| Function | Changes data | Purpose |
| --- | --- | --- |
| `microsoft_meetings_find` | no | Find the Teams meeting behind a join link, with its subject, times, organizer, and expiry. |
| `microsoft_meetings_list_transcripts` | no | List the transcripts of one meeting, with when each was created and ended. |
| `microsoft_meetings_get_transcript` | no | Read one transcript as timestamped lines labeled with their speaker, up to 30,000 characters per call. |

To read a meeting's transcript, the agent takes the event's `join_url` from `outlook_calendar_list_events` or `outlook_calendar_get_event`, finds the meeting, lists its transcripts, and reads one.
Transcript lines look like `00:12:03 Alex Doe: text`, timed from the start of transcription, with each speaker's consecutive sentences joined into lines of up to about 2,000 characters.
A long transcript comes back in chunks with a `next_page_token`; passing it back continues the same transcript, and transcript listings page the same way.
A recurring meeting series is one meeting, so its listing holds a transcript for each occurrence that was transcribed.

Transcripts exist only for meetings in which someone turned on transcription.
Microsoft Graph serves them only to the meeting's organizer and the people its calendar invite names, only for meetings scheduled with a calendar event, and not for live events.
Graph no longer serves the transcripts of an expired meeting; a one-time meeting expires 60 days after its scheduled time, extended by 60 days whenever someone joins or changes it, and `microsoft_meetings_find` reports when a meeting expires.
Two Teams administrator settings also govern transcripts: with Graph access to transcripts turned off, listing and reading transcripts return `transcripts_disabled`, and with speaker attribution turned off, reading a transcript returns `speaker_attribution_disabled`.

## OneNote

| Function | Changes data | Purpose |
| --- | --- | --- |
| `microsoft_onenote_list_notebooks` | no | List the notebooks the requester owns, by name, and which one is the default. |
| `microsoft_onenote_list_sections` | no | List the sections directly in one notebook, by name. |
| `microsoft_onenote_list_pages` | no | List up to 50 pages of one section, most recently modified first, with titles and dates. |
| `microsoft_onenote_get_page` | no | Read a page's content as plain text, 30,000 characters at a time. |
| `microsoft_onenote_create_page` | yes | Create a page with a title and plain-text content in a section. |
| `microsoft_onenote_append_to_page` | yes | Add plain text as a new paragraph at the end of a page's first block of content. |

Only the notebooks the requester owns are listed; notebooks other people share with the requester, and group or SharePoint site notebooks, are not.
Sections inside section groups are not listed either.
Page text leaves out the title, shows images as `[image]`, and separates table cells with `|`.
Listings and page text return a `next_page_token` while more follows; passing it back with the same notebook, section, or page continues.
Created and appended text keeps its line breaks, and HTML in it shows literally instead of as formatting.

## People

| Function | Changes data | Purpose |
| --- | --- | --- |
| `microsoft_people_search` | no | Find people in the organization directory by name or by the beginning of their email address, up to 50 results. |
| `microsoft_people_get_profile` | no | Read one person's profile and their manager. |
| `microsoft_people_list_direct_reports` | no | List the people who report directly to one person. |

Agents use these functions to turn a name the requester mentions into a person and an email address for the mail, calendar, and Teams tools.
Each person comes back with their id, name, email address, job title, department, office location, and business phones; mobile phones and other personal fields are never read.
The email address is the person's primary email address, or their sign-in name when they have none.
Profile and direct report lookups take a person's id, email address, or sign-in name, or `me` for the requester, so "who is my manager" needs no email address.
Search matches names by whole words or their beginnings in any order, and email addresses from their beginning, and someone added to the directory in the last few minutes may not show up yet.
A person without a manager, or whose manager is a contact instead of a user, comes back with `manager` set to `null`, and direct report listings leave out contacts.
Direct report listings return a `next_page_token` while more results exist; passing it back continues the same listing.
Guest accounts cannot search the directory.

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
    - match: outlook_calendar_create_event
      action: require_approval
    - match: outlook_calendar_update_event
      action: require_approval
    - match: outlook_calendar_delete_event
      action: require_approval
    - match: outlook_calendar_respond_to_event
      action: require_approval
    - match: microsoft_todo_create_task
      action: require_approval
    - match: microsoft_todo_update_task
      action: require_approval
    - match: microsoft_todo_delete_task
      action: require_approval
    - match: microsoft_teams_send_channel_message
      action: require_approval
    - match: microsoft_teams_reply_to_channel_message
      action: require_approval
    - match: microsoft_teams_send_chat_message
      action: require_approval
    - match: microsoft_onenote_create_page
      action: require_approval
    - match: microsoft_onenote_append_to_page
      action: require_approval
```

Sent mail cannot be recalled, so require approval at least for `outlook_mail_send`, `outlook_mail_send_draft`, and `outlook_mail_reply`, and likewise for the Teams send functions, whose messages everyone in the channel or chat sees at once.
`outlook_mail_create_draft`, `outlook_mail_mark_read`, and `outlook_mail_move` change only the requester's own mailbox, where they can be undone.
Every calendar write can email invitations, updates, cancellations, or responses to other people.

To give an agent read access only, set `write: false` on the tool.

## Troubleshooting

| Result | Cause and fix |
| --- | --- |
| `AADSTS50011` on the Microsoft sign-in page | The redirect URI is not registered on the app; add the exact URI from [Set Up The Microsoft Entra App](#set-up-the-microsoft-entra-app). |
| `AADSTS65001` or a consent prompt the requester cannot approve | The tenant requires administrator consent, which Teams, Teams meeting transcripts, and the people directory always do; an administrator grants it on the app's **API permissions** page. |
| `AADSTS50194` | The app is registered for one organization; set `MICROSOFT_TENANT_ID`. |
| `invalid_tenant` | `MICROSOFT_TENANT_ID` is not a tenant ID, a verified domain, `organizations`, `common`, or `consumers`. |
| `forbidden` | The connected account lacks access to that item, which reconnecting does not fix. |
| `not_found` | The item does not exist or the connected account cannot see it, or the Teams meeting expired. |
| `invalid_file` | The value is neither a Graph item path nor a OneDrive or SharePoint link. |
| `invalid_argument` | An argument is malformed or exceeds a limit above, such as an ID, folder, email address, time, or time zone, or an end that is not after its start; the message names the field. |
| `invalid_local_path` | An upload or attachment path is not an existing file the agent's `file_access` allows. |
| `online_meeting_unavailable` | The calendar has no default online meeting provider, such as Teams, so the event cannot get a join link; create it without an online meeting. |
| `online_meeting_body` | The update tried to replace an online meeting's description; change the other fields, or edit the description in Outlook. |
| `meeting_not_found` | No Teams meeting with that join link is visible to the requester, such as a meeting they neither organized nor were invited to. |
| `no_transcripts` | Nobody turned on transcription in the meeting, or Teams has not finished saving the transcript yet. |
| `no_calendar_event` | The meeting was created without a calendar event, such as by an app, and Microsoft Graph serves no transcripts for it. |
| `transcripts_disabled` | A Teams administrator turned off Microsoft Graph access to meeting transcripts; only an administrator can turn it back on. |
| `speaker_attribution_disabled` | A Teams administrator turned off speaker attribution for transcripts, and `microsoft_meetings` reads transcripts only with their speakers. |
| `not_a_file_attachment` | The mail attachment is an attached Outlook item or a cloud link, which cannot be downloaded. |
| `etag_mismatch` | The file changed since the `etag` was read; download it again and redo the edit. |
| `locked` | The file is checked out or locked; try again later. |
| `name_conflict` | A file with that name already exists in the folder; replace it or choose another name. |
| `too_large` | The file, range, attachment, or message exceeds a limit above; send a OneDrive link instead of a large attachment. |
| `throttled` | Microsoft Graph is limiting requests; retry after `retry_after_seconds`. |
| `download_rejected` | SharePoint refused the download link Microsoft Graph returned; retry the download. |
| `graph_error` | Microsoft Graph refused the request, such as a time zone Outlook does not support or a response to a meeting the requester organizes; `graph_code` names the reason. |

When Microsoft rejects the connection's access token, the result includes a reconnect link.

## Security Notes

- The tools run in the primary MindRoom runtime, so worker containers never receive the app client configuration or user tokens.
- Tokens are sent only to Microsoft Graph, never to another host, including the SharePoint hosts that serve downloads.
- Request logs never contain tokens or pre-authenticated download links.
- Error results never contain tokens or raw response bodies.

## Related Docs

- [OAuth Integration Framework](../oauth-framework.md)
- [Tool Approval](../tool-approval.md)
