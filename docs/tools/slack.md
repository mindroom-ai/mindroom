---
icon: fontawesome/brands/slack
---

# Slack

The `slack` tool lets agents search and read Slack, list recent conversations, download and upload files, and post, edit, delete, and react to messages through one Slack app that your organization creates for MindRoom.
Agents can also answer mentions of that app in Slack channels, and people can have their own Slack mentions and direct messages forwarded into a Matrix room.
OAuth connection mechanics shared with other providers are on [OAuth Integration Framework](../oauth-framework.md).

An agent works in one of two modes, chosen by whether its `slack` entry lists `channels`:

| | Personal | Project |
| --- | --- | --- |
| Chosen by | no `channels` | `channels` |
| Acts as | each requester, with their own Slack account | the MindRoom Slack app |
| Reads and posts | everything that person can see in Slack, including direct messages | only the listed channels the app has joined, never direct messages |
| Slack events | a person's [watch](#watches) forwards their mentions and direct messages to Matrix | with `room`, a mention of the app [starts a thread](#project-agents) |

## Functions

| Function | Actions | Personal | Project |
| --- | --- | --- | --- |
| `slack_read` | `search` (messages, conversations, or people), `recent` (newest-first DMs or channels within `days`), `read` (latest messages or a thread), and `download` (up to 50 MB into `slack-downloads/`) | all actions; `search` supports `messages`, `conversations`, and `people`; `recent` lists `dms` or `channels` | `search` (`conversations` only), `read`, and `download` within the agent's channels (`recent` is hidden) |
| `slack_send` | `post` (message or thread reply) and `upload` (workspace file up to 50 MB with optional `title` and `comment`) | as the person; a user ID opens a direct message | as the app, in the agent's channels |
| `slack_manage` | `edit` (replace message text), `delete` (delete a message), and `react` (add an emoji reaction) | as the person (only messages Slack allows them to edit/delete) | as the app, in the agent's channels |

Functions take a conversation ID such as `C0123ABCD`, `<conversation>:<ts>` such as `C0123ABCD:1714000000.000100`, or a message link, never a channel name.
`slack_send(action="upload")` takes a path in the agent's workspace and follows the agent's [`file_access`](../configuration/agents.md) setting.
[Background scripts](background-scripts.md) cannot call `slack`.

## Set Up The Slack App

An administrator creates one Slack app for the MindRoom installation.
One MindRoom serves one Slack workspace: accounts from other workspaces cannot connect, their events are ignored, and Enterprise Grid org-wide installs are not supported.
Slack must reach MindRoom for the OAuth redirect and for events, so set `MINDROOM_PUBLIC_URL` to a public HTTPS address.

