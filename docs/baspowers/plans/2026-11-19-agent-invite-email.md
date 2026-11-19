# Agent Invite By Email Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** baspowers:subagent-driven-development — nine tasks across two repositories that share one wire protocol and one store interface, in code that sends mail and admits people to rooms, so each task gets a fresh review.

**Goal:** A person asks an agent to invite an email address to the room; after approval by someone who may invite, the address gets a single-use link, and the newcomer joins through MindRoom Chat with a normal Matrix account.

**Architecture:** A SQLite store holds requests and hashed codes. A gated toolkit creates, sends, lists and revokes them, mailing a fixed text over SMTP or returning the link. A router-only to-device receiver binds a claimed code to the sending account and invites it. Chat gets one protected page that sends the claim and joins the room.

**Tech Stack:** Python 3.12, pydantic v2, `sqlite3`, `smtplib`, `mindroom-nio` 1.4.2, pytest. Chat: TypeScript, React, matrix-js-sdk 41.7.0, vitest.

**Spec:** `docs/baspowers/specs/2026-11-19-agent-invite-email-design.md`

## Global Constraints

- Event types: `io.mindroom.email_invite.claim`, `io.mindroom.email_invite.result`. Protocol `version` is the integer `1`.
- Link: `{chat_url}/invite/#r=<router ID>&c=<code>`, both values URL-encoded. Code: `secrets.token_urlsafe(24)`, stored only as SHA-256 hex, never logged.
- Caps: 5 requests per asker per 24 h; 20 open requests per room; 3 sends per address per 24 h; 50 sends per deployment per 24 h.
- Claim limits: 10 per sender per minute, 60 in total per minute, at most 1024 senders tracked.
- Environment: `MINDROOM_SMTP_URL` (secret), `MINDROOM_SMTP_FROM`. No new dependency in either repository.
- Mail subject, exactly: `You are invited to a MindRoom room`. Mail body: the template in the spec, verbatim.
- No change under `src/mindroom/approval/`. No tool function takes a room, a Matrix ID, a subject, or body text.
- The claimant is always the event's `sender`, never a content field.
- Backend tests: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/<file>.py -x -n 0 --no-cov'`; run each new test file once with the default options too.
- Chat tests: `just test src/app/invites`; `just check` before the pull request.
- Tests and screenshots use invented data only (`alice@example.com`, `@bas:example.org`).
- Commits end with the implementer's own `Assisted-by` trailer, as their brief says.

## Review Focus

1. Two accounts present the same code at the same moment: exactly one is bound, the other gets `rejected`. → Task 1.
2. An address that carries a line break or a second address: refused before anything is stored or sent. → Task 2.
3. MindRoom stops between binding a claim and the Matrix invite: the same account's next claim invites it; another account still gets `rejected`. → Task 5.
4. The approver lost the invite right between sending and claim: the request is revoked and the answer is `rejected`. → Task 5.
5. A person who used the link and was removed from the room opens it again: no second Matrix invite. → Task 5.
6. A newcomer takes longer than 15 minutes to sign up and opens the mail link again: the page still works, because nothing is sent before the click. → Tasks 8 and 9.

## File Structure

Backend (pull request 1, repository `mindroom`):

| File | Responsibility |
|---|---|
| `src/mindroom/email_invites/__init__.py` | Empty package marker. |
| `src/mindroom/email_invites/store.py` | SQLite store and caps. |
| `src/mindroom/email_invites/mail.py` | Address, link, mail text, SMTP. |
| `src/mindroom/email_invites/policy.py` | Who may invite; refused rooms. |
| `src/mindroom/email_invites/receiver.py` | Router's claim receiver. |
| `src/mindroom/config/email_invites.py` | `EmailInvitesConfig`. |
| `src/mindroom/custom_tools/email_invite.py`, `src/mindroom/tools/email_invite.py` | Toolkit and its registration. |

Chat (pull request 2, repository `mindroom-chat`): `src/app/invites/inviteLink.ts`, `inviteClaim.ts`, `InvitePage.tsx`, their tests, and one route.

---

## Pull request 1: backend (about +620 source lines, +650 test lines, +110 docs lines)

### Task 1: Store

**Files:**
- Create: `src/mindroom/email_invites/__init__.py`, `src/mindroom/email_invites/store.py`
- Test: `tests/test_email_invite_store.py`

**Interfaces:**
- Produces:

