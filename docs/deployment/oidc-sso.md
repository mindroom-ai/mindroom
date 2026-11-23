---
icon: lucide/key-round
---

# OIDC & Single Sign-On

Self-hosted deployments running MindRoom's Tuwunel fork (`mindroom-tuwunel`) can delegate Matrix sign-in to any standard OpenID Connect (OIDC) identity provider, including Pocket ID, Authelia, Authentik, and Keycloak.
Tuwunel exposes the same configured identity provider to `m.login.sso` clients (such as MindRoom Chat) and to next-generation Matrix OIDC clients (such as Element X) through its built-in OIDC authorization server.

This page covers configuring Tuwunel, wiring Docker Compose (`mindroom-stack`) or Kubernetes, running an SSO-only homeserver without breaking MindRoom's agent accounts, configuring MindRoom Chat, routing a reverse proxy, and authenticating to the MindRoom dashboard.
Hosted `mindroom.chat` uses MindRoom's own configured SSO providers; bringing your own identity provider applies to self-hosted homeservers.

## Identity Provider Requirements

Create a **confidential** OAuth 2.0 / OIDC client in your identity provider:

- **Client authentication:** Confidential client with a `client_secret` (Tuwunel requires `client_secret` or `client_secret_file`; public clients without a secret are not supported).
- **Grant and flow:** Authorization Code flow (`response_type=code`) with PKCE (`S256`) and `client_secret_post`.
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

=== "Kubernetes (Helm)"

    The [Tuwunel chart](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/tuwunel/README.md#oidc-login) renders `[[global.identity_provider]]` and mounts the client secret from a Kubernetes Secret:

    ```yaml
    tuwunel:
      serverName: example.com
      clientBaseUrl: https://matrix.example.com
      oidc:
        enabled: true
        brand: PocketID
        issuer: https://id.example.com
        clientId: <client_id>
        clientSecret:
          existingSecret: matrix-oidc
          key: OIDC_CLIENT_SECRET
        scope: [openid, profile, email]
        extraConfig: |
          name = "Pocket ID"
    ```

### Matching Existing Matrix Accounts

By default, Tuwunel creates a new Matrix account on a user's first SSO sign-in if the claimed username already belongs to an existing account.
To attach SSO logins to existing accounts on a self-hosted provider you control:

- Set `trusted = true` (with `userid_claims`, such as `["preferred_username"]` or `["sub"]`, matching your Matrix localparts) so Tuwunel signs the user into the matching existing account.
  Never set `trusted = true` on public providers such as Google or GitHub, because it enables account takeover.
- Or map specific addresses explicitly with `assigned_accounts = { "alice@example.com" = "alice" }`.

See the `mindroom-tuwunel` [Identity Providers guide](https://github.com/mindroom-ai/mindroom-tuwunel/blob/main/docs/authentication/providers.md) for the full option reference, registration rules, and provider-specific guides for Authelia, Authentik, Keycloak, and MAS.

## Disabling Password Login (SSO-Only)

By default, MindRoom registers and logs in its agent, team, and router accounts with passwords (`MATRIX_MANAGED_ACCOUNT_AUTH=password`).
Turning off password login on Tuwunel (`authentication.login.with_password = false` or `TUWUNEL_AUTHENTICATION__LOGIN__WITH_PASSWORD="false"`) disables password registration and login for every account, which stops default MindRoom agent startup.

To run an SSO-only homeserver, switch MindRoom to [`appservice` mode](../matrix.md#agent-users):

1. Create an application-service registration file (for example `mindroom-appservice.yaml`) whose exclusive `users` namespace matches every MindRoom-managed account, including any custom `matrix_username` or `mindroom_user.username`:

    ```yaml
    id: mindroom
    url: null
    as_token: <long-random-secret>
    hs_token: <another-long-random-secret>
    sender_localpart: mindroom_appservice
    namespaces:
      users:
        - exclusive: true
          regex: "^@mindroom_.*:example\\.com$"
    ```

2. Point Tuwunel at the directory holding that `.yaml` file via `appservice_dir = "/etc/tuwunel/appservices"` (`TUWUNEL_APPSERVICE_DIR="/etc/tuwunel/appservices"`).
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
Every provider Tuwunel advertises on `/_matrix/client/v3/login` appears automatically as a **Continue with `<name>`** button, along with its `mxc://` `icon` if configured in Tuwunel.

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
| `/_tuwunel/oidc/*` | Tuwunel's OIDC authorize, token, registration, and account endpoints |
| `/.well-known/openid-configuration` | OIDC discovery document (RFC 8414) |
| `/.well-known/matrix/client`, `/.well-known/matrix/server` | Matrix client and server discovery (unless served separately) |

Additional notes:

- To allow both local password accounts and your OIDC provider on Tuwunel's built-in OIDC login page, set `[global.authentication.oidc].native_auth = true` (`TUWUNEL_AUTHENTICATION__OIDC__NATIVE_AUTH="true"`).
- If you restrict dynamic client registration with `authentication.oidc.registration_allowed_redirect_hosts`, include the redirect hosts or private-use schemes your clients use (for example `"element.io"` for Element X).
- When MindRoom Chat and Tuwunel share one origin, exclude `/_tuwunel` from Chat's service-worker navigation fallback so `/_tuwunel/oidc/*` pages reach Tuwunel instead of the cached SPA (see [Service Worker](cloudflare-access.md#service-worker)).
- See Tuwunel's [OIDC Authorization Server guide](https://github.com/mindroom-ai/mindroom-tuwunel/blob/main/docs/authentication/oidc-server.md) for token lifetimes, PKCE, and client-approval settings.

## MindRoom Dashboard & API

The MindRoom dashboard and API (port `8765`) do not run a direct OIDC login flow.
Choose the authentication model that matches your deployment:

- **Sign in from MindRoom Chat:** With `TUWUNEL_MINDROOM_OPENID_AUDIENCE=true` (enabled by default in `mindroom-stack`), users signed in to MindRoom Chat via OIDC can open `/connections` directly using Matrix OpenID tokens, and accounts listed in `administrators` can reach dashboard routes from that session (see [Connections Portal](../connections-portal.md#sessions-and-dashboard-access)).
- **OIDC auth proxy:** Place an OIDC-capable reverse proxy that attaches a signed JWT (such as `oauth2-proxy` backed by Pocket ID, or [Cloudflare Access](cloudflare-access.md)) in front of MindRoom and enable [Trusted Upstream Browser Auth](trusted-upstream-auth.md) (`MINDROOM_TRUSTED_UPSTREAM_*`).
- **API key:** Keep `MINDROOM_API_KEY` for single-operator setups.
