---
icon: fontawesome/brands/slack
---

# Slack

The `slack` tool lets agents search, read, download from, post to, and react in Slack through one Slack app that your organization creates for MindRoom.
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

| Function | Does | Personal | Project |
| --- | --- | --- | --- |
| `slack_search` | Finds messages, conversations, or people and returns the IDs the other functions take | `messages` (Slack search syntax, such as `launch in:#general from:@dana after:2026-10-01`), `conversations`, and `people` | `conversations` only, among the agent's channels |
| `slack_read` | Reads a conversation's latest messages, or one thread | any conversation the person can see | the agent's channels |
| `slack_download` | Saves a file of up to 50 MB in the agent workspace's `slack-downloads/` folder | any file the person can see | files shared in the agent's channels |
| `slack_post` | Posts a message, or a reply in a thread | as the person; a user ID opens a direct message | as the app, in the agent's channels |
| `slack_react` | Adds an emoji reaction to a message | as the person | as the app, in the agent's channels |
| `slack_watch` | Starts, stops, or checks a [watch](#watches) | in a Matrix conversation | not offered |

Functions take a conversation ID such as `C0123ABCD`, `<conversation>:<ts>` such as `C0123ABCD:1714000000.000100`, or a message link, never a channel name.
Agents cannot edit or delete messages or upload files.
[Background scripts](background-scripts.md) cannot call `slack`.

## Set Up The Slack App

An administrator creates one Slack app for the MindRoom installation.
One MindRoom serves one Slack workspace; connections and events from other workspaces are ignored, and Enterprise Grid org-wide installs are not supported.
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
| `write` | boolean | `true` | When `false`, the agent has no `slack_post` and `slack_react`. Replies to Slack mentions are still posted. |

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
Only accounts from the app's workspace can connect.

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
   The agent does not need `slack_post` to answer.
5. Replies in that Slack thread need no mention for 7 days after the last message; they continue the same Matrix thread, as whoever wrote them.
   After that, mention the app again.

Who can wake the agent:

- The author's Slack email must be in `slack.email_domain`, and its localpart maps to a Matrix account through `slack.matrix_user_id_template`.
- Guests, bots and other apps, deactivated accounts, and people from other workspaces never wake an agent.
- The mapped account must also pass the agent's [`access`](../authorization.md#responder-access) check in `room`; with the default `access`, that means being joined to one of the agent's rooms.
- Anyone else sees a message in Slack that only they can see, `MindRoom can't answer you here.`, at most once a day per thread, and nothing reaches Matrix.

Only replies to Slack messages are posted to Slack; what people write in the Matrix thread stays in Matrix.
A reply can be missing in Slack when Slack is unavailable, although it exists in Matrix.
A channel has one answering agent, so two agents cannot both set `room` with the same channel.

## Watches

A watch forwards a person's own Slack mentions and direct messages into the Matrix room where they start it.
It works with a personal agent, needs the app-level token, and needs the person's connected Slack account.

In the room, ask the agent, for example "Tell me when someone messages or mentions me in Slack", and it calls `slack_watch`:

- `action="start"` creates the watch, or replaces the one the person has with this agent in this room.
- `action="stop"` ends it, and `action="status"` shows its filters and, when it is not delivering, why.
- `mentions` (default `true`) delivers messages that mention the person, including in group direct messages.
- `direct_messages` (default `true`) delivers every message in the person's direct messages and group direct messages.
- `channels` (optional) limits channel messages to those channel IDs; direct messages are not limited by it.
  Replies in a thread where the person was mentioned also arrive, within those channels.

Each delivery opens a thread in the room, such as "Slack direct message for you from Dana", with the Slack text, its author, its channel, and a link.
Messages in the same direct message conversation join one thread, and replies in a Slack channel thread join that thread, until 7 days pass without a message.
The agent can then summarize it or draft a reply; its turn runs as the watch's owner.
A watch lasts until it is stopped.

A watch delivers only while all of these hold, so someone else never reads a private message:

- Only the owner and MindRoom's own accounts are in the room, and nobody is invited; a bridge or other bot counts as a person.
- The room hides earlier messages from people who join later.
  A [personal room](../personal-rooms.md) MindRoom created already does.
- The owner's Slack connection works and is still the Slack account the watch started with.

`slack_watch(action="status")` reports a paused watch with one of these texts:

| Status | Fix |
| --- | --- |
| `paused: Slack is not connected` | Connect Slack again from the link the agent gives. |
| `paused: Slack account changed; start the watch again` | Start the watch again. |
| `paused: you are not in this room` | Rejoin the room. |
| `paused: another person is in this room` | Remove the other person or invitation, or watch from a room only you are in. |
| `paused: people who join this room later could read earlier messages` | Change the room's history visibility so that people who join later do not see earlier messages, or use a personal room. |
| `paused: room members could not be checked` | Try again when the homeserver answers. |

Security: Slack text comes from other people, so during a turn that a watch started, `slack_post` and `slack_react` refuse with `Ask me in a new message to post to Slack`, and so do `slack_watch` start and stop.
A new message from the owner in the same thread can post.
Work that turn schedules or delegates still runs as the owner, so add a [`tool_approval`](../tool-approval.md) rule for `slack_post` and `slack_react` to keep Slack writes behind a person's approval.

## MCP

`slack` works through the [MCP gateway](../deployment/mcp-gateway.md): select the agent and the `slack` tool on the Connections portal, and calls use the signed-in person's own Slack account, as in Matrix.
`slack_watch` does not work there.
A project agent's channels are readable over MCP by everyone allowed to use that agent, and writable unless `write` is `false`, because its calls use the app's token.
Which agents the gateway offers is described in [Agent eligibility](../deployment/mcp-gateway.md#agent-eligibility).

## Approval

Add [`tool_approval`](../tool-approval.md) rules to make Slack writes wait for a person:

```yaml
tool_approval:
  rules:
    - match: slack_post
      action: require_approval
    - match: slack_react
      action: require_approval
```

The 👀 reaction and the reply MindRoom posts to a Slack mention are not tool calls, so these rules do not gate them.

## Limits

- Slack sends at most 30,000 events per hour per workspace, and events beyond that are not delivered, so in a very busy workspace some watch deliveries and mentions can be missed until the next hour.
- If MindRoom is unreachable for more than a few minutes, Slack stops resending, and those messages never reach agents.
- Messages sent directly to the app in Slack are not answered.

## Troubleshooting

| You see | Meaning and fix |
| --- | --- |
| `The MindRoom Slack app is not configured` | `slack_app` has no bot token or signing secret, or Slack rejected the bot token. Store valid ones. |
| `Invite the MindRoom app to <#C0123ABCD>` | The app has not joined that listed channel. Invite it. |
| `Not one of this agent's Slack channels` | The conversation is not in the agent's `channels`, or it is a direct message. |
| `This file is not in one of this agent's Slack channels` | A project agent downloads only files shared in its channels. |
| `Message and people search need a personal Slack connection` | A project agent searches only conversations. |
| `Slack needs the <scope> permission; reconnect Slack to grant it` | A personal connection lacks a user scope. Reconnect Slack. |
| `Slack needs the <scope> permission; add it to the MindRoom Slack app` | A project call needs a bot scope. Add it in the app and reinstall the app. |
| `Slack rate limit, retry after 30s` | Slack asked to wait; the agent can retry then. |
| `Slack did not answer; the message may have been sent, so check with slack_read before retrying` | A post may have gone through; check before posting again. |
| `This Slack account belongs to another workspace` | The person approved with an account outside the app's workspace. |
| `The MindRoom Slack app needs an app-level token for watches` | Add `app_token` to `slack_app`. |
| `Cannot watch Slack here: another person is in this room` | A watch needs a room where only the owner is; the text after the colon names the cause, as in the table above. |
| `Watch mentions, direct messages, or both` | `start` needs `mentions` or `direct_messages` set to `true`. |
| `Slack watches work only in a Matrix conversation` | Start the watch from a Matrix room, not from MCP or a script. |
| `MindRoom couldn't receive your message; try again` | Shown in Slack when delivery to the agent failed; mention the app again. |
| Slack cannot verify the request URL | MindRoom answers 404 until `slack_app` has a bot token and signing secret, and 401 for a wrong signing secret; an SSO proxy in front of `/api/slack/events` also blocks it. |

Config errors name the setting:

| Error | Fix |
| --- | --- |
| `Set email_domain and matrix_user_id_template together` | Set both fields of `slack`, or neither. |
| `Answering Slack mentions needs slack.email_domain and slack.matrix_user_id_template` | Add the `slack` section before setting `room`. |
| `agents.<name>.tools.slack.room needs channels` | `room` is only for project agents. |
| `agents.<name>.tools.slack.room must be one of the agent's rooms` | Add the room to the agent's `rooms`. |
| `agents.<name>.tools.slack.channels: Slack channel IDs look like C0123ABCD` | Use channel IDs, not names. |
| `Slack channel C0123ABCD is answered by both first and second` | Remove the channel from one agent's `room` setup. |