1. In Slack, open [Your Apps](https://api.slack.com/apps), choose **Create New App**, then **From a manifest**, pick the workspace, and paste this manifest with `mindroom.example.com` replaced by your public MindRoom host:

   ```yaml
   display_information:
     name: MindRoom
   features:
     app_home:
       home_tab_enabled: false
       messages_tab_enabled: false
     bot_user:
       display_name: MindRoom
       always_online: true
   oauth_config:
     redirect_urls:
       - https://mindroom.example.com/api/oauth/slack/callback
     scopes:
       bot:
         - app_mentions:read
         - channels:history
         - channels:read
         - groups:history
         - groups:read
         - chat:write
         - reactions:write
         - files:read
         - files:write
         - users:read
         - users:read.email
       user:
         - search:read
         - channels:history
         - channels:read
         - groups:history
         - groups:read
         - im:history
         - im:read
         - mpim:history
         - mpim:read
         - users:read
         - users:read.email
         - chat:write
         - im:write
         - reactions:write
         - files:read
         - files:write
   settings:
     event_subscriptions:
       request_url: https://mindroom.example.com/api/slack/events
       bot_events:
         - app_mention
         - message.channels
         - message.groups
       user_events:
         - message.channels
         - message.groups
         - message.im
         - message.mpim
     org_deploy_enabled: false
     socket_mode_enabled: false
     token_rotation_enabled: false
   ```

   The bot's `users:read.email` scope is what maps a Slack author to a MindRoom account; without it nobody can wake a project agent.
   Keep `token_rotation_enabled` and `socket_mode_enabled` off, because MindRoom stores Slack user tokens that do not expire and receives events over HTTP.

2. Install the app to the workspace and copy the **Bot User OAuth Token**.
3. On **Basic Information**, copy the **Client ID**, **Client Secret**, and **Signing Secret**.
   Under **App-Level Tokens**, generate a token with the `authorizations:read` scope; watches need it, and everything else works without it.
4. Store the credentials in the two credential services below, through the dashboard credentials page or a [credential seed](../oauth-framework.md#credential-seeds).
5. Set the [`slack` section](#slack-configuration) of `config.yaml` so Slack people map to MindRoom accounts.
6. If MindRoom sits behind an SSO proxy, let `POST /api/slack/events` through without SSO.
   Slack signs every request and MindRoom rejects unsigned ones.
7. In the app's **Event Subscriptions**, retry the request URL's verification.
   MindRoom answers Slack's verification only once step 4 is done, so the first check fails.

| Credential service | Fields |
| --- | --- |
| `slack_oauth_client` | `client_id`, `client_secret` |
| `slack_app` | `bot_token`, `signing_secret`, and `app_token` (the app-level token) |

```json
[
  {
    "service": "slack_oauth_client",
    "credentials": {
      "client_id": {"env": "SLACK_CLIENT_ID"},
      "client_secret": {"env": "SLACK_CLIENT_SECRET"}
    }
  },
  {
    "service": "slack_app",
    "credentials": {
      "bot_token": {"env": "SLACK_BOT_TOKEN"},
      "signing_secret": {"env": "SLACK_SIGNING_SECRET"},
      "app_token": {"env": "SLACK_APP_TOKEN"}
    }
  }
]
```

A seed with an empty field is skipped, so leave `app_token` out of the seed when you have no app-level token.
The Slack app's credentials stay in the primary runtime and are never given to workers.

## Slack Configuration

```yaml
slack:
  email_domain: example.com
  matrix_user_id_template: "@{localpart}:example.org"
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `email_domain` | string | unset | Email domain of the workspace members who may wake project agents, such as `example.com`. Set it together with `matrix_user_id_template`. |
| `matrix_user_id_template` | string | unset | Matrix user ID that a member's email address maps to, with exactly one `{localpart}`: with the values above, `dana@example.com` becomes `@dana:example.org`. Set it together with `email_domain`. |

Only project agents that answer mentions need this section; personal agents and watches do not.

The `slack` entry of an agent takes these options:

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `channels` | list of channel IDs | unset | Makes the agent a [project agent](#project-agents) with these channels, such as `C0123ABCD`; leave it out for a personal agent. A channel's ID is at the bottom of its details in Slack. Set it on the agent's own entry, not in `defaults.tools`. |
| `room` | string | unset | One of the agent's `rooms`, where Slack mentions of the app in its channels start threads. Needs `channels` and the `slack` section above. |
| `write` | boolean | `true` | When `false`, the agent has no `slack_send` or `slack_manage`. An agent with `room` needs `true`, because it posts its replies to Slack. |

An agent that another agent [creates](../personal-agents.md#created-agents-and-self-updates) keeps its creator's `channels` but not its `room`, because it has no rooms and never answers mentions.

## Personal Agents

```yaml
agents:
  mind:
    private:
      per: user
    tools:
      - slack
```

The first time a person asks for something in Slack, the agent replies with a link to connect Slack.
After the person approves in Slack, the agent searches, reads, and posts with that person's own account, so it finds only what they can see and posts as them.
A person can also connect or disconnect on the [Connections portal](../connections-portal.md); when Slack revokes a token, the agent replies with a new link.

Downloads are saved in the agent's workspace.
On an agent that is not `private`, every requester of the agent shares that workspace and can read what another person downloaded, so use a `private` agent for personal Slack.

## Project Agents

```yaml
agents:
  launch:
    rooms: [launch]
    tools:
      - slack:
          channels: [C0123ABCD, C0456EFGH]
          room: launch
```

Invite the MindRoom app to each listed channel, for example with `/invite @MindRoom`.
The agent reads and posts only in listed channels that the app has joined, never in direct messages, even when one is listed.
Without `room`, the agent works in those channels only when someone asks it in Matrix.

With `room`, the agent answers Slack:

1. A person mentions the app in a listed channel, and a 👀 reaction appears on their message.
2. A thread opens in `room`, showing the Slack message with its author's name and a link, marked as from Slack.
3. The agent answers as that person's MindRoom account, so the agent's access rules and the person's own connected accounts apply.
4. The agent's final reply is posted in the Slack thread by the app, cut with "…" at Slack's length limit.
   The agent does not need `slack_send(action="post", ...)` to answer.
5. Replies in that Slack thread need no mention for 7 days after the last message; they continue the same Matrix thread, as whoever wrote them.
   After that, mention the app again.

Who can wake the agent:

- The author's Slack email must be confirmed and in `slack.email_domain`, and its localpart maps to a Matrix account through `slack.matrix_user_id_template`.
- Guests, bots and other apps, deactivated accounts, and people from other workspaces never wake an agent.
- The mapped account must also pass the agent's [`access`](../authorization.md#responder-access) check in `room`.
- Anyone else sees a message in Slack that only they can see, `MindRoom can't answer you here.`, at most once a day per thread, and nothing reaches Matrix.

Only replies to Slack messages are posted to Slack; what people write in the Matrix thread stays in Matrix.
A reply can be missing in Slack when Slack is unavailable, although it exists in Matrix.
A channel has one answering agent, so two agents cannot both set `room` with the same channel, and a `personal_agents` template cannot set `room`.

## Watches

A Slack watch forwards a person's own Slack mentions, bound thread replies, and direct messages into a private Matrix room through the unified `triggers` toolkit (`source_id="slack"`).
It works with a personal agent, needs the app-level token, and needs the person's connected Slack account.
Existing `<control_state>/slack/watches.json` watches and live thread bindings are migrated automatically on startup and archived as `watches.json.migrated`.

In a private room, ask the agent, for example "Tell me when someone messages or mentions me in Slack", and it calls `triggers_write(action="subscribe_event", source_id="slack", ...)`:

- `kind="message"` (mentions, bound channel thread replies, and direct/group direct messages), `kind="mention"` (mentions and bound channel thread replies, including mentions inside direct messages), or `kind="direct_message"` (direct messages and group direct messages).
- `match={"channel_id": "C0123ABCD"}` or `match={"channel_id": ["C0123ABCD", "C0456EFGH"]}` (optional) limits channel messages and bound thread replies to those channels; direct messages are never limited by it, and `channel_id` is rejected for `kind="direct_message"`.
- Defaults for `source_id="slack"` are persistent (`once=False`), per-conversation threads (`new_thread=True`, `thread_key_from_event=True`), `reopen_on_fire=True` so resolved Matrix threads reopen automatically when a new Slack reply arrives, and `max_fires_per_hour=60` (capped by `external_trigger_policy.max_fires_per_hour`).
- Because Slack watches are reactive triggers, they support optional LLM triage (`triage_instructions` / `triage_model`), dry-run testing (`triggers_read(action="test")`), firing history (`triggers_read(action="history")`, private rooms only), and management in the Room Triggers UI (hidden from non-owners).

Each delivery opens or continues a thread in the room with the untrusted Slack event payload (text, author, channel label for channel messages, and permalink).
Messages in the same direct message conversation join one thread, and replies in a Slack channel thread join that thread, with a sliding 7-day window from the latest message.
The agent's turn runs as the watch's owner.

A Slack watch delivers only while all of these hold, so someone else never reads a private message:

- Only the owner and MindRoom's own accounts are in the room, and nobody is invited; a bridge or other bot counts as a person.
- The room hides earlier messages from people who join later.
  A [personal room](../personal-rooms.md) MindRoom created already does.
- The target agent still has personal Slack configured, the Slack app has an `app_token`, and the owner's Slack connection is still the Slack account bound at subscribe time.

`triggers_read(action="list")` and the Room Triggers UI report a paused Slack trigger in `paused_reason`:

| `paused_reason` | Fix |
| --- | --- |
| `Slack is not connected` | Connect Slack again from the link the agent gives. |
| `Slack account changed; recreate this subscription` | Delete and recreate the Slack trigger subscription. |
| `The MindRoom Slack app needs an app-level token for watches` | Add `app_token` to `slack_app`. |
| `the watch's agent no longer has personal Slack` | Re-enable personal `slack` on the target agent or recreate the subscription with another agent. |
| `you are not in this room` | Rejoin the room. |
| `another person is in this room` | Remove the other person or invitation, or watch from a room only you are in. |
| `people who join this room later could read earlier messages` | Change the room's history visibility so that people who join later do not see earlier messages, or use a personal room. |
| `room members could not be checked` | Try again when the homeserver answers. |

Security: Slack text comes from other people, so during a turn started by a Slack trigger (`event_source:slack`), `slack_send` and `slack_manage` refuse with `Ask me in a new message to post to Slack`, and `triggers_write` actions (`subscribe_event`, `update`, `set_enabled`, `delete`, etc.) refuse with `Ask me in a new message to change your triggers.`.
A new message from the owner in the same thread can post or change triggers normally.
Work that turn schedules or delegates still runs as the owner, so add the [approval](#approval) rules below to keep Slack writes behind a person's approval.

## MCP

`slack` works through the [MCP gateway](../deployment/mcp-gateway.md): select the agent and the `slack` tool on the Connections portal, and calls use the signed-in person's own Slack account, as in Matrix.
A project agent's channels are readable over MCP by everyone allowed to use that agent, and writable unless `write` is `false`, because its calls use the app's token.
Which agents the gateway offers is described in [Agent eligibility](../deployment/mcp-gateway.md#agent-eligibility).

## Approval

Add [`tool_approval`](../tool-approval.md) rules to make Slack writes wait for a person:

```yaml
tool_approval:
  rules:
    - match: slack_send
      action: require_approval
    - match: slack_manage
      action: require_approval
```

Timed approval grants for `slack_read`, `slack_send`, and `slack_manage` are scoped per `action` (for example, approving `slack_manage(action="react")` does not auto-approve `slack_manage(action="delete")`).

The 👀 reaction and the reply MindRoom posts to a Slack mention are not tool calls, so these rules do not gate them.

## Limits

- Slack sends at most 30,000 events per hour per workspace, and events beyond that are not delivered, so in a very busy workspace some watch deliveries and mentions can be missed until the next hour.
- If MindRoom is unreachable for more than a few minutes, Slack stops resending, and those messages never reach agents.
- Messages sent directly to the app in Slack are not answered.
- While anyone's watch takes direct messages, every direct message that a connected person sends or receives costs one Slack request to check who can see it, which counts against Slack's rate limits.
- `slack_read(action="recent")` makes one Slack request per conversation, so for a person in many conversations Slack's rate limit, or more than 1,000 conversations, ends a call early.
  The call then answers with the conversations it read, `complete: false`, and a `next_cursor`; the agent waits `retry_after` seconds, calls again with that cursor to read the next conversations, and merges the batches.

## Troubleshooting

| You see | Meaning and fix |
| --- | --- |
| `The MindRoom Slack app is not configured` | `slack_app` has no bot token or signing secret, or Slack rejected the bot token. Store valid ones. |
| `Invite the MindRoom app to <#C0123ABCD>` | The app has not joined that listed channel. Invite it. |
| `Not one of this agent's Slack channels` | The conversation is not in the agent's `channels`, or it is a direct message. |
| `This file is not in one of this agent's Slack channels` | A project agent downloads only files shared in its channels. |
| `Slack canvases and posts can't be downloaded as files; open them in Slack` | `slack_read(action="download")` takes files, not canvases or posts. |
| `Message and people search need a personal Slack connection` | A project agent searches only conversations. |
| `Slack needs the <scope> permission; reconnect Slack to grant it` | A personal connection lacks a user scope. Reconnect Slack. |
| `Slack needs the <scope> permission; add it to the MindRoom Slack app` | A project call needs a bot scope. Add it in the app and reinstall the app. |
| `Slack rate limit, retry after 30s` | Slack asked to wait; the agent can retry then. |
| `Slack did not answer; the message may have been sent, so check with slack_read(action="read") before retrying` | A post, edit, delete, or upload may have gone through; check before trying again. |
| `Slack refused the request: <code>` | Slack's own error code: for example, `channel_not_found` means the account cannot see that conversation, `not_in_channel` means it is not a member, so join it or invite the app, and `cant_update_message` or `cant_delete_message` means the account may not change that message. |
| `path '<path>' must be an existing file inside the agent workspace` | `slack_send(action="upload")` reads only workspace files under the default `file_access`; save the file in the workspace first. |
| `<path> is <size> bytes; the limit is 52428800 bytes.` | `slack_send(action="upload")` takes files of up to 50 MB. |
| `This Slack account belongs to another workspace` | The person approved with an account outside the app's workspace. |
| `The MindRoom Slack app needs an app-level token for watches` | Add `app_token` to `slack_app`. |
| `Cannot watch Slack here: another person is in this room` | A watch needs a room where only the owner is; the text after the colon names the cause, as in the table above. |
| `MindRoom couldn't receive your message; try again` | Shown in Slack when delivery to the agent failed; mention the app again. |
| Slack cannot verify the request URL | MindRoom answers 404 until `slack_app` has a bot token and signing secret, and 401 for a wrong signing secret; an SSO proxy in front of `/api/slack/events` also blocks it. |
| Slack reports `503` from the request URL | MindRoom answers 503 until it has finished starting, while it stops, and when a delivery failed, so that Slack resends the event; an API server started on its own, outside `mindroom run`, always answers 503. |

Config errors name the setting:

| Error | Fix |
| --- | --- |
| `Slack channel C0123ABCD is answered by both first and second` | Remove the channel from one agent's `channels`, or remove that agent's `room`. |
