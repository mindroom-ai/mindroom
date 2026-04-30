---
icon: lucide/wrench
---

# Messaging & Social

Use these tools to read and send email, post into chat systems, deliver SMS or WhatsApp messages, work with social platforms, and schedule or inspect meetings.

## What This Page Covers

This page documents the built-in tools in the `messaging-and-social` group.
Use these tools when you need outbound communication, mailbox access, team-chat delivery, social/community lookups, or Zoom meeting management.

## Tools On This Page

- [`gmail`] - Gmail mailbox access and message composition through the Google Gmail OAuth provider.
- [`slack`] - Slack channel messaging, threaded replies, channel listing, and history reads.
- [`discord`] - Discord bot messaging, channel inspection, history reads, and message deletion.
- [`telegram`] - Telegram bot delivery to one configured chat.
- [`whatsapp`] - WhatsApp Business API text and template messaging.
- [`twilio`] - Twilio SMS delivery, call lookup, and recent message listing.
- [`webex`] - Webex room messaging and room listing.
- [`resend`] - Transactional email delivery through Resend.
- [`email`] - Simple SMTP email sending through Gmail SMTP.
- [`x`] - X posting, replying, DMs, profile lookup, timeline reads, and recent-post search.
- [`reddit`] - Reddit read access plus optional posting and replies with user auth.
- [`zoom`] - Zoom Server-to-Server OAuth meeting scheduling and management.

## Common Setup Notes

`gmail` uses `auth_provider="google_gmail"` and connects through the generic `/api/oauth/google_gmail/*` flow.
Its OAuth tokens are stored separately from editable Gmail tool settings.
`homeassistant` stays local even when other tools are routed through the sandbox proxy.
MindRoom enforces that restriction both at config-validation time and again during tool construction.
Password fields should be stored through the dashboard or credential store instead of inline YAML.
Several metadata fields on this page are marked `required: false`, but the installed SDKs still need the corresponding token or secret in practice.
Useful environment fallbacks on this page include `SLACK_TOKEN`, `DISCORD_BOT_TOKEN`, `TELEGRAM_TOKEN`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `WEBEX_ACCESS_TOKEN`, `RESEND_API_KEY`, `X_BEARER_TOKEN`, `X_CONSUMER_KEY`, `X_CONSUMER_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`, `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME`, `REDDIT_PASSWORD`, `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, and `ZOOM_CLIENT_SECRET`.
The generic-looking `email` tool is not a fully configurable SMTP client on this branch.
Its installed upstream implementation is hard-wired to Gmail SMTP over `smtp.gmail.com:465` and does not expose SMTP host, port, or TLS configuration fields.

## [`gmail`]

`gmail` is the mailbox-oriented tool for reading, searching, drafting, sending, and labeling Gmail messages through the Google Gmail OAuth provider.

### What It Does

MindRoom wraps Agno's `GmailTools` with `ScopedOAuthClientMixin`, so Gmail credentials are loaded from MindRoom's scoped OAuth credential store instead of a local `token.json` file.
The wrapper refreshes stored Google tokens when needed and raises `OAuthConnectionRequired` with a connect URL when no usable stored OAuth credentials are available.
It only bypasses MindRoom OAuth when configured for Google service-account auth.
The current installed Gmail toolkit exposes `get_latest_emails()`, `get_emails_from_user()`, `get_unread_emails()`, `get_starred_emails()`, `get_emails_by_context()`, `get_emails_by_date()`, `get_emails_by_thread()`, `search_emails()`, `create_draft_email()`, `send_email()`, `send_email_reply()`, `mark_email_as_read()`, `mark_email_as_unread()`, `list_custom_labels()`, `apply_label()`, `remove_label()`, and `delete_custom_label()`.
Draft and send operations accept local file-system paths for attachments.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `get_latest_emails` | `boolean` | `no` | `true` | Declared in MindRoom's registry metadata as a capability flag. |
| `get_emails_from_user` | `boolean` | `no` | `true` | Declared in MindRoom's registry metadata as a capability flag. |
| `get_unread_emails` | `boolean` | `no` | `true` | Declared in MindRoom's registry metadata as a capability flag. |
| `get_starred_emails` | `boolean` | `no` | `true` | Declared in MindRoom's registry metadata as a capability flag. |
| `search_emails` | `boolean` | `no` | `true` | Declared in MindRoom's registry metadata as a capability flag. |
| `create_draft_email` | `boolean` | `no` | `true` | Declared in MindRoom's registry metadata as a capability flag. |
| `send_email` | `boolean` | `no` | `true` | Declared in MindRoom's registry metadata as a capability flag. |
| `send_email_reply` | `boolean` | `no` | `true` | Declared in MindRoom's registry metadata as a capability flag. |

### Example

```yaml
agents:
  assistant:
    worker_scope: shared
    tools:
      - gmail