```python
ASKER_DAILY_LIMIT = 5
ROOM_OPEN_LIMIT = 20
ADDRESS_DAILY_LIMIT = 3
DEPLOYMENT_DAILY_LIMIT = 50

class EmailInviteLimitError(ValueError):
    limit: Literal["asker", "room", "address", "deployment"]

@dataclass(frozen=True, slots=True)
class EmailInvite:
    id: int
    room_id: str
    thread_id: str | None
    email: str
    requested_by: str
    agent_name: str
    created_at: int
    expires_at: int
    inviter_id: str | None      # None while waiting
    sent_at: int | None
    revoked: bool
    claimed_by: str | None
    claimed_at: int | None
    invited_at: int | None

def create_request(paths: RuntimePaths, *, room_id: str, thread_id: str | None, email: str,
                   requested_by: str, agent_name: str, expiry_days: int, now: int | None = None) -> EmailInvite
def issue_code(paths: RuntimePaths, invite_id: int, *, inviter_id: str, expiry_days: int,
               now: int | None = None) -> str
def open_invite(paths: RuntimePaths, *, room_id: str, email: str, now: int | None = None) -> EmailInvite | None
def open_invites(paths: RuntimePaths, room_id: str, now: int | None = None) -> list[EmailInvite]
def used_invite(paths: RuntimePaths, *, room_id: str, email: str) -> EmailInvite | None   # newest row with invited_at set
def revoke(paths: RuntimePaths, invite_id: int) -> bool
def find_by_code(paths: RuntimePaths, code: str, now: int | None = None) -> EmailInvite | None
def bind_claim(paths: RuntimePaths, invite_id: int, claimant: str, now: int | None = None) -> bool
def mark_invited(paths: RuntimePaths, invite_id: int, now: int | None = None) -> None
```

Semantics the signatures do not show:

- `create_request` raises `EmailInviteLimitError("asker")` when `requested_by` created 5 rows in the last 24 h, then `("room")` when the room has 20 open rows. It raises `ValueError` when an open row for the room and address exists.
- `issue_code` works only on an open row without a code. It raises `("address")` then `("deployment")` from `sent_at` counts over the last 24 h, sets `inviter_id`, `code_hash`, `sent_at` and `expires_at = now + expiry_days * 86400`, and returns the code.
- `revoke` sets `revoked = 1` only where `invited_at IS NULL` and returns whether a row changed.
- `find_by_code` returns the row with that hash when it is not revoked and either not expired or already used (`invited_at` set).
- `bind_claim` is one `UPDATE ... WHERE id = ? AND revoked = 0 AND expires_at > ? AND invited_at IS NULL AND (claimed_by IS NULL OR claimed_by = ?)` and returns `rowcount == 1`.
- Every write uses `BEGIN IMMEDIATE`. Each write first deletes rows with `expires_at < now - 86400`.
- File `<storage_root>/tracking/email_invites.sqlite`, directory 0700, file 0600, as `desktop/pairing.py:59-85` does.

- [ ] **Step 1: Write the failing tests** in `tests/test_email_invite_store.py`, with a fixed `now` passed in:
  - `test_code_is_stored_hashed_only`: after `issue_code`, the raw code appears nowhere in the database file bytes; `find_by_code(code).id == invite.id`.
  - `test_open_invite_is_unique_per_room_and_address`: a second `create_request` for the same room and address raises `ValueError`; a different room succeeds.
  - `test_asker_cap`: the 6th `create_request` by one asker within 24 h raises with `.limit == "asker"`; at `now + 86401` it succeeds.
  - `test_room_cap`: with 20 open rows in a room (four askers), the 21st raises with `.limit == "room"`; after one `revoke` it succeeds.
  - `test_address_and_deployment_caps`: the 4th `issue_code` for one address in 24 h raises `"address"`; the 51st overall raises `"deployment"`; a failed-then-revoked send still counts.
  - `test_issue_code_extends_expiry`: a request made at `t`, issued at `t + 3 days` with `expiry_days=7`, has `expires_at == t + 10 days`.
  - `test_two_claimants_one_winner`: from two `threading.Thread`s on separate connections, `bind_claim(id, "@a:x")` and `bind_claim(id, "@b:x")` return exactly one `True`; repeating the winner's call returns `True`, the loser's `False`.
  - `test_claim_fails_when_revoked_or_expired`: `bind_claim` is `False` and `find_by_code` is `None` for both.
  - `test_revoke_after_use_changes_nothing`: after `mark_invited`, `revoke` returns `False`, `find_by_code` still returns the row, `open_invite` is `None`, and `used_invite(...).claimed_by` is the claimant.
  - `test_purge`: a row with `expires_at < now - 86400` is gone after the next write.
  - `test_file_modes`: directory mode `0o700`, file mode `0o600`.
- [ ] **Step 2: Run them; expect** `ModuleNotFoundError: mindroom.email_invites`.
- [ ] **Step 3: Implement** `store.py` with one connection per operation (context manager as in `desktop/pairing.py`), `isolation_level=None` and explicit `BEGIN IMMEDIATE`.
- [ ] **Step 4: Run the tests; expect** all pass.
- [ ] **Step 5: Commit** `feat(email-invites): store for requests, codes and caps`.

### Task 2: Config, address, link and mail

**Files:**
- Create: `src/mindroom/config/email_invites.py`, `src/mindroom/email_invites/mail.py`
- Modify: `src/mindroom/config/main.py` (field next to `tool_approval`, line 434), `src/mindroom/runtime_env_policy.py` (`_RUNTIME_STARTUP_EXCLUDED_NAMES`, line 323), `docs/configuration/index.md`
- Test: `tests/test_email_invite_mail.py`, `tests/test_runtime_env_policy.py`

**Interfaces:**
- Produces:

