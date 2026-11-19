---
icon: lucide/mail-plus
---

# Email Invites

With the `email_invite` tool, a person can ask an agent "invite alice@example.com to this room".
After someone who may invite approves, the address gets a link that works once.
The newcomer opens it in MindRoom Chat, signs up or signs in the normal way on any homeserver that federates with yours, clicks Join, and is invited to the room by the router.
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
```

To send mail, set two environment variables:

| Variable | Meaning |
|---|---|
| `MINDROOM_SMTP_URL` | `smtps://[user:password@]host[:port]` (TLS from the start, default port 465) or `smtp://[user:password@]host[:port]` (default port 587, `STARTTLS` required unless the host is `localhost` or a loopback address); user and password are URL-encoded, and the variable is kept from workers like other secrets |
| `MINDROOM_SMTP_FROM` | The sender, for example `MindRoom <invites@example.org>` |

The router must be in the room and allowed to invite there; without the router, the tool call is denied with the hint to run [`invite_router`](tool-approval.md#when-the-router-is-missing).
Personal rooms and onboarding rooms are refused.

## Who Approves

`invite_by_email(email)` always shows an approval card to the person who asked.
A person *may invite* when they are joined to the room with a power level at or above its `invite` level (0 unless the room raised it), or when they are listed in `email_invites.approvers`.

- If the asker may invite, approving the card sends the invitation.
- If not, approving the card only confirms the request. It is stored as *waiting* and nothing is sent. Someone who may invite then says "approve the invite for alice@example.com" to an agent in the same room and approves their own card; the waiting request is sent in their name.

Asking again for an address that was already sent replaces the old link, which stops working.
While an [auto-approve window](tool-approval.md#auto-approve-everything) is open in the conversation, the tool refuses and asks for `!auto_approve off`, so an invitation never goes out without a card.

`list_email_invites()` shows the room's open invites, and `revoke_email_invite(email)` revokes one; the asker, the approver, or anyone who may invite can revoke.

## Without a Mail Server

When either variable is missing, nothing is mailed. The tool returns the link and the agent shows it to the approver, who forwards it by hand.
The link is then in the room's history and in the model's context.
Everyone who can read it there is already in the room, the link works once, and the room is told who used it.

## What the Link Allows

The link is a bearer token: whoever holds it can join the room once, with any Matrix account.
MindRoom cannot check that the account belongs to the mailed address, so when someone uses a link, the router posts "Invite for alice@example.com (approved by @bas:example.org) was used by @alice:example.org." in the thread where the invite was asked for.
A link stops working once used, when it expires, or when it is revoked.
The approver's right is checked again when the link is used; if they lost it, the invite is revoked.
A person removed from the room cannot use the same link to come back.

In an encrypted room, the newcomer does not see messages sent before they joined; the mail says so.
Whether agents answer the newcomer follows the normal [access rules](authorization.md): in a room that is not one of an agent's managed rooms, the agent may stay silent for them.
A homeserver that needs a registration token cannot be used to sign up; the newcomer needs an account on another homeserver that federates with yours.

## Limits

| Limit | Value |
|---|---|
| Requests per asker | 5 per 24 hours |
| Open requests per room | 20 |
| Mails per address | 3 per 24 hours, failed sends included |
| Mails per deployment | 50 per 24 hours |

The counts are kept on disk, so a restart does not reset them.
Members who may not invite can fill a room's 20 open requests; someone who may invite clears them with `revoke_email_invite`.