```

```python
get_latest_emails(10)
search_emails("label:unread from:billing@example.com", 10)
send_email("alice@example.com", "Project update", "Here is the latest status.")
send_email_reply("thread_id", "message_id", "alice@example.com", "Re: Project update", "Thanks for the update.")
apply_label("is:unread category:promotions", "Needs Review", count=10)
```

### Notes

- Connect Gmail through the `google_gmail` OAuth provider rather than storing a Gmail-specific API key.
- The Gmail provider requests Gmail read, modify, and compose scopes.
- `gmail` can use scoped OAuth credentials for isolated worker scopes.
- The current registry exposes the eight boolean fields above, but the installed `agno.tools.gmail.GmailTools` constructor does not consume those selector kwargs directly on this branch, so `- gmail` plus Google OAuth is the verified setup path.
- Attachment arguments are local file paths in the current runtime, not Matrix attachment IDs.

## [`slack`]

`slack` is the Slack bot toolkit for posting to channels, replying in threads, listing channels, and reading recent channel history.

### What It Does

The installed upstream tool exposes `send_message()`, `send_message_thread()`, `list_channels()`, and `get_channel_history()`.
It uses Slack's `WebClient` from `slack_sdk`.
`markdown` maps to the `mrkdwn` flag on `chat_postMessage()`.
Channel-history responses are normalized into a smaller JSON structure instead of returning the full Slack API payload.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `token` | `password` | `no` | `null` | Slack bot token, or use `SLACK_TOKEN`. Required in practice. |
| `markdown` | `boolean` | `no` | `true` | Enable Slack markdown rendering on sent messages. |
| `enable_send_message` | `boolean` | `no` | `true` | Enable `send_message()`. |
| `enable_send_message_thread` | `boolean` | `no` | `true` | Enable `send_message_thread()`. |
| `enable_list_channels` | `boolean` | `no` | `true` | Enable `list_channels()`. |
| `enable_get_channel_history` | `boolean` | `no` | `true` | Enable `get_channel_history()`. |
| `all` | `boolean` | `no` | `false` | Enable the full upstream toolkit surface. |

### Example

```yaml
agents:
  support:
    tools:
      - slack:
          markdown: true
          enable_get_channel_history: true
```

```python
list_channels()
send_message("C0123456789", "Deployment finished.")
send_message_thread("C0123456789", "Following up in the same thread.", "1743112345.678900")
get_channel_history("C0123456789", limit=25)
```

### Notes

- Although `token` is marked optional in metadata, the installed Slack toolkit raises if no token or `SLACK_TOKEN` is present.
- Use channel IDs for the most reliable calls, especially for threaded replies and history reads.
- `get_channel_history()` returns a simplified JSON view rather than the raw Slack response.

## [`discord`]

`discord` is the Discord bot toolkit for channel messaging, channel metadata, channel history, server channel listing, and message deletion.

### What It Does

The installed upstream tool exposes `send_message()`, `get_channel_messages()`, `get_channel_info()`, `list_channels()`, and `delete_message()`.
It talks directly to `https://discord.com/api/v10` with a bot token in the `Authorization` header.
This is a raw REST wrapper rather than a gateway-connected real-time bot runtime.
Most functions expect Discord IDs such as `channel_id`, `guild_id`, and `message_id`, not display names.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `bot_token` | `password` | `no` | `null` | Discord bot token, or use `DISCORD_BOT_TOKEN`. Required in practice. |
| `enable_send_message` | `boolean` | `no` | `true` | Enable `send_message()`. |
| `enable_get_channel_messages` | `boolean` | `no` | `true` | Enable `get_channel_messages()`. |
| `enable_get_channel_info` | `boolean` | `no` | `true` | Enable `get_channel_info()`. |
| `enable_list_channels` | `boolean` | `no` | `true` | Enable `list_channels()`. |
| `enable_delete_message` | `boolean` | `no` | `true` | Enable `delete_message()`. |
| `all` | `boolean` | `no` | `false` | Enable the full upstream toolkit surface. |