```python
class EmailInvitesConfig(BaseModel):          # extra="forbid"
    chat_url: str = "https://chat.mindroom.chat"
    expiry_days: int = Field(default=7, ge=1, le=30)
    approvers: list[str] = Field(default_factory=list)

Config.email_invites: EmailInvitesConfig

SMTP_URL_ENV = "MINDROOM_SMTP_URL"
SMTP_FROM_ENV = "MINDROOM_SMTP_FROM"
MAIL_SUBJECT = "You are invited to a MindRoom room"

class EmailInviteMailError(Exception): ...

def normalize_email(raw: str) -> str | None
def invite_link(chat_url: str, router_id: str, code: str) -> str
def smtp_configured(paths: RuntimePaths) -> bool
def build_invite_mail(*, sender: str, recipient: str, inviter_name: str, inviter_id: str, room_name: str,
                      link: str, expires_at: int, encrypted: bool) -> EmailMessage
async def send_invite_mail(paths: RuntimePaths, message: EmailMessage) -> None
```

- `chat_url` validator: `normalized_origin(value) == value` (from `mindroom.origins`) and the scheme is `https` or `is_local_origin(value)`.
- `approvers` validator: every entry starts with `@` and contains `:`.
- `normalize_email`: trim; at most 254 characters; full match of `^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$`; return it lower-cased.
- `send_invite_mail` runs `smtplib` in `asyncio.to_thread` with `timeout=20` and `ssl.create_default_context()`; `smtps://` uses `SMTP_SSL` (default port 465); `smtp://` uses `SMTP` (default port 587) and `starttls`, which is required unless the host is `localhost` or a loopback address. It logs in when the URL has a user. Any `smtplib.SMTPException`, `OSError` or a malformed URL becomes `EmailInviteMailError` whose text never contains the URL.

- [ ] **Step 1: Write the failing tests:**
  - `test_normalize_email`: `" Alice@Example.COM "` → `"alice@example.com"`; `None` for `"Alice <a@b.co>"`, `"a@b.co, c@d.co"`, `"a@b.co\nBcc: x@y.co"`, `"a@b"`, `"ä@b.co"`, `"a@bücher.de"`, and a 255-character address; `"a@xn--bcher-kva.de"` is accepted.
  - `test_invite_link`: `invite_link("https://chat.example.org", "@router:example.org", "ab-c_d")` == `"https://chat.example.org/invite/#r=%40router%3Aexample.org&c=ab-c_d"`.
  - `test_config_defaults_and_bounds`: defaults as above; `expiry_days=0` and `31` fail; `chat_url="https://x.org/path"` and `"http://x.org"` fail; `"http://localhost:4200"` passes; `approvers=["bas"]` fails.
  - `test_mail_text`: subject equals `MAIL_SUBJECT`; one `To`; body equals the spec template for fixed inputs, with the expiry as `YYYY-MM-DD`; the encrypted line appears only with `encrypted=True`; `message.get_content_type() == "text/plain"`.
  - `test_names_are_cleaned`: an `inviter_name` with `"\r\n"`, a tab and 200 characters arrives as one line of at most 80 characters.
  - `test_smtps_and_starttls` (monkeypatched `smtplib.SMTP_SSL` and `smtplib.SMTP`): `smtps://u:p%40x@mail.example.org` logs in with `("u", "p@x")` on port 465; `smtp://mail.example.org` calls `starttls` on port 587; `smtp://localhost:2525` does not.
  - `test_send_failure_hides_url`: an `SMTPException` becomes `EmailInviteMailError`, and `"p%40x"` and `"p@x"` are not in its text.
  - `test_smtp_configured_needs_both`: `True` only when both variables are set.
  - `test_smtp_url_is_secret_env` in `tests/test_runtime_env_policy.py`: `public_worker_startup_env({"MINDROOM_SMTP_URL": "x"}) == {}` and `isolated_worker_runtime_env({"MINDROOM_SMTP_URL": "x"}) == {}`.
- [ ] **Step 2: Run them; expect** import errors.
- [ ] **Step 3: Implement** the config model, the `Config` field, the env-policy entry, `mail.py`, and the `email_invites` section in `docs/configuration/index.md` (schema block from the spec).
- [ ] **Step 4: Run the tests; expect** all pass.
- [ ] **Step 5: Commit** `feat(email-invites): config, address check and SMTP mail`.

### Task 3: Policy

**Files:**
- Create: `src/mindroom/email_invites/policy.py`
- Modify: `src/mindroom/matrix/room_power.py` (after `send_power_level`, line 69), `src/mindroom/matrix/personal_room_store.py` (after `personal_room_excludes_router`, line 180)
- Test: `tests/test_email_invite_policy.py`

**Interfaces:**
- Produces:

```python
# matrix/room_power.py
def invite_power_level(power_levels_content: Mapping[str, object]) -> int      # default 0

# matrix/personal_room_store.py
def is_personal_room(runtime_paths: RuntimePaths, room_id: str) -> bool        # any agent's record names the room

# email_invites/policy.py
async def may_invite(client: nio.AsyncClient, room_id: str, user_id: str,
                     approvers: Sequence[str] = ()) -> bool | None
def refused_room_kind(config: Config, paths: RuntimePaths, room_id: str) -> Literal["personal", "onboarding"] | None
```

