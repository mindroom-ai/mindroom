# GitHub requests for client plugins with the user's connected account

## Goal

The pull request side panel (mindroom-ai/pull-request-client-plugin) reads and writes GitHub with a token the user types into the browser.
A user who already connected GitHub in MindRoom (Connect GitHub, the `github` tool's requester-scoped OAuth account) should not have to.
The panel asks the room's MindRoom server to make the GitHub request, and the server adds that user's own token.
The token never leaves the server.

## Design

### Server: `POST /api/github/request`

- **Who:** the caller presents a client session (`presented_session`, the bearer MindRoom Chat gets by trading a Matrix OpenID token), so the route knows the Matrix user. No session: 401.
- **Whose token:** that user's own `github_oauth` connection, resolved and refreshed through the existing OAuth path (`resolve_oauth_token` in `egress_broker/oauth_source.py` or the same lifecycle the `github` tool uses). Never another user's, never a shared or `GITHUB_TOKEN` credential, never an administrator key.
- **Body:** `{method, path, body?, accept?, max_bytes?}`.
  - `method`: one of `GET`, `POST`, `PUT`, `PATCH`, `DELETE`.
  - `path`: must be `/graphql` or match `^/repos/[^/]+/[^/]+(/.*)?$` (plus `?query`), with no `..`, no `//`, no scheme or host, no control characters. Anything else: 400.
  - `accept`: optional, only `application/vnd.github+json`, `application/vnd.github.full+json`, `application/vnd.github.diff`, `application/vnd.github.raw+json`; default `application/vnd.github+json`.
  - `max_bytes`: optional integer, 1 to 10,485,760; defaults to 10 MiB. Reading stops when the streamed body exceeds the cap, with 502 `github_response_too_large`.
- **Upstream:** always `https://api.github.com` + path, with `Authorization: Bearer <token>`, `X-GitHub-Api-Version: 2022-11-28`, a timeout, no redirects followed, a response size cap.
- **Answer:** `{status, body, headers}` with GitHub's status and JSON or text and only these headers passed through: `x-ratelimit-remaining`, `x-ratelimit-reset`, `retry-after`, `x-github-request-id`. Relayed answers use HTTP 200, so the client can tell GitHub's refusal from the route's.
  With `application/vnd.github.raw+json`, the body is always a string decoded as strict UTF-8, or the route answers 502 `github_response_not_text`.
- **Not connected:** 409 `{"error": "github_not_connected"}` (also when the OAuth client is not configured), so the client falls back.
- **`GET /api/github/status`:** `{connected: bool, login?: str}` for the session's user, from stored state only (no GitHub call), so the panel can choose its path once.
- **Logs:** the route's own lines carry method, path template and status only; the token and request/response bodies never. HTTP client libraries (httpx at INFO, httpcore at DEBUG) may log the full URL, including repository and query, as for every outbound call; only the operator reads those logs, and the operator already holds the token.

### Plugin

- A small transport in `src/github.ts`: if `useMindroomServer()` gives a server and `GET /api/github/status` says connected, every GitHub read and write goes through `POST /api/github/request`; else it uses the browser token as today.
- The stored browser token still wins if set (the user chose it explicitly). Settings shows which path is active: "Using your GitHub account connected in MindRoom (login)" or the token field.
- Without a server connection and without a token, nothing changes: anonymous reads, no write controls.
- Query keys and errors never contain a token on either path (unchanged invariant).

## Prerequisite on each deployment

The `github` OAuth provider expects a **GitHub App** (no classic scopes, the App's fine-grained permissions apply; `docs/tools/project-management.md`).
For merge, update branch and delete branch, the App needs Pull requests: read and write, Contents: read and write, Checks: read, Metadata: read, and must be installed on the repositories.
A user token reaches only repositories where the App is installed.

## Out of scope

- Proxying for anything other than `/repos/...` and `/graphql`.
- Sharing one user's account with another user.
- Changing the agent-side `github` tool or Agent Keys.

## Risks

- Any code running in a MindRoom Chat page with the user's session can make GitHub requests as that user through this route, limited to `/repos` and `/graphql` and the App's permissions. `/graphql` is not limited to one repository, so it reaches whatever the App's permissions and installations allow. That is the same reach a page already has over the browser token, and less than handing out the token (no refresh token, no other GitHub APIs, revocable by disconnecting).
- GitHub App user tokens expire after 8 hours; the existing refresh path handles that.
