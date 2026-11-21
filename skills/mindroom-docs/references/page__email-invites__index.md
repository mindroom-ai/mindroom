# Email Invites

With the `email_invite` tool, a person can ask an agent "invite alice@example.com to this room".
When the person asking may invite, or after someone who may invite confirms a waiting request, the address gets a link that works once.
The newcomer opens it in MindRoom Chat, signs up or signs in the normal way on any homeserver that federates with yours, clicks Join, and is invited to the room by the agent that sent the link.
No server-admin rights and no public MindRoom endpoint are needed, so this also works for MindRoom on your own machine behind NAT.

## Set-Up

Add the tool to the agents that should take invite requests:

```yaml
agents:
  host:
    tools:
      - email_invite

email_invites:
  chat_url: https://chat.mindroom.chat   # origin of the Chat web app the link opens; default shown
  expiry_days: 7                         # 1 to 30
  approvers: []                          # Matrix user IDs who may approve in any room

tool_approval:
  rules:
    - match: invite_by_email
      action: require_approval             # recommended: an invitation needs approval, like any gated tool
```

`invite_by_email` is an ordinary tool for [tool approval](https://docs.mindroom.chat/tool-approval/): whether a call asks first is up to your config, and the rule above is recommended.

To send mail, set two environment variables:

| Variable | Meaning |
|---|---|
| `MINDROOM_SMTP_URL` | `smtps://[user:password@]host[:port]` (TLS from the start, default port 465) or `smtp://[user:password@]host[:port]` (default port 587, `STARTTLS` required unless the host is `localhost` or a loopback address); any other scheme sends nothing; user and password are URL-encoded, and the variable is kept from workers like other secrets |
| `MINDROOM_SMTP_FROM` | The sender, for example `MindRoom <invites@example.org>` |

The agent (or team) that takes the request sends the Matrix invite from its own account, so it must be allowed to invite in the room; otherwise the call answers "I need moderator rights in this room to invite people."
Personal rooms, onboarding rooms and spaces are refused.

## Who Approves

A person *may invite* when they are joined to the room with a power level at or above its `invite` level (0 unless the room raised it), or when they are listed in `email_invites.approvers`.

- If the asker may invite, the call sends the invitation (after its approval card, with the rule above).
- If not, the call only stores the request as *waiting* and nothing is sent. Someone who may invite then says "approve the invite for alice@example.com" to an agent in the same room; their call sends the waiting request in their name.

Asking again for an address that was already sent replaces the old link, which stops working.

`list_email_invites()` shows the room's open invites, and `revoke_email_invite(email)` revokes one; the asker, the approver, or anyone who may invite can revoke.

## Without a Mail Server

When either variable is missing, nothing is mailed. The tool returns the link and the agent shows it to the approver, who forwards it by hand.
The link is then in the room's history and in the model's context.
Everyone who can read it there is already in the room, the link works once, and the room is told who used it.

## What the Link Allows

The link is a bearer token: whoever holds it can join the room once, with any Matrix account.
MindRoom cannot check that the account belongs to the mailed address, so when someone uses a link, the agent that sent it posts "Invite for alice@example.com (approved by @bas:example.org) was used by @alice:example.org." in the thread where the invite was asked for.
A link stops working once used, when it expires, or when it is revoked. A link causes at most one Matrix invite: once the agent starts sending it, the link cannot be revoked and never sends again.
The approver's right is checked again when the link is used; if they lost it, the invite is revoked.
A person removed from the room cannot use the same link to come back.
When the Matrix invite does not clearly succeed (a refusal, a timeout or an unexpected error), or the agent stopped while sending it, the link stops working and the thread is told once that the invite may or may not have gone out; the approver can send a new link, which is not blocked by the old one.
When the agent that stopped while sending was since removed from the configuration, an agent or the owner account that is in the room and may post there does this for it at its next start.

In an encrypted room, the newcomer does not see messages sent before they joined; the mail says so.
Whether agents answer the newcomer follows the normal [access rules](https://docs.mindroom.chat/authorization/): in a room that is not one of an agent's managed rooms, the agent may stay silent for them.
A homeserver that needs a registration token cannot be used to sign up; the newcomer needs an account on another homeserver that federates with yours.

## Limits

| Limit | Value |
|---|---|
| Requests per asker | 5 per 24 hours |
| Open requests per room | 20 |
| Mails per address | 3 per 24 hours, failed sends included |
| Mails per deployment | 50 per 24 hours |

The counts are kept on disk, so a restart does not reset them.

Known limits:

- Without a `tool_approval` rule that requires approval for `invite_by_email` (and with `tool_approval.default: auto_approve`), an agent can send invite mail without a card, within the caps; the asker must still have the invite right. Auto-approval (timed card approval, `!auto_approve`) covers it like any tool.

- The Matrix sync store of the agent that sent the link keeps the claim event, code included, in plain text on the agent's own disk. The code works once and is bound to the first account that uses it.
- A claim that arrives just before the agent crashes, or while it is not running, may get no answer; the person clicks Join again.
- A revoke that lands while a claim's Matrix invite is already on its way does not stop that invite; the thread is then told the outcome.
Members who may not invite can fill a room's 20 open requests; someone who may invite clears them with `revoke_email_invite`.