### Example

```yaml
agents:
  community:
    tools:
      - discord:
          enable_delete_message: false
```

```python
list_channels("123456789012345678")
get_channel_info("123456789012345678")
get_channel_messages("123456789012345678", limit=25)
send_message("123456789012345678", "Hello from MindRoom.")
```

### Notes

- `bot_token` is effectively required even though the metadata marks it optional.
- This tool uses ordinary Discord REST calls and does not manage slash commands, presence, or gateway subscriptions.
- Deletion is enabled by default, so disable `enable_delete_message` if you only want read and send access.

## [`telegram`]

`telegram` is the simplest chat-delivery tool on this page, with one configured destination and one send function.

### What It Does

The installed upstream tool exposes only `send_message()`.
It posts to Telegram Bot API `sendMessage` for the configured `chat_id`.
The tool instance is bound to one chat destination, so callers do not pass a chat ID per request.
Responses are returned as the raw Telegram API response text.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `chat_id` | `text` | `yes` | `null` | Telegram chat or channel ID that this tool instance will target. |
| `token` | `password` | `no` | `null` | Telegram bot token, or use `TELEGRAM_TOKEN`. Required in practice. |
| `enable_send_message` | `boolean` | `no` | `true` | Enable `send_message()`. |
| `all` | `boolean` | `no` | `false` | Enable the full upstream toolkit surface. |

### Example

```yaml
agents:
  notifier:
    tools:
      - telegram:
          chat_id: "-1001234567890"
```

```python
send_message("Nightly backup completed.")
```

### Notes

- Set both `chat_id` and `token` for a usable configuration.
- Because the destination chat is fixed in config, this tool is best for one bot-to-room delivery path rather than general multi-room Telegram automation.
- If you need different Telegram destinations, configure different agents or different tool credentials per scope.

## [`whatsapp`]

`whatsapp` is the WhatsApp Business API toolkit for text messages and template messages through Meta's Graph API.

### What It Does

The installed upstream tool can expose either `send_text_message_sync()` and `send_template_message_sync()` or their async variants, depending on `async_mode`.
It posts to `https://graph.facebook.com/<version>/<phone_number_id>/messages`.
`recipient_waid` sets a default recipient so callers can omit the `recipient` argument.
If no default recipient is configured, every send call must provide one.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `access_token` | `password` | `no` | `null` | WhatsApp Business API token, or use `WHATSAPP_ACCESS_TOKEN`. Required in practice. |
| `phone_number_id` | `text` | `no` | `null` | WhatsApp Business phone number ID, or use `WHATSAPP_PHONE_NUMBER_ID`. Required in practice. |
| `version` | `text` | `no` | `v22.0` | Graph API version prefix. |
| `recipient_waid` | `text` | `no` | `null` | Default recipient phone number or WhatsApp ID. |
| `async_mode` | `boolean` | `no` | `false` | Register async send functions instead of sync send functions. |

### Example

```yaml
agents:
  pager:
    tools:
      - whatsapp:
          version: v22.0
          recipient_waid: "+15551234567"
          async_mode: false
```

```python
send_text_message_sync("The deployment finished.")
send_template_message_sync(template_name="deployment_notice", language_code="en_US")
```

### Notes

- Set both `access_token` and `phone_number_id` even though the metadata marks them optional.
- `async_mode: true` changes the registered function names to the async variants, which matters if you are debugging tool traces or reading model-generated tool calls.
- Template sends expect a preapproved WhatsApp template name and, optionally, template `components`.

## [`twilio`]

`twilio` is the telecom-oriented toolkit for SMS sends, call lookups, and recent message history.