- `may_invite` returns `True` for a listed approver without reading state. Otherwise it needs membership `join` (`get_room_membership`, `matrix/client_room_admin.py:866`) and `user_power_level(content, user_id, creators) >= invite_power_level(content)`, with creators from `read_room_creators` (`matrix/room_reconciliation.py:57`). It returns `None` when membership, power levels or creators cannot be read.
- `refused_room_kind` returns `"onboarding"` when `config.personal_rooms` is set and the room is in `resolve_room_aliases(config.personal_rooms.onboarding_rooms, paths)`.

- [ ] **Step 1: Write the failing tests** with an `AsyncMock` client:
  - `test_default_invite_level_lets_members_invite`: power levels without `invite`, member at 0 → `True`.
  - `test_raised_invite_level`: `invite: 50`, member at 0 → `False`; at 50 → `True`.
  - `test_creator_in_privileged_room_version`: user absent from `users`, but in the creators set → `True`.
  - `test_not_joined`: membership `leave` or `invite` → `False`.
  - `test_unreadable_state`: an error response for power levels → `None`.
  - `test_listed_approver`: `may_invite(client, room, "@ops:example.org", ["@ops:example.org"])` → `True` with no client call.
  - `test_refused_rooms`: a room with a personal-room record → `"personal"`; a configured onboarding room given by alias → `"onboarding"`; any other room → `None`.
- [ ] **Step 2: Run them; expect** import errors.
- [ ] **Step 3: Implement** the three helpers.
- [ ] **Step 4: Run the tests; expect** all pass.
- [ ] **Step 5: Commit** `feat(email-invites): invite-right and refused-room checks`.

### Task 4: Toolkit

**Files:**
- Create: `src/mindroom/custom_tools/email_invite.py`, `src/mindroom/tools/email_invite.py`
- Modify: `src/mindroom/tools/__init__.py` (import beside `invite_router_tools`, line 96), `pyproject.toml` (`email_invite = []` beside `invite_router = []`, line 275), `src/mindroom/tools_metadata.json` (regenerate with the command `tests/test_tools_metadata.py:103-110` prints), `docs/tools/matrix-and-attachments.md`
- Test: `tests/test_email_invite_tool.py`

**Interfaces:**
- Consumes: Task 1 store functions, Task 2 `normalize_email`, `invite_link`, `smtp_configured`, `build_invite_mail`, `send_invite_mail`, `EmailInviteMailError`, Task 3 `may_invite`, `refused_room_kind`.
- Produces: `class EmailInviteTools(Toolkit)` named `email_invite` with `async invite_by_email(email: str) -> str`, `async list_email_invites() -> str`, `async revoke_email_invite(email: str) -> str`. `offered_function` gives `invite_by_email` the gate `ToolGate.CONFIRM`, as `custom_tools/contacts.py:61-65` does. Registration copies `tools/invite_router.py:14-26` (`requires_room_context=True`, `requires_primary_runtime=True`, `ToolCategory.COMMUNICATION`).

`invite_by_email` follows data-flow steps 3 to 5 of the spec. Details the spec leaves to this task:

- Window check: `auto_approve_expires_at(paths, room_id=context.room_id, thread_id=context.target.resolved_thread_id, requester_id=<approver>)`, where `<approver>` is `resolve_tool_approval_approver(config, paths, context.requester_id)`; a `None` approver is refused.
- Router rights: `may_invite(context.client, room_id, router_id)` with the router ID from `entity_identity_registry` (as `custom_tools/invite_router.py:57-58`); `None` or `False` refuses.
- Asker rights: `may_invite(..., approver, config.email_invites.approvers)`; `None` refuses with "room state unreadable".
- Mail inputs: room name from `get_room_name`; `encrypted` from `room_encryption_enabled(...) is True`; inviter name from `context.room.user_name(approver)` when the room is cached, else the Matrix ID.
- Results, exact text (`{date}` is the expiry as `YYYY-MM-DD`):
  - `Error: auto-approve is on in this conversation. Send "!auto_approve off" and ask again, so the invitation gets its own approval.`
  - `Error: that is not an email address this tool accepts (one plain ASCII address).`
  - `Error: people cannot be invited by email to a personal room.` and the same with `an onboarding room`.
  - `Error: the router may not invite people to this room.`
  - `Stored, nothing sent: {asker} may not invite people to this room. Someone who may invite can approve it by asking an agent here: "approve the invite for {email}".`
  - `An invite for {email} is already waiting for someone who may invite.` and `An invite for {email} was already sent; it is valid until {date}.`
  - `Invitation mailed to {email}. The link works once and expires on {date}.`
  - `No mail server is configured, so nothing was mailed. Give this link to {email} yourself. It works once, expires on {date}, and lets whoever opens it join this room: {link}`
  - `Error: limit reached: {text}.` with `at most 5 invites per person per day`, `at most 20 open invites per room`, `at most 3 mails per address per day`, or `at most 50 mails per day`.
  - `Error: the mail could not be sent ({reason}). The invite was cancelled.`
- `revoke_email_invite`: allowed for the row's `requested_by`, its `inviter_id`, or a caller for whom `may_invite` is `True`. Results: `Revoked the invite for {email}.`, `No open invite for {email} in this room.`, `Error: only the asker, the approver, or someone who may invite can revoke this.` When the address has no open row but `used_invite` finds one: `The invite for {email} was already used by {claimed_by}; nothing was changed.`

