# Agents invite new people to a room by email

Date: 2026-11-19. Status: decided by Bas, ready to build.

## Intent

Today a MindRoom agent can only invite people who already have a Matrix account.
After this work, a person asks an agent "invite alice@example.com to this room", someone with the right to invite approves, and the invitation reaches Alice by email.
Alice opens the link in MindRoom Chat, signs up or signs in the normal way, clicks Join, and is in the room.

Success means: it works in the common deployment (MindRoom on a person's own machine, often behind NAT, against a hosted homeserver, without server-admin rights and with SSO-only sign-up), nothing is sent before a person with the right to invite approves, and the room always learns which account used an invitation.

Said by Bas: the invite reaches the person by email; a room admin or an approver named in the config approves first; the feature is upstream and public.
Decided by Bas on 2026-11-19: the six decisions below.

## Approaches considered

1. **One-time claim code in an emailed Chat link (chosen).** The newcomer gets their Matrix account the normal way and presents the code to the router over Matrix. Needs no admin rights and no public MindRoom endpoint.
2. **A sign-up page hosted by MindRoom.** Rejected: it cannot be reached behind NAT, and it cannot create accounts on an SSO-only homeserver.
3. **The homeserver fork's email-bound registration invites.** Rejected: they need a server-admin token (`RumaAdmin`) and are read only on sign-in through an upstream identity provider.
4. **Matrix third-party (3PID) invites.** Rejected: the homeserver refuses them ("Third party identifiers are not implemented").
5. **The existing `email` tool.** Not reused: it is Agno's per-agent Gmail sender with a body the model writes. This feature needs operator-level SMTP and a fixed text.

## Decisions

### 1. Single-use bearer link, claimed over a plain to-device event

The mail holds a Chat link: `{chat_url}/invite/#r=<router Matrix ID>&c=<code>`, both values URL-encoded.
The code is in the fragment, so no web server or proxy logs it.
The newcomer signs up or signs in on any homeserver that federates with the deployment's homeserver.
Chat sends the code to the router as an unencrypted to-device event.
The router looks the code up by its SHA-256, invites the sending account to the room the request was made in, and posts a notice in the room naming that account.
The code works once, expires (default 7 days), and can be revoked.

Why unencrypted: the code already travels in clear text by mail, the sender's homeserver can act as its user anyway, and the router's homeserver can forge membership for its own users.
Encryption would protect the code from no one who cannot already misuse it.
The claim needs only the sender's Matrix ID, which the homeserver stamps on every to-device event.
An Olm-encrypted claim would not even arrive: nio marks a to-device event as authenticated only when the sender's device is already in the router's device store, and a new account shares no room with the router.

### 2. Who approves

Only the turn's requester can answer an approval card, and this work does not change the approval core.

- **The asker may invite.** They approve their own card and the invitation is sent. "May invite" means: joined to the room with a power level at or above the room's `invite` level (default 0), room creators counted in room versions with privileged creators; or listed in `email_invites.approvers`.
- **The asker may not invite.** They still answer their own card, which confirms that they made the request. The request is then stored as *waiting* and nothing is sent. A person who may invite says "approve the invite for alice@example.com" to an agent in the same room. That is their own turn, so they get their own card. On approval the waiting request is sent in their name.

Approving a waiting request uses the same tool function as a new invitation, `invite_by_email(email)`.
Called by a person who may invite, it sends the open waiting request for that address in that room, or creates and sends a new one.
One function keeps the card identical in both cases and adds no second code path.

The right is checked again when the code is claimed.
If the approver has lost it by then, the invitation is revoked.

### 3. No SMTP configured

The tool returns the link to the agent, which shows it to the approver in chat to forward by hand.
This keeps the feature usable without a mail server, which is the usual case on a person's own machine.
The cost, which the docs state: the link is then in room history and in the model's context.
Everyone who can read it there is already in the room, the code works once, and the room is told who used it.

### 4. No "address must appear in the asker's message" rule

The tool takes one argument, `email`, and the card shows it.
The tool context carries no message text, and such a rule breaks follow-ups such as "yes, invite her".

### 5. Kept from the plan

- **No auto-approval ever approves an invite.** `!auto_approve` also approves tools that ask for their own confirmation. The approval core therefore has one rule (`CARD_ONLY_TOOL_NAMES` in `approval/manager.py`) that both auto-approve paths read: a window does not approve a new `invite_by_email` call, which shows its card as usual, and starting a window does not bulk-approve waiting invite cards (decided from the stored call's tool name, not the card text). The card offers no timed approval because the tool asks for its own confirmation. Each invite needs its own card answer. (Revised after review: the earlier body-time refusal is gone.)
- **SMTP from the environment only.** `MINDROOM_SMTP_URL` and `MINDROOM_SMTP_FROM`, standard-library `smtplib`. The URL holds a password, so it is added to the secret environment list.
- **Fixed plain-text mail.** No text written by the model; the tool has no message argument.
- **The requesting room only.** No other room, no space, no power above the default, no personal agent.
- **Personal rooms and onboarding rooms are refused.** A personal room removes members it does not permit. Joining an onboarding room creates a personal agent and a Matrix account.
- **Daily caps** per asker, per address and per deployment, counted from the store so a restart does not reset them.
- **List and revoke.**
- **Codes are stored hashed** in the invite store. The router's Matrix sync store still holds the claim event in plain text (see Known limits).

### 6. The two assumptions, verified by reading code

Both hold. Nothing was run.

- **Chat receives plain custom to-device events.** matrix-js-sdk 41.7.0 with Chat's patch: `RustCrypto.preprocessToDeviceMessages` pushes `{message, encryptionInfo: null}` for `ProcessedToDeviceEventType.PlainText` (`src/rust-crypto/rust-crypto.ts:1807-1813`), and `processToDeviceMessages` emits `ClientEvent.ReceivedToDeviceMessage` for every received message (`src/sync.ts:2076-2129`). `message.sender` is the field from `/sync`. Chat already listens on this event (`src/app/models/modelController.ts:180`). Not read: the Rust crate that sorts an event into `PlainText`; only its JS side was.
- **nio 1.4.2 delivers plain custom to-device events with the homeserver's sender.** `SyncResponse._get_to_device` parses each event with `ToDeviceEvent.parse_event` (`nio/responses.py:2151-2157`), which returns `UnknownToDeviceEvent.from_dict` for an unknown type (`nio/events/to_device.py:93`) with `sender = event_dict["sender"]` (`:532-538`). `_iter_decrypted_to_device` yields it (`nio/client/base_client.py:795-823`) and `_on_to_device` runs the to-device callbacks (`nio/client/async_client.py:713-717`). A durable replay parses it the same way (`nio/durable/codec.py:192`). An Olm-encrypted event from an unknown device also arrives as `UnknownToDeviceEvent`, after `_verify_olm_payload` has checked that the inner sender equals the outer one (`nio/crypto/olm_machine.py:1186-1189`, `:1353-1376`), so the receiver may accept both forms.

A fallback is designed in anyway, because the Rust side was not read: Chat treats an invitation from the router named in the link, arriving after the claim was sent, as success even when no result event arrives.

## Architecture

### Units

Backend, new package `src/mindroom/email_invites/`:

| Unit | Responsibility |
|---|---|
| `email_invites/store.py` | SQLite store: requests, codes (hashed), claims, caps, purge. |
| `email_invites/mail.py` | Address normalisation, link, fixed mail text, SMTP send. |
| `email_invites/policy.py` | Who may invite, which rooms are refused. |
| `email_invites/receiver.py` | Router-only to-device callback that handles claims. |
| `custom_tools/email_invite.py`, `tools/email_invite.py` | Toolkit `email_invite` with `invite_by_email`, `list_email_invites`, `revoke_email_invite`. |
| `config/email_invites.py` | The `email_invites` config section. |

Chat, new folder `src/app/invites/`: `inviteLink.ts` (parse the fragment), `inviteClaim.ts` (send the claim, await the outcome), `InvitePage.tsx` (the page), plus one route.

### Protocol, version 1

- **Link.** `{chat_url}/invite/#r=<router Matrix ID>&c=<code>`. The code is `secrets.token_urlsafe(24)` (192 bits).
- **Claim.** To-device, unencrypted, to the router's Matrix ID, device `*`. Type `io.mindroom.email_invite.claim`. Content exactly `{"version": 1, "request_id": <string, 1-64 characters>, "code": <string, 1-128 characters>}`. Any other shape is ignored without an answer.
- **Result.** To-device, unencrypted, to the claim's sender, device `*`. Type `io.mindroom.email_invite.result`. Content `{"version": 1, "request_id": <same>, "status": "invited" | "rejected" | "retry"}`, plus `"room_id"` when the status is `invited`.

`rejected` is the one answer for a code that is unknown, expired, revoked, or bound to another account, for an approver who lost the right, and for a claimant the room has banned.
`retry` means a temporary failure; the claim stays bound to that account.

### Data flow

1. A person asks; the agent calls `invite_by_email(email)`.
2. The approval card appears for the asker (existing `ToolGate.CONFIRM`). A room without the router is denied by the existing gate, with its hint to run `invite_router`.
3. After approval the tool body checks, in this order: the address is valid; the room is not a personal or onboarding room; the router is joined and may invite.
4. The tool looks for an open request for this room and address, and decides by whether the asker may invite:
   - May not invite, no open request: store a waiting request (caps: asker, room). Answer: stored, waiting for a person who may invite.
   - May not invite, open request exists: answer that it is already waiting, or already sent.
   - May invite, open waiting request: use it.
   - May invite, open request already sent: revoke it and create a new one, so the old link stops working.
   - May invite, no open request: create one (caps: asker, room).
5. For a person who may invite, the tool issues the code (caps: deployment, address), builds the link, and delivers it: with SMTP it sends the mail and answers "sent"; without SMTP it answers with the link to forward. A failed send revokes the request and returns the error.
6. The newcomer opens the link. Signed out, Chat's protected-route guard sends them to sign-in and brings them back, fragment included.
7. On the invite page the newcomer clicks Join. Chat sends the claim.
8. The router's receiver admits the event (shape, rate limits, human sender) and finds the request by the code's hash. A code that is unknown, revoked, expired, or bound to another account is `rejected`. A request this sender already used is answered `invited` again.
9. A request in `sending` is `rejected`, whoever claims. If the sender is already joined or already invited to the room, it answers `invited` and changes nothing.
10. Otherwise it binds the request to the sender with one conditional update, checks again that the approver may invite, that the router may invite and that the room is not refused, then moves the row `open → sending` with one conditional update (bound to this sender, not revoked, not expired, not used, not sending). Only the call that wins this update calls `room_invite`; a revoke or expiry during the reads wins over it.
11. On success it records the invitation (`sending → used`), answers `invited` with the room ID, and posts the notice "Invite for alice@example.com (approved by @bas:example.org) was used by @alice:example.org." in the thread of the request.
12. Chat joins the room, with the router's server as `via`, and opens it.

### Storage

One SQLite file, `<storage_root>/tracking/email_invites.sqlite`, directory mode 0700 and file mode 0600, as desktop pairing does.
One connection per operation.
Every write runs in a `BEGIN IMMEDIATE` transaction, and no network call happens inside one.

Table `email_invites`:

| Column | Meaning |
|---|---|
| `id` | Integer key. |
| `room_id`, `thread_id`, `email` | Where the request was made, and the normalised address. |
| `requested_by`, `agent_name` | The asker and the agent that took the request. |
| `created_at`, `expires_at` | Unix seconds. A waiting request expires `expiry_days` after it was made; issuing the code sets the expiry to `expiry_days` after that moment. |
| `inviter_id` | The person who approved. Null while waiting. |
| `code_hash` | SHA-256 hex of the code, unique. Null while waiting. |
| `sent_at` | When the code was issued. Null while waiting. |
| `revoked` | 0 or 1. |
| `claimed_by`, `claimed_at` | The account the code is bound to. |
| `invited_at` | When the router's Matrix invite succeeded. |
| `sending_at` | When the one call that may send the Matrix invite won the right to send it. |

Claim state machine, with one invariant: **a link causes at most one Matrix invite, ever.**

| State | Means | Moves to |
|---|---|---|
| `waiting` | No code yet. | `open` (code issued), `revoked`. |
| `open` | Code valid, not expired, no invite attempted. May be bound to one claimant. | `sending` (one conditional update, only the winner sends), `revoked`, expired. |
| `sending` | The Matrix invite is on its way. Never sends again; a claim finding it is `rejected`. | `used` on success, with the one success notice. `revoked` on any other outcome (`M_FORBIDDEN`, another error, a timeout), with the one "may or may not have gone out" notice. `revoked` at router start (a crash left it), with the same notice. |
| `used` | Invited. The same claimant is answered `invited`; no second invite. | — |
| `revoked` | Dead link. Does not block a new request for the address. | — |

No locks and no retries: the conditional updates decide. A request is *open* when it is not revoked, not expired, not sending, and `invited_at` is null.
At most one open request exists per room and address.
Rows stay one day past expiry, so the 24-hour counts are complete, and are purged after that whenever the store is opened for a write.

### Failure handling

| Case | Result |
|---|---|
| Router not in the room | Existing denial with the `invite_router` hint. Nothing stored. |
| Router joined but may not invite | Tool refuses. Nothing stored. |
| Auto-approve window open | The card appears as usual; the window does not approve it, and starting a window does not approve waiting invite cards. |
| A cap is reached | Tool refuses and names the cap. Nothing sent. |
| SMTP send fails | Request revoked; the error text goes back to the agent. The attempt still counts toward the caps. |
| MindRoom offline when the claim is sent | The homeserver queues the event; it is handled on the next sync. Chat shows the offline text and keeps listening. |
| Room state unreadable at claim | `retry`. The claim stays bound. |
| Matrix invite not a success (`M_FORBIDDEN`, any other error, a timeout, a connection error) | `rejected`. `sending → revoked`, so the link is dead; that one update posts exactly one notice in the thread: the invite may or may not have gone out, and the approver can send a new link if needed. A fresh `invite_by_email` for the address is not blocked. Stricter than a retry: the person may need a new link. |
| Router crashes while a row is `sending` | At the next start, before any claim is handled, every `sending` row becomes `revoked` with the same notice. A person who joined through that invite and was removed meanwhile cannot use the link again. |
| Claim repeated (page reload, durable replay) | Same account: `invited` again once the invite succeeded, with no second Matrix invite. Another account: `rejected`. Chat sends each claim once; a duplicate `request_id` gets no special handling. |
| Claimant already joined, or already invited | `invited`. The request is not changed. |
| Revoke races a claim | The conditional updates decide; a revoke before `open → sending` wins. A row in `sending` cannot be revoked; its outcome posts the notice. A revoke of a used link reports the account that used it. |
| Approver lost the right before the claim | Request revoked, `rejected`. |
| Router lost the invite right before the claim | `retry`. The claim stays bound; a room admin can restore the router's power. |
| Room became a personal or onboarding room before the claim | Request revoked, `rejected`. |
| Router account recreated | Old links name the old router ID and no longer work. Invite again. |
| Restart or config reload | State is on disk; the receiver is registered with the router's other callbacks at start. |

### Tool behaviour

Toolkit `email_invite`, added to an agent's `tools` like any other tool; it needs room context and the primary runtime.

- `invite_by_email(email)`: gated with `ToolGate.CONFIRM`. Behaviour as in the data flow.
- `list_email_invites()`: the open requests of this room, each with address, state (`waiting` or `sent`), asker, approver and expiry. Not gated.
- `revoke_email_invite(email)`: revokes the open request for that address in this room. Allowed for its asker, its approver, and anyone who may invite. Not gated. If the request was already used, it changes nothing and reports the account that used it.

No function accepts a room, a Matrix ID, a subject, or body text.

### Mail

Sent with `smtplib` in a worker thread, 20 seconds timeout, TLS certificates checked with `ssl.create_default_context()`.
`MINDROOM_SMTP_URL` is `smtps://[user:password@]host[:port]` (TLS from the start, default port 465) or `smtp://[user:password@]host[:port]` (default port 587, `STARTTLS` required unless the host is `localhost` or a loopback address).
User and password are URL-decoded.
`MINDROOM_SMTP_FROM` is the sender, for example `MindRoom <invites@example.org>`.
SMTP counts as configured only when both are set.

The mail is plain text with one recipient.
Subject, fixed: `You are invited to a MindRoom room`.
Body:

```text
{inviter_name} ({inviter_id}) invited you to the room "{room_name}" on MindRoom.

Open this link to join. You can create an account or sign in with one you have:
{link}

The link works once and expires on {expiry date, YYYY-MM-DD} (UTC).
If you did not expect this invitation, ignore this mail.
```

When the room is encrypted, one more line follows the expiry line: `This room is encrypted: you will not see messages sent before you joined.`
`inviter_name` and `room_name` have non-printable characters removed and whitespace collapsed, and are cut to 80 characters.

### Address

An address is accepted when, after trimming, it is at most 254 characters and matches `^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$`.
It is then lower-cased, and that form is what the store, the caps and the mail use.
This refuses display names, lists, line breaks and non-ASCII characters; an international domain must be given in its `xn--` form.

## Security model and known limits

What each party can do:

| Party | Can | Cannot |
|---|---|---|
| Agent or model (also when prompt-injected) | Propose an address. | Send anything without a person's card; choose room, text, or recipient account. |
| Asker without the invite right | Store a waiting request, within the caps. | Cause a mail or a link. |
| Approver | Send one fixed mail per card, within the caps. | Change the text; invite to another room. |
| Holder of the link | Join the room once, with any Matrix account. | Join twice; join after expiry or revoke; learn the room before a valid claim. |
| Other Matrix users | Send claims (rate-limited). | Guess a 192-bit code; learn from the answer whether a code ever existed. |
| Homeservers on the path | Read the code in the claim. | Gain anything they do not have already (see decision 1). |

Checks that carry this:

- The claimant is `event.sender`, never a field of the content. Agents, the router and configured bot accounts are refused as claimants.
- Codes are compared by SHA-256 lookup, stored in the invite store only as hashes, and never logged. With SMTP configured, the code never reaches the model.
- Claims are bounded before any work: at most 10 per sender per minute and 60 in total per minute, with at most 1024 senders tracked.
- Binding is one conditional update (`claimed_by IS NULL OR claimed_by = sender`, not revoked, not expired), so two accounts cannot both win and a failed invite never frees the code for someone else.
- Rights are read from live room state at request time and again at claim time.
- A link causes at most one Matrix invite, ever (`open → sending` has one winner; `sending` is never reopened, and a crash leaves it revoked), so a person removed from the room cannot use the link to return.
- Mail headers are built with `email.message.EmailMessage`, which refuses line breaks.
- Caps: 5 requests per asker per 24 hours; 20 open requests per room; 3 sends per address per 24 hours; 50 sends per deployment per 24 hours. Without them, anyone who is admin of a room of their own could use the operator's mail server as a relay.

Known limits:

- **The link is a bearer token.** Whoever holds the mail can join, and in an unencrypted room with shared history can read it at once. MindRoom cannot compare the claimant's mail address without server-admin rights. The room notice gives accountability after the fact, not prevention. Phase 2 lists the hardening.
- **Without SMTP** the link is in room history and in the model's context.
- **The asker's card appears before the right is checked.** The gate runs before the tool body, so an asker who may not invite approves a card and is then told the request is waiting.
- **Plaintext code in the router's sync store.** nio's durable sync store keeps the claim event, code included, on the router's own disk. Accepted: the code is single-use and bound to the first claimant. (An earlier per-claim WAL checkpoint did not achieve hash-only storage and was removed.)
- **A claim lost in a crash right after the sync ack gets no answer.** The person clicks Join again.
- **Revoking a link that is `sending`** fails; the tool reports no open invite, and the outcome's notice follows.
- **nio's own transport retry** may repeat the one `room_invite` HTTP request inside that single call after a timeout. This is not a second application-level invite; a removal between those repeats within seconds is not covered.
- **A crash after recording `used` but before the notice** loses the notice; the invite stays recorded.
- **An unknown Matrix-invite outcome costs a new link.** The link is revoked even if the invite went out; the thread notice says so.
- **Store I/O is only partly offloaded.** The personal-room scan runs in a worker thread; the invite store's short SQLite transactions (2-second busy timeout) still run on the event loop.
- **Encrypted rooms:** the newcomer cannot read messages from before they joined.
- **Homeservers that need a registration token:** a newcomer cannot sign up there. They need an account on another federating homeserver, or Phase 2.
- **Waiting requests fill the room's cap.** Members who may not invite can fill the 20 open requests of a room, five each per day. A person who may invite clears them with `revoke_email_invite`.
- **Federation off, or a room that does not federate:** a claimant from another homeserver gets `retry` or `rejected`.
- **Whether agents answer the newcomer** follows the existing access rules. In a room that is not one of an agent's managed rooms the agent may stay silent for them; the docs page says so.
- **The invite page can be used as a lure.** A crafted link can make a signed-in person send their Matrix ID to a foreign router after a click. The page names the router and joins only a room that this router invited the person to.
- **Chat installs with hash routing** (`hashRouter.enabled`) are not served by the link, which uses the path `/invite/` and the fragment for its values. The default Chat configuration uses path routing.
- **Native apps:** the link opens web Chat. App links for iOS and Android are not part of this work.

## Chat landing page

Route `/invite/`, inside the protected routes, with its own route entry so it does not open as a space named "invite".
The page reads `r` and `c` from the fragment once, keeps them only in memory, and removes the fragment from the address bar with `history.replaceState`. The code is never written to session or local storage, and the saved sign-in path does not include it: a signed-out newcomer signs up (or signs in) and then opens the mail link again.
It sends nothing until the Join button is clicked, and it shows only the framed state when it runs inside a frame.

| State | Shown | Leaves when |
|---|---|---|
| `invalid` | "This invite link is incomplete. Open the link from the mail again." | — |
| `framed` | "Open this page in its own tab to use the invitation." | — |
| `ready` | "Join a room through `<router ID>` as `<my ID>`", Join button. | Click → `claiming`. |
| `claiming` | Progress. The claim is sent exactly once (no resend). | Result or router invite → `joining`; `rejected` → `rejected`; `retry` → `retry`; 20 seconds without an answer → `offline`. |
| `offline` | "This MindRoom is not answering. Your request is saved; the invitation will appear in your room list when it is back." | A later result or router invite → `joining` or `rejected`. |
| `rejected` | "This invitation is no longer valid. Ask for a new one." | — |
| `retry` | "That did not work. Try again in a moment.", Try again button. | Click → `claiming`. |
| `joining` | Progress. | Joined → the room opens; failure → `joinFailed`. |
| `joinFailed` | The join error, Try again button. | Click → `joining`. |

Rules:

- A result counts only when its sender is the router ID from the link and its `request_id` is the one sent.
- A router invite counts only when the membership event's sender is that router ID and the invitation was not already pending when the claim was sent. Chat notes its pending invitations just before it sends the claim.
- After `invited`, Chat joins only when it holds a pending invitation to that room whose sender is that router. If it is already joined, it opens the room. If it holds an invitation from someone else, it opens the room's normal invitation view instead. If it holds none, it waits up to 20 seconds for one to arrive through sync and then shows `rejected`; this is what a person sees who used the link, was removed from the room, and opens the link again.
- The join passes the server name of the router ID as `via`.

No change to the approval card: it shows the tool name and the `email` argument, in the room the invitation is for.

## Config schema

```yaml
email_invites:
  chat_url: https://chat.mindroom.chat   # origin of the Chat web app the link opens; default shown
  expiry_days: 7                         # 1 to 30
  approvers: []                          # Matrix user IDs who may approve in any room
```

`chat_url` must be an `https` origin, or an `http` origin on a local address, with no path.
Environment: `MINDROOM_SMTP_URL` (secret) and `MINDROOM_SMTP_FROM`.
The feature is on for an agent when `email_invite` is in its `tools`.

## Docs

A new page `docs/email-invites.md`: set-up, the approval rule, what happens without SMTP and its cost, the bearer-link limit, encrypted rooms, access rules for the newcomer, the caps.
A row for the tool in `docs/tools/`, a sentence in `docs/tool-approval.md` under "Auto-Approve Everything" on the refusal, and the config section in the configuration reference.

## Phase 2 (not part of this work)

1. **The hosted provisioning service sends the mail** for paired local installs, so the common deployment needs no mail server. It needs SMTP and abuse limits on the service.
2. **Second-mail binding as optional hardening.** After the claim, a short code goes to the original address and the newcomer types it into Chat before the router invites. This proves control of the mailbox by the account that joins, at the cost of a second mail.
3. **Single-use registration token** for homeservers that require one, when the operator configures a server-admin token.
4. **A richer approval card and a banner on the sign-in page** that say what the visitor was invited to.

## Not verified

- Nothing was run: no SMTP, no SSO sign-up, no federation, no encrypted room. The live-test task in the plan covers these.
- The Rust crypto crate's sorting of an unencrypted custom event into `PlainText` (decision 6).
- Whether signing in with SSO on the sign-in page creates a new account on first use on the hosted homeserver, or the newcomer must use Register. It decides one sentence of the mail.
- That the hosted homeserver delivers to-device events between accounts that share no room. The fork's send path has no such condition (`src/api/client/to_device.rs`), but the deployed configuration was not seen.