### What It Does

The installed upstream tool exposes `send_sms()`, `get_call_details()`, and `list_messages()`.
It supports two auth modes: `account_sid` plus `auth_token`, or `account_sid` plus `api_key` and `api_secret`.
Optional `region` and `edge` are passed into the Twilio client for regional routing.
`send_sms()` validates that both `to` and `from_` are in E.164 format before sending.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `account_sid` | `text` | `no` | `null` | Twilio account SID, or use `TWILIO_ACCOUNT_SID`. Required in practice. |
| `auth_token` | `password` | `no` | `null` | Twilio auth token for the simpler auth mode. |
| `api_key` | `password` | `no` | `null` | Twilio API key for key-and-secret auth. |
| `api_secret` | `password` | `no` | `null` | Twilio API secret for key-and-secret auth. |
| `region` | `text` | `no` | `null` | Optional Twilio region such as `au1`. |
| `edge` | `text` | `no` | `null` | Optional Twilio edge such as `sydney`. |
| `debug` | `boolean` | `no` | `false` | Enable Twilio HTTP client logging. |
| `enable_send_sms` | `boolean` | `no` | `true` | Enable `send_sms()`. |
| `enable_get_call_details` | `boolean` | `no` | `true` | Enable `get_call_details()`. |
| `enable_list_messages` | `boolean` | `no` | `true` | Enable `list_messages()`. |
| `all` | `boolean` | `no` | `false` | Enable the full upstream toolkit surface. |

### Example

```yaml
agents:
  phone_ops:
    tools:
      - twilio:
          region: au1
          edge: sydney
          enable_list_messages: true
```

```python
send_sms("+15551234567", "+15557654321", "Build passed.")
get_call_details("CA1234567890abcdef")
list_messages(limit=10)
```

### Notes

- `account_sid` is required in both supported auth modes.
- Use either `auth_token` or the `api_key` plus `api_secret` pair, not both as separate independent auth methods.
- `send_sms()` rejects non-E.164 numbers before it reaches Twilio's API.

## [`webex`]

`webex` is the Cisco Webex toolkit for room messaging and room listing.

### What It Does

The installed upstream tool exposes `send_message()` and `list_rooms()`.
It authenticates with `webexpythonsdk.WebexAPI` using one access token.
Despite the broader description in the registry, the current tool surface is messaging-centric and does not manage meetings.
Responses are returned as JSON strings built from the SDK result objects.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `enable_send_message` | `boolean` | `no` | `true` | Enable `send_message()`. |
| `enable_list_rooms` | `boolean` | `no` | `true` | Enable `list_rooms()`. |
| `all` | `boolean` | `no` | `false` | Enable the full upstream toolkit surface. |
| `access_token` | `password` | `no` | `null` | Webex access token, or use `WEBEX_ACCESS_TOKEN`. Required in practice. |

### Example

```yaml
agents:
  meetings:
    tools:
      - webex:
          enable_list_rooms: true
```

```python
list_rooms()
send_message("Y2lzY29zcGFyazovL3VzL1JPT00v...", "Agenda is ready.")
```

### Notes

- Set `access_token` even though metadata marks it optional.
- The current tool surface is room messaging and room discovery only.
- Use room IDs, not room titles, when sending messages.

## [`resend`]

`resend` is the transactional-email API toolkit for HTML email delivery through Resend.

### What It Does

The installed upstream tool exposes one function, `send_email()`.
It sets `resend.api_key` on each call and sends email through `resend.Emails.send()`.
The current implementation passes the message body as HTML, not plain text.
`from_email` is stored on the tool instance and reused for every call.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `api_key` | `password` | `no` | `null` | Resend API key, or use `RESEND_API_KEY`. Required in practice. |
| `from_email` | `text` | `no` | `null` | Sender identity used for every call. Usually required by the provider in practice. |
| `enable_send_email` | `boolean` | `no` | `true` | Enable `send_email()`. |
| `all` | `boolean` | `no` | `false` | Enable the full upstream toolkit surface. |

### Example

```yaml
agents:
  mailer:
    tools:
      - resend:
          from_email: no-reply@example.com
```