- [ ] **Step 1: Write the failing tests** (context from `tests/authorization_helpers.make_test_tool_runtime_context`, `AsyncMock` client, `may_invite` and `send_invite_mail` monkeypatched where a test is not about them):
  - `test_invite_by_email_is_confirm_gated`: the offered `invite_by_email` has `gate is ToolGate.CONFIRM`; the other two have no gate.
  - `test_refuses_inside_auto_approve_window`: with a window stored for the conversation, the result is the first text and the store is empty.
  - `test_refuses_bad_address`, `test_refuses_personal_room`, `test_refuses_onboarding_room`, `test_refuses_when_router_may_not_invite`: exact texts; store empty; no mail.
  - `test_asker_without_right_stores_waiting`: result is the "Stored" text; one open row with `inviter_id is None`; no mail; no code.
  - `test_asker_without_right_asks_again`: second call returns the "already waiting" text and adds no row.
  - `test_approver_sends_waiting_request`: after a waiting request by `@carol:example.org`, a call by `@bas:example.org` (may invite) mails once; the row has `requested_by == "@carol:example.org"` and `inviter_id == "@bas:example.org"`.
  - `test_approver_resend_revokes_old_code`: a second send for the same address makes the first code fail `find_by_code` and the second succeed.
  - `test_listed_approver_not_in_room_may_send`.
  - `test_smtp_mode_returns_no_code`: the result contains neither the code nor `/invite/#`.
  - `test_no_smtp_mode_returns_link`: the result contains `invite_link(...)` for the issued code.
  - `test_send_failure_revokes`: `EmailInviteMailError("connection refused")` → the "could not be sent" text; no open row; the address count is 1.
  - `test_cap_texts`: each of the four limits yields its text.
  - `test_list_is_room_scoped`: rows of another room are absent; a waiting and a sent row show their state.
  - `test_revoke_rules`: the asker may; a stranger without the right gets the error; someone who may invite may.
  - `test_revoke_after_use_reports_claimant`: the result names `@alice:example.org` and the row is unchanged.
- [ ] **Step 2: Run them; expect** import errors.
- [ ] **Step 3: Implement** the toolkit, its registration, the metadata file, and a section for the three functions in `docs/tools/matrix-and-attachments.md`.
- [ ] **Step 4: Run** `tests/test_email_invite_tool.py tests/test_tools_metadata.py`; **expect** all pass.
- [ ] **Step 5: Commit** `feat(email-invites): email_invite toolkit`.

### Task 5: Claim receiver and docs

**Files:**
- Create: `src/mindroom/email_invites/receiver.py`, `docs/email-invites.md`
- Modify: `src/mindroom/bot.py` (after `register_model_catalog_receiver(...)`, lines 1649-1656), `docs/tool-approval.md` ("Auto-Approve Everything", line 69), `zensical.toml` (navigation entry beside "Tool Approval", line 99)
- Test: `tests/test_email_invite_receiver.py`

**Interfaces:**
- Consumes: Task 1 `find_by_code`, `bind_claim`, `mark_invited`, `revoke`; Task 3 `may_invite`, `refused_room_kind`.
- Produces:

```python
CLAIM_EVENT_TYPE = "io.mindroom.email_invite.claim"
RESULT_EVENT_TYPE = "io.mindroom.email_invite.result"

def register_email_invite_receiver(
    *, client: nio.AsyncClient, agent_name: str, runtime_paths: RuntimePaths,
    config_getter: Callable[[], Config],
    callback_wrapper: Callable[[Callable[[nio.UnknownToDeviceEvent], Awaitable[None]]], Callable[..., Awaitable[None]]],
) -> None
```

It registers only when `agent_name == ROUTER_AGENT_NAME`, with `client.add_to_device_callback(on_event, nio.UnknownToDeviceEvent)`; `AuthenticatedToDeviceEvent` is a subclass, so an encrypted claim is handled the same way. Admission (type, shape, sender, rate limits) runs synchronously before the wrapped task is created, as `model_catalog_receiver.py:310-337` does. A claim that fails admission gets no answer.

Admission: `event.type == CLAIM_EVENT_TYPE`; content keys exactly `{"version", "request_id", "code"}`; `version` is the `int` 1; `request_id` a string of 1 to 64 characters; `code` a string of 1 to 128 characters; `is_human_requester_id(event.sender, config, paths)`; the rate limits from Global Constraints over a sliding 60 seconds.

Handling, in this order (each `→` sends the result and stops):

1. `invite = find_by_code(code)`; `None` → `rejected`.
2. `invite.claimed_by` is another account → `rejected`.
3. `invite.invited_at` is set → `invited` (no Matrix call).
4. `get_room_membership(client, room, sender)`: `ban` → `rejected`; `join` or `invite` → `mark_invited` when `claimed_by == sender`, then `invited`. `None` and `leave` continue.
5. `bind_claim(invite.id, sender)` is `False` → `rejected`.
6. `refused_room_kind(...)` is set → `revoke`, `rejected`.
7. `may_invite(client, room, invite.inviter_id, config.email_invites.approvers)`: `False` → `revoke`, `rejected`; `None` → `retry`.
8. `may_invite(client, room, <router ID>)` is not `True` → `retry`.
9. `client.room_invite(room, sender)`: `RoomInviteResponse` → `mark_invited`, `invited`, then the notice. `RoomInviteError` with `status_code == "M_FORBIDDEN"` → `rejected`. Any other error response, `aiohttp.ClientError` or `TimeoutError` → `retry`.

