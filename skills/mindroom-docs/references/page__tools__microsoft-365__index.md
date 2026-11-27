# Microsoft 365

The Microsoft 365 tools reach OneDrive, SharePoint, Outlook mail and calendars, Microsoft To Do, Teams, OneNote, and the organization directory through Microsoft Graph as the person who asked.
Each requester connects their own Microsoft account, so agents see and change only what that person can.
OAuth connection mechanics shared with other providers are on [OAuth Integration Framework](https://docs.mindroom.chat/oauth-framework/).

| Tool | What it covers | Provider ID | Delegated Graph permissions |
| --- | --- | --- | --- |
| `microsoft_files` | OneDrive and SharePoint files, and Excel ranges in them | `microsoft_files` | `Files.ReadWrite.All` |
| `microsoft_sharepoint` | SharePoint sites, list items, and site pages the requester can open | `microsoft_sharepoint` | `Sites.ReadWrite.All` |
| `outlook_mail` | The requester's Outlook mailbox and automatic replies, and shared mailboxes the requester can open | `outlook_mail` | `Mail.ReadWrite`, `Mail.Send`, `Mail.ReadWrite.Shared`, `Mail.Send.Shared`, `MailboxSettings.ReadWrite` |
| `outlook_calendar` | The requester's Outlook calendars and events, and calendars others shared with or delegated to them | `outlook_calendar` | `Calendars.ReadWrite`, `Calendars.ReadWrite.Shared` |
| `microsoft_todo` | The requester's Microsoft To Do lists and tasks | `microsoft_todo` | `Tasks.ReadWrite` |
| `microsoft_teams` | Teams channels and chats the requester belongs to, new chats, and people's presence | `microsoft_teams` | `Team.ReadBasic.All`, `Channel.ReadBasic.All`, `ChannelMessage.Read.All`, `ChannelMessage.Send`, `Chat.ReadWrite`, `ChatMessage.Send`, `Presence.Read.All`, `User.Read.All` |
| `microsoft_meetings` | Transcripts of Teams meetings the requester organized or was invited to | `microsoft_meetings` | `OnlineMeetings.Read`, `OnlineMeetingTranscript.Read.All` |
| `microsoft_onenote` | OneNote notebooks of the requester, of their Teams teams and SharePoint sites, and of people who share notebooks with them | `microsoft_onenote` | `Notes.ReadWrite.All` |
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

`ChannelMessage.Read.All` and `User.Read.All`, which `microsoft_teams` requests, always need administrator consent, so nobody can connect Teams until an administrator chooses **Grant admin consent** on the app's **API permissions** page.
`OnlineMeetingTranscript.Read.All`, which `microsoft_meetings` requests, likewise always needs administrator consent.
`User.Read.All`, which `microsoft_people` requests, likewise always needs administrator consent before anyone can connect the people directory.

For a local installation, the redirect URIs are `http://localhost:8765/api/oauth/<provider ID>/callback`, such as `http://localhost:8765/api/oauth/microsoft_files/callback`.
A hosted installation must set `MINDROOM_PUBLIC_URL`, from which MindRoom derives each redirect URI, and the URIs registered on the app must match exactly.
Do not add application permissions; the tools only use delegated permissions, which act as the signed-in user.

Store the client ID and secret in the `microsoft_oauth_client` credential service through the dashboard credentials page.
For non-interactive deployments, seed it at startup with a [credential seed](https://docs.mindroom.chat/oauth-framework/#credential-seeds):

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
      - microsoft_sharepoint
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
| `write` | `microsoft_files` | `boolean` | `true` | Allows replacing and uploading files and writing Excel ranges (`microsoft_files_write`); with `false`, that function is not offered. |
| `write` | `microsoft_sharepoint` | `boolean` | `true` | Allows adding list items and changing their column values (`microsoft_sharepoint_write`); with `false`, that function is not offered. |
| `write` | `outlook_mail` | `boolean` | `true` | Allows creating drafts, sending new mail and replies, marking messages read or unread, moving messages, and changing automatic replies (`outlook_mail_create_draft`, `outlook_mail_send`, `outlook_mail_manage`); with `false`, those functions are not offered. |
| `write` | `outlook_calendar` | `boolean` | `true` | Allows creating, changing, deleting, and answering events (`outlook_calendar_write`); with `false`, that function is not offered. |
| `write` | `microsoft_todo` | `boolean` | `true` | Allows creating, updating, completing, and deleting tasks (`microsoft_todo_write`); with `false`, that function is not offered. |
| `write` | `microsoft_teams` | `boolean` | `true` | Allows sending channel posts, channel replies, and chat messages, and starting chats (`microsoft_teams_send`); with `false`, that function is not offered. |
| `write` | `microsoft_onenote` | `boolean` | `true` | Allows creating pages and appending text to pages (`microsoft_onenote_write`); with `false`, that function is not offered. |

The scopes stay the same with `write: false`, so turning writes back on does not need new consent.
`microsoft_meetings` and `microsoft_people` only read, so they have no `write` option.

## Connect An Account

When a requester has not connected a tool's service yet, each of its functions returns `oauth_connection_required: true` with a `connect_url` for that requester.
The requester opens the link, signs in to Microsoft, approves the requested permissions, and retries the request.
Users can also connect and disconnect from the dashboard Integrations page, where each service is a separate connection.
The [Connections portal](https://docs.mindroom.chat/connections-portal/#what-the-portal-shows) can connect most of an agent's unconnected Microsoft 365 services with one sign-in.

Tokens belong to the requester and are never shared with other users or stored globally.
A call without a concrete requester always asks for a connection instead of using another account.
Microsoft ends a connection that has not been used for 90 days, and the requester then connects again.

## Files

| Function | Actions | Changes data | Purpose |
| --- | --- | --- | --- |
| `microsoft_files_read` | `search`, `list_folder`, `get_file`, `download_file`, `excel_list_worksheets`, `excel_read_range` | no | Search accessible OneDrive/SharePoint files (up to 50), list folders (up to 200), inspect metadata (`etag`, `web_url`, size, last editor), download files (up to 100 MB) to `microsoft-files-downloads/`, list Excel worksheets, and read worksheet ranges (up to 10,000 cells). |
| `microsoft_files_write` | `replace_file`, `upload_file`, `excel_update_range` | yes | Replace an existing file (up to 250 MB, checked against `etag`), upload a new workspace file (up to 250 MB), or write values/formulas into an Excel range (up to 10,000 cells). |

Functions name a file or folder by its Graph item path, `/drives/{drive-id}/items/{item-id}`, which search, listing, and details return, or by a OneDrive or SharePoint link the requester can open.
Replacing a file, uploading into a folder, and writing a range also take the target's `web_url` from its details, download, or folder listing (search results show another form) as `expected_web_url`, so an approval card shows where the write lands, and fail with `target_mismatch`, naming the target's `web_url`, when it differs.
Uploads read from the agent's workspace and follow its [`file_access`](https://docs.mindroom.chat/configuration/agents/) setting, and downloads always land in the workspace.

To edit a document, the agent downloads it, saves its edited copy under a new name outside `microsoft-files-downloads/`, and replaces the original with the `etag` from the download.
The replace then fails with `etag_mismatch` if someone changed the file in the meantime, and the file's version history keeps the replaced version.
A later download of the same name overwrites the copy in `microsoft-files-downloads/`.
Uploads never overwrite an existing file; a name that already exists returns `name_conflict`.

Excel actions work on `.xlsx` workbooks in OneDrive for work or school and SharePoint.
They take a worksheet name and an address such as `B2` or `A1:C10` without a worksheet prefix, and written values must have the shape of the address.
A written string that starts with `=` becomes a formula.

Search uses Microsoft Search, which covers work and school accounts.
Search and folder listings return a `next_page_token` while more results exist; passing it back continues the same search or listing.

## SharePoint

| Function | Actions | Changes data | Purpose |
| --- | --- | --- | --- |
| `microsoft_sharepoint_read` | `search_sites`, `get_site`, `list_lists`, `get_list`, `list_items`, `list_pages`, `get_page` | no | Search SharePoint sites, look up a site by URL, list non-hidden lists and columns (internal/display name, type, required/read-only), list up to 50 items (text capped at 4,000 chars), list site pages/news posts, and read a page as plain text in 30,000-character chunks. |
| `microsoft_sharepoint_write` | `create_item`, `update_item` | yes | Add a new list item or update column values on an existing list item (omitted columns stay unchanged). |

The tool covers sites, lists, list items, and pages; files and the files in document libraries stay with `microsoft_files`.
List items take and return column values keyed by each column's internal name, which `microsoft_sharepoint_read` (`action="get_list"`) reports next to the display name.
A lookup or person column is written as its name plus `LookupId`, such as `OwnerLookupId`, with the lookup ID.
A multi-value column takes a list, together with `<name>@odata.type` set to `Collection(Edm.String)` for text or `Collection(Edm.Int32)` for lookup IDs.
Adding and changing items also take the list's `web_url` from `microsoft_sharepoint_read` (`list_lists` or `get_list`) as `expected_web_url`, so an approval card shows which list the write lands in, and fail with `target_mismatch`, naming the list's `web_url`, when it differs.
Site addresses must be the site itself, without a page, list, or library path after it.
Page text leaves out web parts that are not text, such as image or embedded document parts, and images inside text show as `[image]`.
Site, list, item, and page listings, and long pages, return a `next_page_token` while more exists; passing it back continues the same listing or page.

## Mail

| Function | Actions | Changes data | Purpose |
| --- | --- | --- | --- |
| `outlook_mail_read` | `search`, `get_message`, `download_attachment`, `get_automatic_replies` | no | List/search messages newest first (up to 50), read a message as plain text (up to 20,000 chars) with recipients and attachments, download a file attachment (up to 50 MB) to `outlook-mail-attachments/`, and read automatic-reply (out-of-office) settings. |
| `outlook_mail_create_draft` | — | yes | Save a new plain-text draft, optionally with workspace files attached, without sending it. |
| `outlook_mail_send` | `send`, `reply` | yes, sends mail | Send a new plain-text message (optionally with workspace attachments) or reply to sender / everyone (`reply_all`) above the quoted original, honoring reply-to addresses. |
| `outlook_mail_manage` | `mark_read`, `move`, `set_automatic_replies` | yes | Mark a message read or unread, move a message to a folder, or configure the requester's automatic replies (`disabled`, `alwaysEnabled`, or `scheduled`). |

Search takes Outlook search syntax, such as `from:sam@example.com subject:"quarterly plan" hasAttachments:true`.
Search returns a `next_page_token` while more messages exist; passing it back continues the same listing or search.
A search with `query` stops after its first 1,000 matches, so a narrower query finds the rest.
Folders are named by a well-known name, `inbox`, `sentitems`, `drafts`, `archive`, `deleteditems`, or `junkemail`, or by a folder ID, such as a message's `parent_folder_id`.
Messages and replies are plain text, and sent mail is saved in the sending mailbox's Sent Items.
A new message takes at most 50 email addresses across To and Cc.
To review a message before it goes out, ask for a draft; its `web_link` opens it in Outlook, where the requester sends it.

Every operation except the automatic-reply actions (`get_automatic_replies`, `set_automatic_replies`) takes an optional `mailbox`: the email address of a shared mailbox, or of a person who gave the requester access to their mailbox, to act in instead of the requester's own.
Exchange permissions on that mailbox decide what works: reading and changing its messages needs access to them, such as Full Access, and sending from it, as a new message or a reply, needs Full Access plus Send As or Send on Behalf.
With Send As, recipients see mail from the mailbox itself; with Send on Behalf, they see that the requester sent it on the mailbox's behalf.
A missing permission fails with `forbidden`, and Microsoft Graph cannot list which mailboxes a requester may open.
Message IDs and page tokens belong to the mailbox that returned them, so pass the same `mailbox` again.
A call with `mailbox` repeats it in every result, errors included.

Automatic replies are always the requester's own.
Their messages are plain text, and a scheduled period takes a local start and end with a time zone, like a new calendar event; Outlook schedules only a period that has not ended yet.
Turning them on names the internal message and an external audience of `internal_only`, `contactsOnly`, or `all`, plus the external message unless the audience is `internal_only`, so an approval card shows everything senders get.
Outlook Mail asks a requester to connect again when their connection lacks any of the permissions above.

Attachments are workspace files and follow the agent's [`file_access`](https://docs.mindroom.chat/configuration/agents/) setting.
Each attached file may be at most 3 MB, and all attachments of one message together about 3 MB; share larger files through OneDrive and send a link instead.
Only file attachments can be downloaded, not attached Outlook items such as forwarded messages or invitations, and not cloud links.
A later download of the same attachment name overwrites the copy in `outlook-mail-attachments/`.

## Calendar

| Function | Actions | Changes data | Purpose |
| --- | --- | --- | --- |
| `outlook_calendar_read` | `list_calendars`, `list_events`, `get_event`, `get_free_busy` | no | List calendars, list events in a window (up to 50) with attendees and meeting links, read an event description (up to 20,000 chars), and look up free/busy schedules and working hours for up to 20 people or rooms. |
| `outlook_calendar_write` | `create_event`, `update_event`, `delete_event`, `respond_to_event` | yes | Create, update, or delete an event (sending invitations, updates, or cancellations to attendees), or accept, tentatively accept, or decline a meeting invitation. |

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
Responding to a meeting also takes the organizer's email from the event as `expected_organizer`, so an approval card shows who receives the response, and fails with `target_mismatch`, naming the real organizer, when it differs.
A new event can invite at most 100 attendees.
The description of an online meeting cannot be replaced, because that would remove its join link; edit it in Outlook instead.

Listing calendars and events, reading an event, and creating, changing, and deleting events also take an optional `calendar_owner`: the email address or sign-in name of another person whose calendar the requester can open in Outlook because that person shared it with them or made them a delegate.
Reading needs at least read access to that calendar and writing needs edit or delegate access; otherwise Microsoft Graph answers `forbidden` or `not_found`.
Calendar IDs, event IDs, and page tokens from such a call belong to that person's mailbox, so follow-up calls pass the same `calendar_owner`, and a page token from another mailbox fails with `invalid_page_token`.
A calendar shared with the requester can also appear in their own calendar list, with that person as owner, and its ID works without `calendar_owner`.
On another person's calendar, that person organizes a new event, and Outlook sends its invitations, updates, and cancellations in that person's name, sent by the requester on their behalf.
A call with `calendar_owner` shows it on its approval card and repeats it in every result, errors included.
Responding to invitations covers only the requester's own invitations, and free/busy already looks up other people.
`Calendars.ReadWrite.Shared` needs no administrator consent; calendar connections made before the tool requested it lack it, so each requester reconnects once.

## To Do

| Function | Actions | Changes data | Purpose |
| --- | --- | --- | --- |
| `microsoft_todo_read` | `list_lists`, `list_tasks` | no | List task lists (including shared lists, default Tasks list, and flagged emails) and list up to 50 open or completed tasks with notes up to 2,000 characters. |
| `microsoft_todo_write` | `create_task`, `update_task`, `delete_task` | yes | Create a task, update its title, due date, notes, importance, or completion status, or delete a task. |

The lists include the requester's default Tasks list and the list of flagged Outlook messages.
New and changed due dates take a calendar date and become midnight in the `time_zone` the agent passes, which should be the requester's zone and defaults to UTC.
Listed, created, and updated tasks return the due date, time, and zone Graph reports; a due date is midnight in the zone of whoever set it, which Graph usually reports in UTC, so the agent converts it to the requester's zone before naming the day.
List and task listings return a `next_page_token` while more results exist; passing it back continues the same listing.
Updating a task's notes replaces them completely.

## Teams

| Function | Actions | Changes data | Purpose |
| --- | --- | --- | --- |
| `microsoft_teams_read` | `list_teams`, `list_channels`, `list_channel_messages`, `list_channel_message_replies`, `list_chats`, `list_chat_messages`, `get_presence` | no | List teams, channels, top-level channel posts and thread replies, chats and chat messages (up to 50), and availability/status presence for up to 20 people. |
| `microsoft_teams_send` | `send_channel_message`, `reply_to_channel_message`, `send_chat_message`, `start_chat` | yes, posts to Teams | Post a channel message, reply in a channel thread, send a message in an existing chat, or start a new one-on-one or group chat (up to 20 people) and send its first message. |

Messages come back as plain text, up to 4,000 characters each.
Deleted messages and system events, such as members joining, come back labeled, without text.
Channel, chat, and message listings return a `next_page_token` while more results exist; passing it back continues the same listing.
Sent messages appear under the requester's name and keep their line breaks, and HTML in the text shows literally instead of as formatting.
A sent message holds up to 28,000 bytes once its text is HTML-escaped.
A chat message takes the email address of every chat member, the requester's own included, as `recipients`, and a channel post or reply takes the team and channel names as `expected_team` and `expected_channel`, so an approval card shows who reads the message.
When a member's email address is missing or not listed, or a name differs, nothing is sent and the call fails with `recipients_mismatch`, listing every member, or `target_mismatch`, naming the real team and channel.
Starting a chat takes the other people's sign-in addresses (userPrincipalName), usually their email addresses, so its approval card shows who reads the first message.
One person gives a one-on-one chat, and Teams keeps one per pair, so the message lands in the existing one-on-one chat when there is one; two or more people give a new group chat, which can have a topic.
Before posting, it reads the chat's members and sends nothing, failing with `recipients_mismatch` and the `chat_id`, when a member's email address is missing or differs from the addresses given.
Presence looks up each person's user id by sign-in address, reports a person Microsoft Graph cannot find or show with `not_found` or `forbidden` without failing the others, and returns status messages as plain text.
Connections made before `microsoft_teams` requested `Chat.ReadWrite`, `Presence.Read.All`, and `User.Read.All` ask their users to connect again once.
The tool sees private and shared channels only when the requester is a member.
Shared channels from a team the requester is not a member of are not reachable.

## Teams Meetings

| Function | Actions | Changes data | Purpose |
| --- | --- | --- | --- |
| `microsoft_meetings_read` | `find`, `list_transcripts`, `get_transcript` | no | Find a Teams meeting by join link (subject, times, organizer, expiry), list its transcripts, and read a transcript as speaker-labeled timestamped lines (up to 30,000 characters per call). |

To read a meeting's transcript, the agent takes the event's `join_url` from `outlook_calendar_read` (`list_events` or `get_event`), finds the meeting, lists its transcripts, and reads one.
Transcript lines look like `00:12:03 Alex Doe: text`, timed from the start of transcription, with each speaker's consecutive sentences joined into lines of up to about 2,000 characters.
A long transcript comes back in chunks with a `next_page_token`; passing it back continues the same transcript, and transcript listings page the same way.
A recurring meeting series is one meeting, so its listing holds a transcript for each occurrence that was transcribed.

Transcripts exist only for meetings in which someone turned on transcription.
Microsoft Graph serves them only to the meeting's organizer and the people its calendar invite names, only for meetings scheduled with a calendar event, and not for live events.
Graph no longer serves the transcripts of an expired meeting; a one-time meeting expires 60 days after its scheduled time, extended by 60 days whenever someone joins or changes it, and `microsoft_meetings_read` (`action="find"`) reports when a meeting expires.
Two Teams administrator settings also govern transcripts: with Graph access to transcripts turned off, listing and reading transcripts return `transcripts_disabled`, and with speaker attribution turned off, reading a transcript returns `speaker_attribution_disabled`.

## OneNote

| Function | Actions | Changes data | Purpose |
| --- | --- | --- | --- |
| `microsoft_onenote_read` | `list_notebooks`, `list_sections`, `list_pages`, `get_page` | no | List notebooks (owned, team, site, or shared), top-level sections, and up to 50 pages per section, and read page content as plain text in 30,000-character chunks. |
| `microsoft_onenote_write` | `create_page`, `append_to_page` | yes | Create a page with a title and plain-text content in a section, or append a plain-text paragraph to an existing page. |

Without `container`, the functions reach the notebooks the requester owns.
Both functions also take an optional `container` naming where other notebooks are, told apart by its form: a Teams team's id from `microsoft_teams_read` (`action="list_teams"`; a team's id is its Microsoft 365 group's id), a SharePoint site's id from `microsoft_sharepoint_read` (`search_sites` or `get_site`, `host,guid,guid`), or a person's sign-in email address (user principal name) for the notebooks that person shares with the requester.
A bare id always names a team, so a person is named by sign-in email address and not by id; a container of any other form is refused before any request.
Results repeat the `container`, and calls on the sections and pages of those notebooks pass it again; `Notes.ReadWrite.All` already covers every notebook the requester can open, so containers need no new consent.
Sections inside section groups are not listed.
Page text leaves out the title, shows images as `[image]`, and separates table cells with `|`.
Listings and page text return a `next_page_token` while more follows; passing it back with the same container and the same notebook, section, or page continues.
Created and appended text keeps its line breaks, and HTML in it shows literally instead of as formatting.
Creating and appending also take the `web_url` of the notebook they write into, from `microsoft_onenote_read` (`action="list_notebooks"`), as `expected_web_url`, so an approval card shows whose notebook the write lands in, and fail with `target_mismatch`, naming the notebook's `web_url`, when it differs.

## People

| Function | Actions | Changes data | Purpose |
| --- | --- | --- | --- |
| `microsoft_people_read` | `search`, `get_profile`, `list_direct_reports` | no | Search organization directory people by name or email prefix (up to 50), read one person's profile and manager, and list direct reports. |

Agents use `microsoft_people_read` to turn a name the requester mentions into a person and an email address for the mail, calendar, and Teams tools.
Each person comes back with their id, name, email address, job title, department, office location, and business phones; mobile phones and other personal fields are never read.
The email address is the person's primary email address, or their sign-in name when they have none.
Profile and direct report lookups take a person's id, email address, or sign-in name, or `me` for the requester, so "who is my manager" needs no email address.
Search matches names by whole words or their beginnings in any order, and email addresses from their beginning, and someone added to the directory in the last few minutes may not show up yet.
A person without a manager, or whose manager is a contact instead of a user, comes back with `manager` set to `null`, and direct report listings leave out contacts.
Direct report listings return a `next_page_token` while more results exist; passing it back continues the same listing.
Guest accounts cannot search the directory.

## Approvals For Writes

The tools run without approval unless you add [tool approval](https://docs.mindroom.chat/tool-approval/) rules, which show a Matrix approval card before each change.
Timed approval grants on action-dispatched Microsoft 365 functions are scoped per `(function, action)` pair, so approving one action never grants a different action on the same function:

```yaml
tool_approval:
  rules:
    - match: microsoft_files_write
      action: require_approval
    - match: microsoft_sharepoint_write
      action: require_approval
    - match: outlook_mail_send
      action: require_approval
    - match: outlook_mail_manage
      action: require_approval
    - match: outlook_calendar_write
      action: require_approval
    - match: microsoft_todo_write
      action: require_approval
    - match: microsoft_teams_send
      action: require_approval
    - match: microsoft_onenote_write
      action: require_approval
```

Sent mail cannot be recalled, so require approval at least for `outlook_mail_send` (both `send` and `reply`), and likewise for `microsoft_teams_send`, whose messages everyone in the channel or chat sees at once.
The approval cards of `outlook_mail_send` list every recipient, and a reply is sent only to the recipients its card lists.
A card without `mailbox` sends as the requester; with it, the card names the shared mailbox the mail goes out from.
`outlook_mail_create_draft` and mailbox-organization actions on `outlook_mail_manage` (`mark_read`, `move`) change only the mailbox they name, the requester's own by default, where they can be undone; colleagues who share a mailbox see those changes too.
Automatic replies (`outlook_mail_manage` with `action="set_automatic_replies"`) answer every sender while they are on, outside the organization too when `external_audience` allows it.
Every calendar write (`outlook_calendar_write`) can email invitations, updates, cancellations, or responses to other people.

To give an agent read access only, set `write: false` on the tool.

## Troubleshooting

| Result | Cause and fix |
| --- | --- |
| `AADSTS50011` on the Microsoft sign-in page | The redirect URI is not registered on the app; add the exact URI from [Set Up The Microsoft Entra App](#set-up-the-microsoft-entra-app). |
| `AADSTS65001` or a consent prompt the requester cannot approve | The tenant requires administrator consent, which Teams, Teams meeting transcripts, and the people directory always do; an administrator grants it on the app's **API permissions** page. |
| `AADSTS50194` | The app is registered for one organization; set `MICROSOFT_TENANT_ID`. |
| `invalid_tenant` | `MICROSOFT_TENANT_ID` is not a tenant ID, a verified domain, `organizations`, `common`, or `consumers`. |
| `forbidden` | The connected account lacks access to that item, such as a shared mailbox without the Exchange permission, which reconnecting does not fix. |
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
| `graph_error` | Microsoft Graph refused the request, such as a time zone Outlook does not support or a response to a meeting the requester organizes; `graph_code` and `graph_message` give Microsoft Graph's reason. |

Apart from `throttled`, errors that Microsoft Graph returns carry its error code as `graph_code` and, when Graph gives one, its reason as `graph_message`: one line of plain text without links, cut to 300 characters.
When Microsoft rejects the connection's access token, the result includes a reconnect link.

## Security Notes

- The tools run in the primary MindRoom runtime, so worker containers never receive the app client configuration or user tokens.
- Tokens are sent only to Microsoft Graph, never to another host, including the SharePoint hosts that serve downloads.
- Request logs never contain tokens or pre-authenticated download links.
- Error results never contain tokens or raw response bodies, only Graph's error code and its trimmed reason.

## Related Docs

- [OAuth Integration Framework](https://docs.mindroom.chat/oauth-framework/)
- [Tool Approval](https://docs.mindroom.chat/tool-approval/)