```python
send_email("alice@example.com", "Welcome", "<p>Your account is ready.</p>")
```

### Notes

- `from_email` is not marked required in metadata, but most Resend setups need a verified sender.
- The `body` argument is sent as HTML.
- Use `resend` when you want provider-managed transactional delivery rather than Gmail mailbox access.

## [`email`]

`email` is the simplest SMTP mailer on this page, but on this branch it is specifically wired for Gmail SMTP rather than generic SMTP.

### What It Does

The installed upstream tool exposes one function, `email_user()`.
It builds an `EmailMessage`, then logs in with `smtplib.SMTP_SSL("smtp.gmail.com", 465)`.
That means the sender account must be a Gmail account or a Google Workspace account compatible with Gmail SMTP.
The current implementation sends plain-text bodies only.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `receiver_email` | `text` | `no` | `null` | Default recipient for all `email_user()` calls. |
| `sender_name` | `text` | `no` | `null` | Display name shown in the `From` header. |
| `sender_email` | `text` | `no` | `null` | Gmail sender address used for SMTP login. |
| `sender_passkey` | `password` | `no` | `null` | Gmail app password or equivalent SMTP passkey. Required in practice. |
| `enable_email_user` | `boolean` | `no` | `true` | Enable `email_user()`. |
| `all` | `boolean` | `no` | `false` | Enable the full upstream toolkit surface. |

### Example

```yaml
agents:
  alerts:
    tools:
      - email:
          receiver_email: oncall@example.com
          sender_name: MindRoom Alerts
          sender_email: alerts@gmail.com
```

```python
email_user("Nightly report", "The report finished successfully.")
```

### Notes

- Despite the generic tool name, there are no host, port, TLS, or auth-mechanism overrides in the current implementation.
- This tool is best for simple Gmail-based outbound mail, not arbitrary SMTP providers.
- If you need mailbox reads, drafts, replies, or labels, use `gmail` instead.
- If you need provider-managed transactional delivery with HTML bodies, use `resend` instead.

## [`x`]

`x` is the X or Twitter toolkit for recent-post search, user lookup, timeline access, posting, replying, and DMs.

### What It Does

The installed upstream tool exposes `create_post()`, `reply_to_post()`, `send_dm()`, `get_user_info()`, `get_home_timeline()`, and `search_posts()`.
It uses `tweepy.Client` and passes through `wait_on_rate_limit`.
`search_posts()` calls `search_recent_tweets()` and, when `include_post_metrics` is enabled, augments each returned post with reply, retweet, like, and quote counts.
The implementation clamps `max_results` for search to the Twitter API's supported `10` to `100` range.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `bearer_token` | `password` | `no` | `null` | Bearer token, or use `X_BEARER_TOKEN`. |
| `consumer_key` | `password` | `no` | `null` | OAuth consumer key, or use `X_CONSUMER_KEY`. |
| `consumer_secret` | `password` | `no` | `null` | OAuth consumer secret, or use `X_CONSUMER_SECRET`. |
| `access_token` | `password` | `no` | `null` | OAuth access token, or use `X_ACCESS_TOKEN`. |
| `access_token_secret` | `password` | `no` | `null` | OAuth access token secret, or use `X_ACCESS_TOKEN_SECRET`. |
| `include_post_metrics` | `boolean` | `no` | `false` | Include public metrics in `search_posts()` output. |
| `wait_on_rate_limit` | `boolean` | `no` | `false` | Let Tweepy wait for rate limits instead of failing immediately. |

### Example

```yaml
agents:
  social:
    tools:
      - x:
          include_post_metrics: true
          wait_on_rate_limit: true
```

```python
search_posts("mindroom matrix", max_results=10)
get_user_info("mindroom_ai")
create_post("MindRoom now supports another release.")
reply_to_post("1890123456789012345", "Thanks for the feedback.")
```

### Notes

- A bearer token can support read-style endpoints such as search, but posting, replying, DMs, and home-timeline access generally need full OAuth user credentials.
- The toolkit also defines `get_my_info()`, but MindRoom's current registered tool list on this branch does not advertise a separate config flag for it.
- `reply_to_post()` builds a `twitter.com` URL in its response, while `create_post()` builds an `x.com` URL, so response formatting is currently inconsistent upstream.