Result: `client.to_device(nio.ToDeviceMessage(type=RESULT_EVENT_TYPE, recipient=sender, recipient_device="*", content={"version": 1, "request_id": ..., "status": ...}))`, with `"room_id"` added for `invited`. A failed send is logged and nothing else changes.
Notice: body `Invite for {email} (approved by {inviter_id}) was used by {sender}.`, `msgtype` `m.notice`, in thread `invite.thread_id` when set, sent with `build_message_content` and `send_message_result`. A failed notice is logged; the result stays `invited`.
Logs name the sender, the invite ID and the status. They never contain the code or the address.

- [ ] **Step 1: Write the failing tests** (events built as `nio.UnknownToDeviceEvent(source, sender, CLAIM_EVENT_TYPE)`; `AsyncMock` client; a real store in `tmp_path`):
  - `test_valid_claim_invites_answers_and_posts_notice`: `room_invite` awaited once with `(room, "@alice:example.org")`; the result content is `{"version": 1, "request_id": "r1", "status": "invited", "room_id": room}` to `("@alice:example.org", "*")`; the notice body is exact and its `msgtype` is `m.notice`; the row has `claimed_by` and `invited_at`.
  - `test_unknown_revoked_and_expired_codes_answer_rejected_alike`: three claims, three contents equal except `request_id`; no Matrix invite.
  - `test_malformed_claims_get_no_answer`: an extra key, `version: "1"`, `version: True`, an empty code, a 129-character code, a missing `request_id` → `to_device` never called.
  - `test_agent_and_bot_senders_are_ignored`: a claim from the router's own ID and from a configured `bot_accounts` entry → no answer.
  - `test_per_sender_and_global_rate_limits`: the 11th claim of one sender within 60 s and the 61st overall get no answer and do not read the store.
  - `test_second_account_is_rejected`: after `@alice` is invited, `@mallory` with the same code gets `rejected`.
  - `test_restart_between_bind_and_invite` (Review Focus 3): with a row already bound to `@alice` and `invited_at` unset, `@alice`'s claim invites her; `@mallory`'s claim gets `rejected`.
  - `test_approver_demoted_revokes` (Review Focus 4): `may_invite` for the inviter returns `False` → `rejected`, the row is revoked, no Matrix invite.
  - `test_unreadable_state_answers_retry_and_keeps_binding`: `may_invite` returns `None` → `retry`; the row stays bound to the sender and is not revoked.
  - `test_router_without_right_answers_retry`.
  - `test_forbidden_invite_is_rejected_other_errors_retry`: `RoomInviteError` with `M_FORBIDDEN` → `rejected`; with `M_UNKNOWN`, and a raised `TimeoutError` → `retry`; in all three the row stays bound and `invited_at` is unset.
  - `test_removed_member_gets_no_second_invite` (Review Focus 5): row used by `@alice`, membership now `leave` → result `invited`, `room_invite` not called.
  - `test_already_joined_claimant_does_not_use_up_the_code`: `@bob` is joined and presents an unbound code → `invited`; the row is still unbound.
  - `test_banned_claimant_is_rejected`.
  - `test_notice_failure_keeps_result`.
  - `test_registered_for_router_only`: `add_to_device_callback` is called for `ROUTER_AGENT_NAME` and not for another agent.
  - `test_encrypted_claim_is_handled`: the same claim as `nio.AuthenticatedToDeviceEvent` invites.
- [ ] **Step 2: Run them; expect** import errors.
- [ ] **Step 3: Implement** `receiver.py` and the registration call in `bot.py` with `config_getter=lambda: self.config` and the same `callback_wrapper` as its neighbours.
- [ ] **Step 4: Write the docs.** `docs/email-invites.md` covers, from the spec: set-up (tool name, config block, the two environment variables), who approves and how a waiting request is approved, the refusal inside an auto-approve window, what happens without SMTP and its cost, the bearer-link limit, encrypted rooms, whether agents answer the newcomer, the caps, list and revoke. Add one sentence to `docs/tool-approval.md` under "Auto-Approve Everything": `invite_by_email` refuses while a window is open. Add the page to `zensical.toml`. Do not edit `skills/mindroom-docs/references/`.
- [ ] **Step 5: Run** all five `tests/test_email_invite_*.py` files once with the default options, then `pre-commit run --all-files`; **expect** all pass.
- [ ] **Step 6: Commit** `feat(email-invites): router claim receiver and docs`.
- [ ] **Step 7: Open pull request 1.** Its body says "No visible change" for Chat and links the spec.

---

## Pull request 2: Chat (about +240 product lines, +260 test lines, plus locale strings)

Work in a worktree of `mindroom-chat` from fresh `origin/main`. Read its `AGENTS.md` first. The page works only against a backend that has pull request 1.

### Task 6: Link parsing and route

