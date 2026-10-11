# Messaging & Social

This page covers the built-in tools that read and send email, post into team chats, send SMS and WhatsApp messages, work with X and Reddit, and manage Zoom meetings.
Use it to pick a tool, configure its credentials, and understand why a tool is unavailable or a send failed.

## Choosing a Tool

| Tool | Use it for | Setup |
| --- | --- | --- |
| [`gmail`](#gmail) | Reading, searching, drafting, sending, and labeling mail in a Gmail mailbox | Google Gmail OAuth |
| [`slack`](#slack) | Slack messages, threaded replies, channel and user lookups, history, search, and files | Bot or user token |
| [`discord`](#discord) | Discord channel messages, channel info and history, and message deletion | Bot token |
| [`telegram`](#telegram) | Text and media sent by a Telegram bot to one fixed chat | Bot token and chat ID |
| [`whatsapp`](#whatsapp) | WhatsApp Business text, template, interactive, and media messages | Access token and phone number ID |
| [`twilio`](#twilio) | SMS sends, call lookups, and recent message listing | Account SID plus auth token or API key |
| [`webex`](#webex) | Webex room messages and room listing | Access token |
| [`resend`](#resend) | Transactional HTML email through Resend | API key and sender |
| [`email`](#email) | Plain-text mail to one fixed recipient through Gmail SMTP | Gmail address and app password |
| [`x`](#x) | X posts, replies, DMs, profile lookups, home timeline, and recent-post search | Bearer token or OAuth user credentials |
| [`reddit`](#reddit) | Reddit user and subreddit reads, plus optional posts and replies | App client ID and secret |
| [`zoom`](#zoom) | Scheduling, listing, inspecting, and deleting Zoom meetings and reading recordings | Server-to-Server OAuth app |

For Amazon SES email, see `aws_ses` in [Automation & Platforms](https://docs.mindroom.chat/tools/automation-and-platforms/).

## Credentials

Set fields of type `password` through the dashboard or credential store; see [Security Restrictions](https://docs.mindroom.chat/tools/#security-restrictions).
Many credential fields below are optional in the dashboard but required in practice, and the tool fails to load or errors on its first call without them.
Instead of stored fields, these tools also read the environment variables `SLACK_TOKEN`, `SLACK_USER_TOKEN`, `DISCORD_BOT_TOKEN`, `TELEGRAM_TOKEN`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_API_KEY`, `TWILIO_API_SECRET`, `WEBEX_ACCESS_TOKEN`, `RESEND_API_KEY`, `X_BEARER_TOKEN`, `X_CONSUMER_KEY`, `X_CONSUMER_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`, `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME`, `REDDIT_PASSWORD`, `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, and `ZOOM_CLIENT_SECRET`.
`gmail` instead connects through Google OAuth; see [Google Services OAuth For Local Installs](https://docs.mindroom.chat/deployment/google-services-user-oauth/) or [Google Services OAuth](https://docs.mindroom.chat/deployment/google-services-oauth/) for custom and hosted setups.
Most tools enable or disable individual functions with `enable_<function>` flags, and `all: true` enables every function; `include_tools` and `exclude_tools` filter further, as described in [Filtering Toolkit Functions](https://docs.mindroom.chat/tools/#filtering-toolkit-functions).

## [`gmail`]

`gmail` reads, searches, drafts, sends, replies to, stars, and labels mail in the connected Gmail account.
Connect the account through the `google_gmail` OAuth provider; when no usable connection exists, the tool replies with a link to connect.
To use a Google Workspace service account instead, set `GOOGLE_SERVICE_ACCOUNT_FILE` to the key file path and `GOOGLE_DELEGATED_USER` to the mailbox to impersonate through domain-wide delegation.
`gmail` always runs in the primary MindRoom runtime, even for agents whose other tools run in a worker, so workers never receive Google credentials.

Its functions are `apply_label()`, `create_draft_email()`, `delete_custom_label()`, `get_draft()`, `get_emails_by_context()`, `get_emails_by_date()`, `get_emails_by_thread()`, `get_emails_from_user()`, `get_latest_emails()`, `get_message()`, `get_starred_emails()`, `get_thread()`, `get_unread_emails()`, `list_custom_labels()`, `list_drafts()`, `mark_email_as_read()`, `mark_email_as_unread()`, `remove_label()`, `search_emails()`, `search_threads()`, `send_email()`, `send_email_reply()`, `send_email_to_self()`, `star_email()`, `unstar_email()`, and `update_draft()`.
Archiving, trashing, attachment downloads, and sending existing drafts are not available.
`send_email_to_self(subject, body)` always sends to the connected account's own address, so an approval rule can allow it while still requiring approval for `send_email`; see [Tool Approval](https://docs.mindroom.chat/tool-approval/).

Draft and send functions accept attachment file paths, not Matrix attachment IDs.
Allowed paths follow the agent's [`file_access`](https://docs.mindroom.chat/architecture/security-posture/#file-access) setting: with the default `workspace`, attachments must be files inside the agent workspace, and an agent without a workspace cannot attach files.
With `unrestricted`, any file MindRoom can read is allowed.
Relative paths resolve from the workspace root, or from the MindRoom working directory when an `unrestricted` agent has no workspace.
Attachments may total at most 25 MiB per message; larger sets fail with `Gmail attachments exceed the 25 MiB limit`.

Each option below defaults to `true`, and setting it to `false` removes the matching function.

| Option | Type | Default | Controls |
| --- | --- | --- | --- |
| `get_latest_emails` | `boolean` | `true` | `get_latest_emails()` |
| `get_emails_from_user` | `boolean` | `true` | `get_emails_from_user()` |
| `get_unread_emails` | `boolean` | `true` | `get_unread_emails()` |
| `get_starred_emails` | `boolean` | `true` | `get_starred_emails()` |
| `search_emails` | `boolean` | `true` | `search_emails()` |
| `create_draft_email` | `boolean` | `true` | `create_draft_email()` |
| `send_email` | `boolean` | `true` | `send_email()` |
| `send_email_reply` | `boolean` | `true` | `send_email_reply()` |

Use `exclude_tools` to remove other functions, such as `delete_custom_label`.

## [`slack`]

`slack` sends messages and threaded replies, lists channels and users, reads channel history and threads, searches messages, and uploads or downloads files.
Use channel IDs rather than names, especially for threaded replies and history reads.
`get_channel_history()` returns only top-level messages; to read a thread's replies, enable `get_thread()` and pass the parent message's timestamp.
`slack` always runs in the primary runtime, and its file uploads and downloads are not confined by the agent's `file_access`, so enable it only for agents trusted with what the MindRoom process can reach; see [File access](https://docs.mindroom.chat/architecture/security-posture/#file-access).

To search messages, set `enable_search_messages: true` and store a user token with the [`search:read`](https://docs.slack.dev/reference/methods/search.messages/) scope in `user_token`; a bot token alone cannot search.
A user token stored as the primary `token` also works for search.
`search_workspace()` works only when Slack itself invokes the agent and supplies an action token, so it fails in ordinary Matrix conversations; use `search_messages()` there.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `token` | `password` | `null` | Bot or user token, or `SLACK_TOKEN`; required. |
| `user_token` | `password` | `null` | User token for `search_messages()`, or `SLACK_USER_TOKEN`. |
| `markdown` | `boolean` | `true` | Render Slack markdown in sent messages. |
| `output_directory` | `text` | `null` | Directory for saved downloads; setting it also enables saving. |
| `save_downloads` | `boolean` | `false` | Save downloads to disk instead of returning their content; without `output_directory`, files go to the current working directory. |
| `enable_send_message` | `boolean` | `true` | Enable `send_message()`. |
| `enable_send_message_thread` | `boolean` | `true` | Enable `send_message_thread()`. |
| `enable_list_channels` | `boolean` | `true` | Enable `list_channels()`. |
| `enable_get_channel_history` | `boolean` | `true` | Enable `get_channel_history()`. |
| `enable_upload_file` | `boolean` | `true` | Enable `upload_file()`. |
| `enable_download_file` | `boolean` | `true` | Enable `download_file()`. |
| `enable_search_messages` | `boolean` | `false` | Enable `search_messages()`; needs a user token. |
| `enable_search_workspace` | `boolean` | `false` | Enable `search_workspace()`. |
| `enable_get_thread` | `boolean` | `false` | Enable `get_thread()`. |
| `enable_list_users` | `boolean` | `false` | Enable `list_users()`. |
| `enable_get_user_info` | `boolean` | `false` | Enable `get_user_info()`. |
| `enable_get_channel_info` | `boolean` | `false` | Enable `get_channel_info()`. |
| `all` | `boolean` | `false` | Enable every function. |
| `max_file_size` | `number` | `1073741824` | Maximum upload or download size in bytes. |
| `thread_message_limit` | `number` | `20` | Maximum messages returned by `get_thread()`. |

```yaml
agents:
  support:
    tools:
      - slack:
          enable_get_thread: true
          enable_search_messages: true
```

## [`discord`]

`discord` sends messages, reads channel messages and info, lists a server's channels, and deletes messages through the Discord REST API with a bot token.
It does not run a live Discord bot, so it cannot receive messages, handle slash commands, or set presence.
Functions take Discord IDs such as channel, server, and message IDs, not names.
Deletion is enabled by default; set `enable_delete_message: false` for read and send access only.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `bot_token` | `password` | `null` | Discord bot token, or `DISCORD_BOT_TOKEN`; required. |
| `enable_send_message` | `boolean` | `true` | Enable `send_message()`. |
| `enable_get_channel_messages` | `boolean` | `true` | Enable `get_channel_messages()`. |
| `enable_get_channel_info` | `boolean` | `true` | Enable `get_channel_info()`. |
| `enable_list_channels` | `boolean` | `true` | Enable `list_channels()`. |
| `enable_delete_message` | `boolean` | `true` | Enable `delete_message()`. |
| `all` | `boolean` | `false` | Enable every function. |

## [`telegram`]

`telegram` sends messages from a Telegram bot to the one chat or channel set in `chat_id` or `TELEGRAM_CHAT_ID`, so calls never choose a destination.
For several Telegram destinations, configure the tool separately per agent or credential scope.
Only `send_message()` is enabled by default.
Optional functions send photos, documents, video, audio, animations, and stickers, edit, delete, and pin messages, react with emoji, read chat details with `get_chat()`, and download files with `get_file()`.
`get_file()` returns file content unless `save_downloads` or `output_directory` is set, in which case it saves the file and returns its path.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `chat_id` | `text` | yes | `null` | Chat or channel ID every call targets, or `TELEGRAM_CHAT_ID`. |
| `token` | `password` | no | `null` | Bot token, or `TELEGRAM_TOKEN`; required in practice. |
| `output_directory` | `text` | no | `null` | Directory for downloaded files; setting it also enables saving. |
| `save_downloads` | `boolean` | no | `false` | Save downloads to disk; without `output_directory`, files go to the current working directory. |
| `enable_send_message` | `boolean` | no | `true` | Enable `send_message()`. |
| `enable_send_photo` | `boolean` | no | `false` | Enable `send_photo()`. |
| `enable_send_document` | `boolean` | no | `false` | Enable `send_document()`. |
| `enable_send_video` | `boolean` | no | `false` | Enable `send_video()`. |
| `enable_send_audio` | `boolean` | no | `false` | Enable `send_audio()`. |
| `enable_send_animation` | `boolean` | no | `false` | Enable `send_animation()`. |
| `enable_send_sticker` | `boolean` | no | `false` | Enable `send_sticker()`. |
| `enable_edit_message` | `boolean` | no | `false` | Enable `edit_message()`. |
| `enable_delete_message` | `boolean` | no | `false` | Enable `delete_message()`. |
| `enable_react_with_emoji` | `boolean` | no | `false` | Enable `react_with_emoji()`. |
| `enable_pin_message` | `boolean` | no | `false` | Enable `pin_message()`. |
| `enable_get_chat` | `boolean` | no | `false` | Enable `get_chat()`. |
| `enable_get_file` | `boolean` | no | `false` | Enable `get_file()`. |
| `all` | `boolean` | no | `false` | Enable every function. |

```yaml
agents:
  notifier:
    tools:
      - telegram:
          chat_id: "-1001234567890"
          enable_send_document: true
```

## [`whatsapp`]

`whatsapp` sends messages through the WhatsApp Business (Meta Graph) API.
Text and template messages are enabled by default; reply buttons, list messages, images, documents, locations, and reactions are opt-in.
Template sends need a template name approved in WhatsApp Business.
Set `recipient_waid` or `WHATSAPP_RECIPIENT_WAID` to give calls a default recipient; otherwise, every call must name one.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `access_token` | `password` | `null` | Business API access token, or `WHATSAPP_ACCESS_TOKEN`; required. |
| `phone_number_id` | `text` | `null` | Business phone number ID, or `WHATSAPP_PHONE_NUMBER_ID`; required. |
| `version` | `text` | `v22.0` | Graph API version, or `WHATSAPP_VERSION`. |
| `recipient_waid` | `text` | `null` | Default recipient phone number or WhatsApp ID. |
| `timeout` | `number` | `30` | Per-request HTTP timeout in seconds. |
| `enable_send_text_message` | `boolean` | `true` | Enable `send_text_message()`. |
| `enable_send_template_message` | `boolean` | `true` | Enable `send_template_message()`. |
| `enable_send_reply_buttons` | `boolean` | `false` | Enable `send_reply_buttons()`. |
| `enable_send_list_message` | `boolean` | `false` | Enable `send_list_message()`. |
| `enable_send_image` | `boolean` | `false` | Enable `send_image()`. |
| `enable_send_document` | `boolean` | `false` | Enable `send_document()`. |
| `enable_send_location` | `boolean` | `false` | Enable `send_location()`. |
| `enable_send_reaction` | `boolean` | `false` | Enable `send_reaction()`. |
| `all` | `boolean` | `false` | Enable every function. |

```yaml
agents:
  pager:
    tools:
      - whatsapp:
          recipient_waid: "+15551234567"
          enable_send_reply_buttons: true
```

## [`twilio`]

`twilio` sends SMS with `send_sms()`, looks up calls with `get_call_details()`, and lists recent messages with `list_messages()`.
Authenticate with `account_sid` plus either `auth_token` or the `api_key` and `api_secret` pair.
`send_sms()` rejects sender and recipient numbers that are not in E.164 format, such as `+15551234567`, and the sender must be a number on the Twilio account.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `account_sid` | `text` | `null` | Account SID, or `TWILIO_ACCOUNT_SID`; required. |
| `auth_token` | `password` | `null` | Auth token, or `TWILIO_AUTH_TOKEN`. |
| `api_key` | `password` | `null` | API key, or `TWILIO_API_KEY`; used with `api_secret`. |
| `api_secret` | `password` | `null` | API secret, or `TWILIO_API_SECRET`; used with `api_key`. |
| `region` | `text` | `null` | Twilio region, such as `au1`. |
| `edge` | `text` | `null` | Twilio edge location, such as `sydney`. |
| `debug` | `boolean` | `false` | Log Twilio HTTP requests. |
| `enable_send_sms` | `boolean` | `true` | Enable `send_sms()`. |
| `enable_get_call_details` | `boolean` | `true` | Enable `get_call_details()`. |
| `enable_list_messages` | `boolean` | `true` | Enable `list_messages()`. |
| `all` | `boolean` | `false` | Enable every function. |

## [`webex`]

`webex` sends messages to Webex rooms with `send_message()` and lists rooms with `list_rooms()`.
It does not manage meetings.
Send to room IDs, not room titles.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `access_token` | `password` | `null` | Webex access token, or `WEBEX_ACCESS_TOKEN`; required. |
| `enable_send_message` | `boolean` | `true` | Enable `send_message()`. |
| `enable_list_rooms` | `boolean` | `true` | Enable `list_rooms()`. |
| `all` | `boolean` | `false` | Enable every function. |

## [`resend`]

`resend` sends transactional email through Resend with `send_email()`, using the message body as HTML.
Every message comes from `from_email`, which usually must be a sender verified in Resend.
Use `gmail` instead when the agent needs to read or reply within a mailbox.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Resend API key, or `RESEND_API_KEY`; required. |
| `from_email` | `text` | `null` | Sender address for every message. |
| `enable_send_email` | `boolean` | `true` | Enable `send_email()`. |
| `all` | `boolean` | `false` | Enable every function. |

## [`email`]

`email` sends plain-text mail with `email_user(subject, body)` to the single address in `receiver_email`; calls cannot choose another recipient.
It always signs in to Gmail SMTP (`smtp.gmail.com:465`), so the sender must be a Gmail or Google Workspace account, usually with an app password, and other SMTP servers cannot be configured.
All four fields are required; a missing one makes the call return an error such as `error: No receiver email provided`.
Use `gmail` for mailbox access or `resend` for HTML transactional mail.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `receiver_email` | `text` | `null` | Recipient of every message. |
| `sender_name` | `text` | `null` | Display name in the `From` header. |
| `sender_email` | `text` | `null` | Gmail address used to sign in and send. |
| `sender_passkey` | `password` | `null` | Gmail app password. |
| `enable_email_user` | `boolean` | `true` | Enable `email_user()`. |
| `all` | `boolean` | `false` | Enable every function. |

```yaml
agents:
  alerts:
    tools:
      - email:
          receiver_email: oncall@example.com
          sender_name: MindRoom Alerts
          sender_email: alerts@gmail.com
```

## [`x`]

`x` creates posts, replies to posts, sends DMs, looks up users, reads the home timeline, and searches recent posts.
A bearer token is enough for search and lookups, but posting, replying, DMs, and the home timeline generally need the four OAuth user credentials.
`search_posts()` accepts a result limit of 10 to 100, and may return fewer matches.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `bearer_token` | `password` | `null` | Bearer token, or `X_BEARER_TOKEN`. |
| `consumer_key` | `password` | `null` | OAuth consumer key, or `X_CONSUMER_KEY`. |
| `consumer_secret` | `password` | `null` | OAuth consumer secret, or `X_CONSUMER_SECRET`. |
| `access_token` | `password` | `null` | OAuth access token, or `X_ACCESS_TOKEN`. |
| `access_token_secret` | `password` | `null` | OAuth access token secret, or `X_ACCESS_TOKEN_SECRET`. |
| `include_post_metrics` | `boolean` | `false` | Add reply, repost, like, and quote counts to search results. |
| `wait_on_rate_limit` | `boolean` | `false` | Wait out rate limits instead of failing. |

## [`reddit`]

`reddit` reads user profiles, top posts, subreddit info and stats, and trending subreddits, and can create posts and reply to posts and comments.
`client_id` and `client_secret` are enough for reads; posting and replying also need `username` and `password`.
A nonempty `allowed_subreddits` list limits posts and replies to those subreddits, named without `r/` and matched case-insensitively; reads are unaffected.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `client_id` | `text` | `null` | Reddit app client ID, or `REDDIT_CLIENT_ID`; required. |
| `client_secret` | `password` | `null` | Reddit app client secret, or `REDDIT_CLIENT_SECRET`; required. |
| `user_agent` | `text` | `null` | Custom user agent, or `REDDIT_USER_AGENT`; defaults to `RedditTools v1.0`. |
| `username` | `text` | `null` | Reddit username for posts and replies. |
| `password` | `password` | `null` | Reddit password for posts and replies. |
| `allowed_subreddits` | `string[]` | `null` | Subreddits permitted for posts and replies; `null` or `[]` allows all. |
| `reddit_instance` | `text` | `null` | Programmatic only: an existing PRAW `Reddit` client, not settable from YAML. |

```yaml
agents:
  community:
    tools:
      - reddit:
          user_agent: MindRoomCommunityBot/1.0
          allowed_subreddits: [matrixdotorg]
```

## [`zoom`]

`zoom` schedules, lists, inspects, and deletes Zoom meetings and reads meeting recordings.
It uses the credentials of a Zoom Server-to-Server OAuth app, so there is no browser connect step.
Meetings are scheduled for the app account's own user with fixed settings: automatic recording is off, and participants cannot join before the host.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `account_id` | `text` | `null` | Account ID of the Server-to-Server OAuth app, or `ZOOM_ACCOUNT_ID`; required. |
| `client_id` | `text` | `null` | Client ID, or `ZOOM_CLIENT_ID`; required. |
| `client_secret` | `password` | `null` | Client secret, or `ZOOM_CLIENT_SECRET`; required. |
| `timeout` | `number` | `30` | Per-request HTTP timeout in seconds. |

## Related Docs

- [Tools Overview](https://docs.mindroom.chat/tools/)
- [Automation & Platforms](https://docs.mindroom.chat/tools/automation-and-platforms/) - `aws_ses` for Amazon SES email.
