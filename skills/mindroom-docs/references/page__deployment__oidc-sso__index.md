# OIDC & Single Sign-On

Self-hosted deployments running MindRoom's Tuwunel fork (`mindroom-tuwunel`) can delegate Matrix sign-in to any OpenID Connect (OIDC) identity provider that supports Authorization Code with PKCE (`S256`), `client_secret_post`, and a UserInfo endpoint returning `sub`, including Pocket ID, Authelia, Authentik, and Keycloak.
Tuwunel exposes the same configured identity provider to `m.login.sso` clients (such as MindRoom Chat) and to next-generation Matrix OIDC clients (such as Element X) through its built-in OIDC authorization server.

This page covers configuring Tuwunel, wiring Docker Compose (`mindroom-stack`) or Kubernetes, running an SSO-only homeserver without breaking MindRoom's agent accounts, configuring MindRoom Chat, routing a reverse proxy, and authenticating to the MindRoom dashboard.
Hosted `mindroom.chat` uses MindRoom's own configured SSO providers; bringing your own identity provider applies to self-hosted homeservers.

## Identity Provider Requirements

Create a **confidential** OAuth 2.0 / OIDC client in your identity provider:

- **Client authentication:** Confidential client with a `client_secret` (Tuwunel requires `client_secret` or `client_secret_file`; public clients without a secret are not supported).
- **Grant and flow:** Authorization Code flow (`response_type=code`) with PKCE (`S256`) and `client_secret_post` (providers that only accept `client_secret_basic` are not supported).
- **UserInfo endpoint:** After exchanging the authorization code, Tuwunel queries the discovered UserInfo endpoint and requires a JSON response containing `sub`.
- **Scopes:** `openid`, `email`, and `profile` (Tuwunel's default).
- **Callback / redirect URI:** Register this exact URL with your identity provider:

```text
https://<your-matrix-host>/_matrix/client/unstable/login/sso/callback/<client_id>
```

Tuwunel discovers endpoints automatically from `<issuer_url>/.well-known/openid-configuration`.
Keep `client_id` and `issuer_url` unchanged after users begin signing in, because Tuwunel binds stored user identities to them.

## Configure Tuwunel

The examples below use [Pocket ID](https://pocket-id.org/) as a concrete self-hosted OIDC provider; substitute your own `brand`, `name`, and `issuer_url` for Authelia, Authentik, Keycloak, or another OIDC provider.

=== "tuwunel.toml"

    ```toml
    [global.well_known]
    client = "https://matrix.example.com"

    [[global.identity_provider]]
    brand = "PocketID"
    name = "Pocket ID"
    client_id = "<client_id>"
    client_secret_file = "/etc/tuwunel/oidc_client_secret"  # or client_secret = "<client_secret>"
    issuer_url = "https://id.example.com"
    callback_url = "https://matrix.example.com/_matrix/client/unstable/login/sso/callback/<client_id>"
    ```

=== "Docker Compose (mindroom-stack)"

    In [`mindroom-stack`](https://github.com/mindroom-ai/mindroom-stack), set `CLIENT_HOMESERVER_URL=https://matrix.example.com` in `.env` (which sets `TUWUNEL_WELL_KNOWN__CLIENT`) and add your provider in a `compose.override.yaml`:

    ```yaml
    services:
      tuwunel:
        environment:
          TUWUNEL_IDENTITY_PROVIDER__0__BRAND: "PocketID"
          TUWUNEL_IDENTITY_PROVIDER__0__NAME: "Pocket ID"
          TUWUNEL_IDENTITY_PROVIDER__0__CLIENT_ID: "${OIDC_CLIENT_ID}"
          TUWUNEL_IDENTITY_PROVIDER__0__CLIENT_SECRET: "${OIDC_CLIENT_SECRET}"
          TUWUNEL_IDENTITY_PROVIDER__0__ISSUER_URL: "https://id.example.com"
          TUWUNEL_IDENTITY_PROVIDER__0__CALLBACK_URL: "https://matrix.example.com/_matrix/client/unstable/login/sso/callback/${OIDC_CLIENT_ID}"
    ```

    Put `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET` in `.env`, or mount a secret file and use `TUWUNEL_IDENTITY_PROVIDER__0__CLIENT_SECRET_FILE`.
    Multiple providers use sequential indexes (`__0__`, `__1__`, and so on).

    > [!WARNING]
    > `mindroom-stack` defaults to open local registration (`TUWUNEL_ALLOW_REGISTRATION: "true"`) and grants local users access to the shell-enabled `Mind` agent.
    > Adding an identity provider does not turn off password registration, and hiding Chat's Register link does not enforce it server-side.
    > Before exposing the stack beyond localhost, restrict agent access in `config/config.yaml`, limit who can create accounts through SSO (at your IdP, or via the provider's `registration = false` or `registration_rules`), and close open password registration in one of two ways (turning off registration alone stops MindRoom from creating new password-based agent accounts):
    >
    > - Keep password-based agents with token-gated registration: add `TUWUNEL_REGISTRATION_TOKEN` (or `TUWUNEL_REGISTRATION_TOKEN_FILE`) and `TUWUNEL_YES_I_AM_VERY_VERY_SURE_I_WANT_AN_OPEN_REGISTRATION_SERVER_PRONE_TO_ABUSE: "false"` to `services.tuwunel.environment`, and the matching `MATRIX_REGISTRATION_TOKEN` to `services.mindroom.environment`.
    > - Or switch MindRoom to [`appservice` mode](#disabling-password-login-sso-only) before setting `TUWUNEL_ALLOW_REGISTRATION: "false"` (and optionally `TUWUNEL_AUTHENTICATION__LOGIN__WITH_PASSWORD: "false"` for SSO-only sign-in).

=== "Kubernetes (Helm)"

    The [Tuwunel chart](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/tuwunel/README.md#oidc-login) renders `[[global.identity_provider]]` and mounts the client secret from a Kubernetes Secret:

    ```yaml
    tuwunel:
      serverName: example.com
      clientBaseUrl: https://matrix.example.com
      oidc:
        enabled: true
        brand: PocketID
        name: Pocket ID
        issuer: https://id.example.com
        clientId: <client_id>
        clientSecret:
          existingSecret: matrix-oidc
          key: OIDC_CLIENT_SECRET
        scope: [openid, profile, email]
    ```

### Matching Existing Matrix Accounts

By default, Tuwunel creates a new Matrix account on a user's first SSO sign-in if the claimed username already belongs to an existing account.
To attach SSO logins to existing accounts on an identity provider where you control the matched claims (self-hosting alone is not enough if users can choose another person's username or email):

- Set `trusted = true` (with `userid_claims`, such as `["preferred_username"]` or `["sub"]`, matching your Matrix localparts) so Tuwunel signs the user into the matching existing account.
  Never set `trusted = true` on public providers such as Google or GitHub, or on any provider where users can freely pick the matched claim, because it enables account takeover.
- Or map specific addresses explicitly to full local Matrix user IDs with `assigned_accounts = { "alice@example.com" = "@alice:example.com" }`.
  Do not combine this with `trusted = true`, and use it only with an OIDC client restricted to your own organization or paired with an organization-specific `assigned_accounts_claims` check (such as `{ hd = "example.com" }`) set before the first sign-in.

See the `mindroom-tuwunel` [Identity Providers guide](https://github.com/mindroom-ai/mindroom-tuwunel/blob/main/docs/authentication/providers.md) for the full option reference, registration rules, and provider-specific guides for Authelia, Authentik, Keycloak, and MAS.

## Disabling Password Login (SSO-Only)

By default, MindRoom registers and logs in its agent, team, and router accounts with passwords (`MATRIX_MANAGED_ACCOUNT_AUTH=password`).
Turning off password login on Tuwunel (`authentication.login.with_password = false` or `TUWUNEL_AUTHENTICATION__LOGIN__WITH_PASSWORD="false"`) refuses password registration and fresh password logins for every account; existing agents with valid stored device tokens can restart, but fresh installs and new agents or devices fail until you switch MindRoom to [`appservice` mode](https://docs.mindroom.chat/matrix/#agent-users):

1. Create an application-service registration file (for example `mindroom-appservice.yaml`) whose exclusive `users` namespace matches every MindRoom-managed account, including any custom `matrix_username` or `mindroom_user.username`:

    ```yaml
    id: mindroom
    url: null
    as_token: <long-random-secret>
    hs_token: <another-long-random-secret>
    sender_localpart: mindroom-appservice
    namespaces:
      users:
        - exclusive: true
          # Widen this regex (for example "^@(mindroom_.*|custom_bot):example\\.com$")
          # if you set custom matrix_username or mindroom_user.username values.
          regex: "^@mindroom_.*:example\\.com$"
    ```

2. Mount or place that `.yaml` file in a directory readable by Tuwunel and set `appservice_dir = "/etc/tuwunel/appservices"` (`TUWUNEL_APPSERVICE_DIR="/etc/tuwunel/appservices"`).
3. Configure MindRoom with `MATRIX_MANAGED_ACCOUNT_AUTH=appservice` and `MATRIX_APPSERVICE_TOKEN` (or `MATRIX_APPSERVICE_TOKEN_FILE`) matching `as_token`, and leave `MATRIX_REGISTRATION_TOKEN` unset.
4. Disable password login on Tuwunel:

    ```toml
    [global.authentication.login]
    with_password = false
    ```

    Or via environment variable: `TUWUNEL_AUTHENTICATION__LOGIN__WITH_PASSWORD="false"`.

On Kubernetes, see [Passwordless MindRoom Accounts](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/tuwunel/README.md#passwordless-mindroom-accounts) and [Matrix Managed Account Authentication](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#matrix-managed-account-authentication).

## MindRoom Chat

MindRoom Chat supports custom OIDC providers out of the box on web, iOS, and Android through Matrix's `m.login.sso` flow (including SSO interactive authentication for sensitive actions).
Every custom provider Tuwunel advertises on `/_matrix/client/v3/login` appears automatically as a **Continue with `<name>`** button (custom `mxc://` `icon` images display only if the homeserver serves unauthenticated media, which Tuwunel disables by default; Apple, Google, and GitHub use MindRoom Chat's bundled marks, and Apple uses **Sign in with Apple**).

Optional settings in MindRoom Chat's `config.json` (`client-config.json` in `mindroom-stack`):

```json
{
  "defaultHomeserver": 0,
  "homeserverList": ["https://matrix.example.com"],
  "allowCustomHomeservers": false,
  "auth": {
    "hideServerPickerWhenSingle": true,
    "allowRegistration": false,
    "disablePasswordLogin": true
  }
}
```

- `auth.disablePasswordLogin: true` hides the password form when password login is off.
- `auth.allowRegistration: false` hides the Register link when accounts are created on first SSO sign-in.
- Listing your homeserver in `homeserverList` (or setting `allowCustomHomeservers: false`) avoids the unlisted-server trust prompt before SSO redirects.

See [MindRoom Chat sign-in configuration](https://github.com/mindroom-ai/mindroom-chat/blob/main/docs/configuration.md#sign-in) for all client options.

## Next-Gen OIDC Clients (Element X) & Reverse Proxy

Once `[global.well_known].client` and at least one `[[global.identity_provider]]` are configured, Tuwunel also activates its built-in OIDC authorization server (MSC3861 / MSC2965) for clients such as Element X.
Your reverse proxy must forward all of these paths to Tuwunel (`:8008`):

| Path | Used by |
| --- | --- |
| `/_matrix/*` | Matrix client, SSO redirect/callback, and federation APIs |
| `/_tuwunel/oidc/*`, `/_tuwunel/sso/*` (or `/_tuwunel/*`) | Tuwunel's OIDC endpoints and SSO interactive-auth assets (`sso.css`, `complete.js`) |
| `/.well-known/openid-configuration` | OIDC discovery document |
| `/.well-known/matrix/client`, `/.well-known/matrix/server` | Matrix client and server discovery (unless served separately) |

Additional notes:

- To allow both local password accounts and your OIDC provider on Tuwunel's built-in OIDC login page, set `[global.authentication.oidc].native_auth = true` (`TUWUNEL_AUTHENTICATION__OIDC__NATIVE_AUTH="true"`).
- If you restrict dynamic client registration with `authentication.oidc.registration_allowed_redirect_hosts`, include the exact redirect hosts or private-use schemes your clients use (for example `["element.io", "io.element.android"]` for Element X on iOS and Android; hosts match exactly rather than by subdomain, so list any web client's exact host too).
- When MindRoom Chat and Tuwunel share one origin, exclude `/_tuwunel` from Chat's service-worker navigation fallback so `/_tuwunel/oidc/*` pages reach Tuwunel instead of the cached SPA (see [Service Worker](https://docs.mindroom.chat/deployment/cloudflare-access/#service-worker)).
- See Tuwunel's [OIDC Authorization Server guide](https://github.com/mindroom-ai/mindroom-tuwunel/blob/main/docs/authentication/oidc-server.md) for token lifetimes, PKCE, and client-approval settings.

## MindRoom Dashboard & API

The MindRoom dashboard and API (port `8765`) do not run a direct OIDC login flow.
Choose the dashboard authentication boundary that matches your deployment:

- **API key + optional Chat sign-in:** Keep `MINDROOM_API_KEY` set whenever no upstream auth proxy fronts MindRoom; unsetting it leaves the dashboard in open standalone mode instead of SSO-only mode.
  With `TUWUNEL_MINDROOM_OPENID_AUDIENCE=true` (default in `mindroom-stack`), `MINDROOM_PUBLIC_URL` set to the MindRoom portal origin (`CLIENT_MINDROOM_URL` in `mindroom-stack`), and any public self-hosted Chat origin listed in `MINDROOM_CONNECTIONS_ALLOWED_ORIGINS`, users signed in to MindRoom Chat via OIDC who have access to at least one agent or its credentials (other than `access.current_room_members`, which does not apply in a browser) can open `/connections` using Matrix OpenID tokens, and accounts in `administrators` can also reach dashboard routes from that session (see [Connections Portal](https://docs.mindroom.chat/connections-portal/#sessions-and-dashboard-access)).
- **OIDC auth proxy:** Place an OIDC-capable reverse proxy that attaches a raw signed JWT header without a `Bearer ` prefix (such as `oauth2-proxy` backed by Pocket ID, or [Cloudflare Access](https://docs.mindroom.chat/deployment/cloudflare-access/)) in front of MindRoom and enable [Trusted Upstream Browser Auth](https://docs.mindroom.chat/deployment/trusted-upstream-auth/) (`MINDROOM_TRUSTED_UPSTREAM_*`).
  Unless `MINDROOM_CONNECTIONS_AGENT` (a private agent with `private.per: user` or `user_agent`) is set, every user admitted by the proxy gets dashboard operator access, so either restrict the proxy policy to operators or set `MINDROOM_CONNECTIONS_AGENT` to limit the dashboard to `administrators` (see [Security Boundary](https://docs.mindroom.chat/deployment/trusted-upstream-auth/#security-boundary)).