**Files:**
- Create: `src/app/invites/inviteLink.ts`, `src/app/invites/inviteLink.test.ts`
- Modify: `src/app/routes/paths.ts` (after `CONNECT_PATH`), `src/app/shell/Router.tsx` (inside `<Route element={<ProtectedRoutes .../>}>`, before the space routes), `src/app/navigation/routeSessionGuards.test.ts`

**Interfaces:**
- Produces:

```ts
// routes/paths.ts
export const INVITE_PATH = '/invite/';

// invites/inviteLink.ts
export type InviteLink = { routerId: string; code: string };
/** Parses `#r=<router ID>&c=<code>`; undefined unless both are present and well formed. */
export const parseInviteFragment = (hash: string): InviteLink | undefined;
```

`routerId` must match `^@[^\s:]+:[^\s]+$`; `code` is 1 to 128 characters. The route renders `InvitePage` (Task 8) lazily; until Task 8 it may render a placeholder element.

- [ ] **Step 1: Write the failing tests:**
  - `parses a complete fragment`: `parseInviteFragment('#r=%40router%3Aexample.org&c=ab-c_d')` equals `{ routerId: '@router:example.org', code: 'ab-c_d' }`.
  - `rejects incomplete or malformed fragments`: `''`, `'#c=x'`, `'#r=%40router%3Aexample.org'`, `'#r=router&c=x'`, `'#r=%40a%3Ab&c='`, and a 129-character code give `undefined`.
  - In `routeSessionGuards.test.ts`, `keeps the invite fragment through sign-in`: `resolveProtectedRouteRedirect('<origin>/invite/#r=%40router%3Aexample.org&c=x', undefined, false, undefined)?.afterLoginPath` equals `'/invite/#r=%40router%3Aexample.org&c=x'`.
- [ ] **Step 2: Run** `just test src/app/invites src/app/navigation`; **expect** the new tests fail.
- [ ] **Step 3: Implement** the constant, the parser and the route.
- [ ] **Step 4: Run the tests; expect** all pass.
- [ ] **Step 5: Commit** `feat(invites): invite link parsing and route`.

### Task 7: Claim transport

**Files:**
- Create: `src/app/invites/inviteClaim.ts`, `src/app/invites/inviteClaim.test.ts`

**Interfaces:**
- Consumes: `InviteLink` from Task 6.
- Produces:

```ts
export const CLAIM_EVENT = 'io.mindroom.email_invite.claim';
export const RESULT_EVENT = 'io.mindroom.email_invite.result';
export const CLAIM_TIMEOUT_MS = 20_000;
export const CLAIM_RESEND_MS = 5_000;

export type ClaimOutcome =
  | { status: 'invited'; roomId: string }
  | { status: 'rejected' }
  | { status: 'retry' }
  | { status: 'offline' };

/**
 * Sends the claim and reports outcomes: `offline` after CLAIM_TIMEOUT_MS without an answer, and
 * any final outcome whenever it arrives, also after `offline`. Returns a function that stops it.
 */
export const startClaim = (
  mx: MatrixClient,
  link: InviteLink,
  onOutcome: (outcome: ClaimOutcome) => void
): (() => void);

/** The room this account is invited to by `routerId`, limited to `roomId` when given. */
export const pendingRouterInvite = (
  mx: MatrixClient,
  routerId: string,
  roomId?: string
): string | undefined;
```

- The claim is sent with `mx.sendToDevice(CLAIM_EVENT, new Map([[link.routerId, new Map([['*', { version: 1, request_id, code: link.code }]])]]))`, at once and once more after `CLAIM_RESEND_MS` with the same `request_id` (`crypto.randomUUID()`).
- A result counts only when `message.type === RESULT_EVENT`, `message.sender === link.routerId`, `content.version === 1` and `content.request_id` is the one sent. It is read from `ClientEvent.ReceivedToDeviceMessage`, encrypted or not.
- Fallback without a result: before sending, note the rooms where `pendingRouterInvite` already holds. On `RoomEvent.MyMembership`, an invitation from `link.routerId` to a room not in that note reports `{ status: 'invited', roomId }`.
- `invited`, `rejected` and `retry` are final: listeners and timers are removed. `offline` is reported once and listening continues.
- `pendingRouterInvite` reads `room.getMember(mx.getSafeUserId())?.events.member?.getSender()` on rooms whose own membership is `invite`, as `rooms/join/JoinBeforeNavigate.tsx:110` does.

- [ ] **Step 1: Write the failing tests** with a fake client (an `EventEmitter` with `sendToDevice`, `getSafeUserId`, `getRooms`) and fake timers:
  - `sends the claim to the router's devices and resends once`: two `sendToDevice` calls, both to `'@router:example.org'` / `'*'`, same `request_id`, the second at 5 s.
  - `reports the router's result`: a matching `invited` result gives `{ status: 'invited', roomId: '!room:example.org' }` and stops the resend.
  - `ignores a result from another sender`, `ignores another request_id`, `ignores an invited result without room_id`.
  - `reports rejected and retry`.
  - `reports offline after 20 seconds and still reports a late result`.
  - `counts a new invitation from the router as invited`: a `MyMembership` invite whose member event sender is the router gives `invited` with that room.
  - `ignores an invitation that was pending before the claim` and `ignores an invitation from someone else`.
  - `stop removes listeners and timers`: after the stop function, no outcome is reported and no second send happens.
