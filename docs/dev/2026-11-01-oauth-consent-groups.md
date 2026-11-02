# OAuth consent groups: one sign-in for services that share an account

This record holds the decisions behind connecting several OAuth services of one agent with a single provider sign-in on the Connections portal.
It records the goal, reasons, external constraints, rejected alternatives, and accepted limitations; the code shows the mechanisms.

## Goal

On the Connections portal, an agent with Google Calendar, Gmail, Sheets, Docs, Tasks, and Drive tools lists one **Connect** button per service, and each opens its own Google sign-in and consent screen.
The request is one button that connects all of them at once, and the same for other providers whose services share one OAuth app.
Success means that an agent with two or more unconnected services of one group shows one **Connect all** action, one sign-in and one consent screen connect every service whose scopes the user granted, and the per-service buttons keep working as before.

Assumptions:

- The action lives on the Connections portal, per agent, because each agent resolves its own credential scope; agents in the `user` scope already share one store, so connecting for one covers the others.
- Only services that are not connected join the sign-in, so the action never replaces an account the user already connected.
- The dashboard Integrations cards, connect links that agents post in conversations, Agent Keys' own-account connections, and reset links keep their per-service flows.

## Decisions

- **Providers opt in with a consent group.**
  `OAuthProvider` gains an optional consent group with a key and a display name.
  The built-in Google providers, Google Cloud included, form the `google` group; each [additional Google workspace](../plugins.md#additional-google-workspaces) forms its own group named after the workspace, so its account picker and hosted-domain rule stay separate from the built-in connection; the Microsoft 365 providers form the `microsoft` group, except Teams, Teams Meetings, and People, whose `ChannelMessage.Read.All`, `OnlineMeetingTranscript.Read.All`, and `User.Read.All` scopes need admin consent.
  Plugin providers may declare a group too.
  Grouping is never inferred from a shared OAuth client, because copying one refresh token into several connections is safe only for providers whose old refresh tokens stay valid after use (see External constraints).
- **The server picks the members.**
  A group connect for an agent takes the agent's services in that group that the requester may connect, that are not connected or awaiting reset, that use no service account, and that resolve to the same OAuth client ID as the first such service.
  The browser names only the agent and the group, so a stale page cannot replace a connected account.
- **One authorization request.**
  The first member in the agent's service order leads: its endpoints, redirect URI, PKCE method, and extra authorization parameters build the request, which asks for the union of every member's scopes in order without duplicates.
  The callback is the lead's `/api/oauth/<provider-id>/callback`, which custom OAuth clients already register for each provider.
  The pending state records each member and the connection generation it had when the flow started.
- **One code exchange, one store per member.**
  The callback exchanges the code once at the token endpoint bound when the flow started, then stores the token for each member in that member's own credential transaction.
  A member is skipped, and reported on the result page, when its connection changed since the flow started, when the account fails its allowed-domain rules, or when the grant lacks one of its required scopes, which happens when a user unticks a service on Google's granular consent screen.
  Each stored copy names its own provider, so status, refresh, disconnect, and reset stay per service.
  When every member is skipped, the result page is an error.
  A result page that names a skipped member stays open for the user to close, because the portal shows only which services connected, not why the others did not.
- **Authorization matches the per-service flow.**
  A group connect requires the same management authority and same-origin request as connecting each member, and a shared agent shows its shared-account confirmation once for the whole group.
- **Interface.**
  `POST /api/connections/agents/{agent_name}/consent-groups/{group}/connect` returns the same response as a per-service connect, naming the lead provider so the existing popup flow matches its completion message.
  The agent listing reports each service's consent group, and the portal shows **Connect all <group> services** above the agent's tools when two or more members are connectable and not connected, then reloads every status when the popup finishes.

## External constraints

- Google does not rotate refresh tokens, and lets users grant only part of a multi-scope request ("users may not grant your app access to all of them"), so granted scopes are checked per member after the exchange.
- Microsoft Entra issues a new refresh token on every use but "doesn't revoke old refresh tokens when used to fetch new access tokens" ([Refresh tokens in the Microsoft identity platform](https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens)), so each copy refreshes independently.
- MindRoom never revokes tokens at the provider when a connection is disconnected, so disconnecting one member leaves the other copies working.
- Microsoft Entra refuses a whole request when one scope needs admin consent the tenant has not given, which is why the admin-consent providers stay out of the `microsoft` group.

## Security

- Every copy from a group sign-in carries every scope of that sign-in.
  Separately connected Drive, Gmail, Calendar, and Sheets tokens already carry scopes the account granted earlier, through `include_granted_scopes`, and a group sign-in led by one of them does the same; Docs, Tasks, and Google Cloud tokens, which opt out of that today, become as broad when connected in a group.
  The difference reaches agent code only through an Agent Keys allowance for one of those services, which then acts with the whole grant on the hosts that service allows; users who want narrow tokens keep connecting services one by one.
  `docs/architecture/security-posture.md` records this beside the Agent Keys entries.
- Tokens stay in the primary runtime as before; no worker sees them.

## Accepted limitations

- When the provider refuses the whole request because of one member, such as a Google scope the OAuth app is not verified for, the sign-in connects nothing; that member's own **Connect** button fails the same way, and the others still connect one by one.

## Rejected alternatives

- **Merge the Google providers into one provider**: every agent would request every Google scope even for one tool, per-service consent and its verification limits would go away, and stored tokens, egress opt-ins, and workspace providers would need a migration.
- **Open the per-service flows one after another from the browser**: each flow still shows its own consent screen because Google providers force `prompt=consent`, and browsers block every popup after the first that a click did not open.
- **Group providers automatically by shared OAuth client**: a provider whose refresh tokens are single use would break when the copies refresh independently.
- **Store one group token that members read**: avoids copies, but changes every credential lookup, generation check, reset, and Agent Keys opt-in owner for a saving users cannot see.
- **One button across all agents**: agents resolve different credential scopes, so one sign-in would have to fan out across stores with different managers and confirmation rules.

## Documentation

- `docs/connections-portal.md` gains the **Connect all** action and what happens to services the user does not grant.
- `docs/deployment/google-services-oauth.md` replaces "users connect and approve each service separately" with both ways to connect.
- `docs/oauth-framework.md` lists the consent group among the provider fields plugin authors set.

## Testing

- Backend, at the connections API and OAuth callback seams: one authorization URL with the union of scopes and the lead's callback, members chosen by management, connection, service account, and client; every member stored on success; members skipped for a missing scope, a rejected domain, and a changed connection; an all-skipped sign-in fails; a shared agent's group connect follows the shared-connection rules.
- Frontend: the action shows only for two or more connectable unconnected members, starts the group connect, and reloads statuses.
