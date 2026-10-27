# Separate Accounts for Agent Keys Services

This record explains how an agent's shell and python can use a second account of an OAuth provider through Agent Keys, by giving a service of the user's own its own login, separate from the connection the agent's tools use.

## Why

Agent Keys hands worker requests the scope's one connection per provider, the same one the agent's tools use.
Agent Vault, which it replaced, let a vault hold several logins, such as a user's own Google account and a second Google account for a shared mailbox or another calendar.
Nothing in Agent Keys can express that, so a migration had to drop those logins or map them onto the tool connection, which belongs to a different account.

A first design added labelled accounts beside every provider connection, with their own listing, connect, disconnect, opt-in keys and dashboard section.
It needed several times the code it was estimated at, because each account became a second kind of thing to manage.
This design reuses the one thing Agent Keys already manages per row, a service: a second account is a service of the user's own that has its own login.

## Decisions

- **A service of the user's own can have its own login.** `EgressService` gains `oauth_separate_account: StrictBool = False`.
  It is valid only together with `oauth_provider`, and only on user services of personal (`user`, `user_agent`) scopes, the same places a user service may name a provider today; a config service or a shared scope refuses it, and so does a requester-scoped provider (GitHub, Atlassian) for now.
  Such a service's connection is stored in the scope's store under its own credential service, `f"{base}__{service}_oauth"`, where `base` is the provider's `credential_service` without its `_oauth` suffix (for example `google_calendar__team_calendar_oauth` for a service `team_calendar`).
  The provider with that one field replaced (`dataclasses.replace`) is the connection everywhere: the connect flow, the callback, refresh and the credential lifecycle already work per credential service.
  The agent's tools keep using the provider's own connection and never see a separate one.
- **The existing service row manages it.** The Agent Keys connect, disconnect and opt-in routes for that service, and its status, resolve the separate connection instead of the provider's own; there are no new routes or dashboard sections.
  The opt-in entry stays keyed by the service name, with the same fields (`provider`, `access`, `enabled_by`, `enabled_at`) plus `credential_service`, the connection it unlocks.
  An entry counts as off while it names another connection than the one the service uses now, and an entry without `credential_service` names the provider's own, so ticking or unticking the box never carries an opt-in to the other account, not even for a request routed while the save lands.
  Until it is connected, the broker's refusal asks the user to connect the service's own account at `manage_url`, with no connect link, since the provider's link connects the tools' account.
  Deleting the service, unticking the box, or changing its `oauth_provider` removes its own connection and its opt-in, so no refresh token stays behind and ticking the box again starts unconnected.
  Saving a service with the box ticked that did not already have its own account of that provider also clears any left under its name first, such as by a delete while a config service shadowed the name, so an old account never comes back.
- **A request picks such a service with a header.** Routing is unchanged without it, so a user service that ties with an operator's service on host and path is never chosen by accident.
  A request may add `X-Agent-Keys-Service: <name>`; routing then matches only that service's rules, after the same restriction checks as today, and a request no rule of that service matches is refused with 403 `service_not_matched`.
  A header sent twice, or whose value is not a valid service name (empty included), is a 400 `bad_request` whose message names the header and never repeats the value. The broker removes the header before forwarding.
  This holds on hosts with rules, the only ones whose HTTPS the broker reads: a request to another host is tunneled blind, so the header selects nothing there and reaches the host as sent, which exposes only a service name the agent itself chose.
  The header names one of the requester's own (user) services, separate-account or not, so an agent can also pick a user service with its own API key the same way.
  An operator's service cannot be named: the most specific operator rule keeps choosing among the operator's credentials, and an agent cannot override it.
- **The agent learns which services exist.** `list_egress_credentials` marks services with `separate_account: true`, and its guidance names the header.
- **The editor offers it.** When a user's own service names an OAuth provider, the service editor shows one checkbox, "Use its own account, not the one the agent's tools use", with a line saying requests choose it with `X-Agent-Keys-Service: <name>`.

## Later, not now

- Separate accounts for config services and shared agents.
- Separate accounts of requester-scoped providers (GitHub, Atlassian): their connections follow the requester across agents while a `user_agent` scope's services are per agent, so two agents' services of one name would share one account.
- Permissions the provider does not request (for example a Google admin API), which still need a provider of their own.
- Choosing a service by path instead of a header, for tools that cannot set headers.

## Assumptions

- Every place that reads or writes a connection resolves its store through the provider's `credential_service`; the refresh backoff is keyed by it, not by the provider id.
- Squid hands the broker every credentialed request with its headers intact.

## Testing

- Config: `oauth_separate_account` needs `oauth_provider`, is refused on config services and shared scopes, and names `f"{base}__{service}_oauth"`.
- Connect and disconnect through the service's Agent Keys routes store and remove only the separate connection; the provider's own connection and the tools are untouched; status shows the separate account's label.
- Broker: no header routes as today; `X-Agent-Keys-Service` picks the named service and its separate connection's token, under the opt-in's access level; a name whose rules do not match is 403 `service_not_matched`; a bad header is 400; the header is never forwarded.
- Deleting the service, unticking the box, or changing its provider removes its own connection and opt-in.
- Tool and editor: the flag and header appear in `list_egress_credentials`, and the checkbox saves `oauth_separate_account`.