## [`reddit`]

`reddit` is the Reddit toolkit for reading user and subreddit data, listing trending communities, and optionally posting or replying with user credentials.

### What It Does

The installed upstream tool exposes `get_user_info()`, `get_top_posts()`, `get_subreddit_info()`, `get_trending_subreddits()`, `get_subreddit_stats()`, `create_post()`, `reply_to_post()`, and `reply_to_comment()`.
With only `client_id` and `client_secret`, the tool initializes a read-only `praw.Reddit` client.
If `username` and `password` are also configured, the tool enables posting and replying with authenticated user actions.
`reddit_instance` is an advanced programmatic injection point for an existing `praw.Reddit` object rather than a normal YAML value.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `reddit_instance` | `text` | `no` | `null` | Advanced programmatic Reddit client injection, not normal YAML authoring. |
| `client_id` | `text` | `no` | `null` | Reddit app client ID, or use `REDDIT_CLIENT_ID`. Required in practice. |
| `client_secret` | `password` | `no` | `null` | Reddit app client secret, or use `REDDIT_CLIENT_SECRET`. Required in practice. |
| `user_agent` | `text` | `no` | `null` | Optional custom user agent, with `RedditTools v1.0` as the upstream fallback. |
| `username` | `text` | `no` | `null` | Reddit username for posting and replying. |
| `password` | `password` | `no` | `null` | Reddit password for posting and replying. |

### Example

```yaml
agents:
  research:
    tools:
      - reddit:
          user_agent: MindRoomResearchBot/1.0
```

```python
get_top_posts("matrixdotorg", time_filter="week", limit=10)
get_subreddit_info("python")
get_trending_subreddits()
get_user_info("spez")
```

### Notes

- `client_id` and `client_secret` are enough for read-only operations.
- `create_post()`, `reply_to_post()`, and `reply_to_comment()` additionally require `username` and `password`.
- The current MindRoom metadata treats `reddit_instance` as text, but the upstream constructor expects an already constructed `praw.Reddit` object.

## [`zoom`]

`zoom` is the Zoom meeting-management toolkit for scheduling, listing, inspecting, deleting, and reading recordings through Zoom's Server-to-Server OAuth flow.

### What It Does

The installed upstream tool exposes `get_access_token()`, `schedule_meeting()`, `get_upcoming_meetings()`, `list_meetings()`, `get_meeting_recordings()`, `delete_meeting()`, and `get_meeting()`.
It exchanges `account_id`, `client_id`, and `client_secret` against `https://zoom.us/oauth/token` with `grant_type=account_credentials`.
The generated access token is cached in process until shortly before expiry.
Meeting creation always targets `users/me/meetings` and applies a fixed set of default meeting settings in the upstream implementation.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `account_id` | `text` | `no` | `null` | Zoom account ID from a Server-to-Server OAuth app, or use `ZOOM_ACCOUNT_ID`. Required in practice. |
| `client_id` | `text` | `no` | `null` | Zoom client ID, or use `ZOOM_CLIENT_ID`. Required in practice. |
| `client_secret` | `password` | `no` | `null` | Zoom client secret, or use `ZOOM_CLIENT_SECRET`. Required in practice. |

### Example

```yaml
agents:
  coordinator:
    tools:
      - zoom:
          account_id: your_account_id
          client_id: your_client_id
```

```python
schedule_meeting("Weekly sync", "2026-04-02T16:00:00Z", 30, timezone="America/Los_Angeles")
get_upcoming_meetings()
list_meetings(type="scheduled")
get_meeting("81234567890")
```

### Notes

- Although MindRoom marks `zoom` as `setup_type: oauth`, the current implementation is not an interactive browser OAuth flow.
- The installed tool expects Server-to-Server OAuth credentials and exchanges them directly for an access token on demand.
- `get_meeting_recordings()` can request a download token and TTL per call, but those are call arguments rather than config fields.

## Related Docs

- [Tools Overview](index.md)
- [Automation & Platforms](automation-and-platforms.md) - For `aws_ses` when you want an alternative outbound-email provider.