- [ ] **Step 2: Run** `just test src/app/invites`; **expect** the new tests fail.
- [ ] **Step 3: Implement** `inviteClaim.ts`.
- [ ] **Step 4: Run the tests; expect** all pass.
- [ ] **Step 5: Commit** `feat(invites): claim transport`.

### Task 8: Invite page

**Files:**
- Create: `src/app/invites/InvitePage.tsx`, `src/app/invites/InvitePage.test.tsx`
- Modify: `src/app/shell/Router.tsx` (render `InvitePage`), `src/app/i18n/locales/en.json` and the other locale files (new keys under `mindroomUi.invites`)

**Interfaces:**
- Consumes: `parseInviteFragment`, `startClaim`, `pendingRouterInvite`, `ClaimOutcome`.
- Produces: `export function InvitePage(): JSX.Element`.

States and their English text are the table in the spec's "Chat landing page", verbatim; the keys are `mindroomUi.invites.<state>` plus `join` and `tryAgain` for the buttons. Layout follows `local-mindroom/ConnectPage.tsx`.

- The page parses `window.location.hash` once on mount, keeps the link in a ref, and then calls `history.replaceState` with the path alone.
- `window.top !== window.self` shows `framed`. A missing link shows `invalid`.
- Nothing is sent before the Join click. A click in `ready` or `retry` calls `startClaim`.
- On `invited`: already joined → open the room. `pendingRouterInvite(mx, routerId, roomId)` → `mx.joinRoom(roomId, { viaServers: [<server name of routerId>] })`, then open the room with `getHomeRoomPath(roomId)`. An invitation to that room from someone else → open `getHomeRoomPath(roomId)` without joining. None → wait up to 20 seconds for `RoomEvent.MyMembership`, then show `rejected`.
- Unmount stops the claim.

- [ ] **Step 1: Write the failing tests** (mocks as in `local-mindroom/ConnectPage.test.tsx`; `startClaim` mocked):
  - `sends nothing on mount` (Review Focus 6): after render with a valid fragment, `startClaim` and `sendToDevice` are not called, and the Join button and both Matrix IDs are shown.
  - `removes the fragment from the address bar`: `window.location.hash === ''` after mount, and a click still claims with the parsed code.
  - `shows invalid for a bad fragment` and `shows framed inside a frame`; neither renders a Join button.
  - `joins the router's invitation and opens the room`: outcome `invited` with a pending router invitation → `joinRoom('!room:example.org', { viaServers: ['example.org'] })` and navigation to the room.
  - `opens the room when already joined`: no `joinRoom`.
  - `does not join an invitation from someone else`: navigation to the room path, no `joinRoom`.
  - `shows rejected when no invitation arrives after invited`: after 20 s of fake time.
  - `shows rejected, retry and offline`: each outcome shows its text; `retry` shows Try again, which claims again; a late `invited` after `offline` joins.
  - `shows the join error and retries the join`.
  - `stops the claim on unmount`.
- [ ] **Step 2: Run** `just test src/app/invites`; **expect** the new tests fail.
- [ ] **Step 3: Implement** the page and the strings; run `just test src/app/i18n` and add what its locale tests ask for.
- [ ] **Step 4: Run** `just check`; **expect** it passes.
- [ ] **Step 5: Take screenshots** of `ready`, `claiming`, `offline`, `rejected`, `retry` and `joinFailed` with placeholder data (the repository's screenshot method in its `AGENTS.md`), and look at them.
- [ ] **Step 6: Commit** `feat(invites): invite page`, and open pull request 2 with a `## Screenshots` section.

---

### Task 9: Live test on a throwaway deployment

Not on lab or production. Fresh homeserver, backend from the pull request 1 branch, Chat from the pull request 2 branch, a local SMTP sink (`python -m aiosmtpd -n -l 127.0.0.1:2525` from a throwaway environment, or any mail catcher), `MINDROOM_SMTP_URL=smtp://127.0.0.1:2525`, `email_invites.chat_url` set to the local Chat origin, one agent with `email_invite` in a room with the router. Tear everything down afterwards.

- [ ] New account, end to end: ask, approve the card, read the mail in the sink, open the link signed out, sign up, land on the page, Join, read a new message in the room. The room shows the notice.
- [ ] Sign-up slower than 15 minutes (shorten by clearing the saved path): opening the mail link again while signed in still works (Review Focus 6).
- [ ] Existing account uses a link.
- [ ] A second account opens the used link: `rejected`.
- [ ] Member who may not invite asks: nothing is mailed; `list_email_invites` shows `waiting`; an admin says "approve the invite for …", gets a card, and the mail arrives.
- [ ] `!auto_approve 10` then an invite request: refused, no mail.
- [ ] Revoke, then open the link: `rejected`.
- [ ] Stop MindRoom, click Join, see `offline`, start MindRoom: the page joins the room.
- [ ] Encrypted room: join works; a message sent after the join is readable; the mail has the encryption line.
- [ ] Without the SMTP variables: the agent shows the link; it works.
- [ ] If the set-up has a second federated homeserver: an account there uses a link.
- [ ] Record what failed and fix it in the pull request that owns the code. Record which of the spec's "Not verified" items the run settled.
