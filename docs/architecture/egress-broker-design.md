---
icon: lucide/scale
---

# Egress Broker Design Decisions

This page records the decisions behind the [Agent Keys](../deployment/egress-broker.md) feature (the egress broker) that the code does not explain on its own, and why each was made.
How to configure and run the broker is in the deployment guide; the trust model is in the [Security Model](security-posture.md).

The guarantee everything below serves: worker code can use a credential through the broker but can never read it.

## Architecture

**Native broker in the primary process, not Agent Vault, mitmproxy, or Envoy.**
The broker needs the credential store, the encryption key, the config, worker identity, and the existing Connections and dashboard UIs, which all live in the primary.
Keeping Agent Vault meant a second service, a second credential store, and its own accounts and roles; mitmproxy brings a heavy, strictly pinned dependency tree and a separate process that would fetch secrets over an API.
The cost is that MindRoom owns a TLS-intercepting proxy: TLS, HTTP parsing, and certificates are delegated to `ssl`, `h11`, and `cryptography`, and the roughly 1,700 lines of transport glue are hardened with a request-smuggling corpus, property-based fuzzing, and dial-guard cases.
A human penetration test is still recommended before wide exposure.

**Own thread and event loop.**
Large brokered transfers, such as a git clone through a matched host, must not stall the bot's event loop.

**TLS is intercepted only for hosts that have rules; every other host is tunneled blind.**
Intercepting everything breaks clients that pin certificates and means decrypting traffic the broker has no reason to see.
Blind tunnels keep the upstream's real certificate, so workers get a combined trust bundle (system roots plus the broker CA) instead of the broker CA alone.
The broker CA is trusted only inside the worker's execution environment, never system-wide on the host.

## Where a secret may go

**The CONNECT target alone decides which credential is used.**
The TLS server name and the `Host` header must match it, plain `http://` to a matched host is refused, and redirects are returned to the client instead of followed.
Any of those would otherwise let a request carry one host's secret to another host.

**A request only gets the credential of the service definition that routed it.**
A service changed while the request waited for its secret, by a user-service save or a config change, may hold a key or account meant for other hosts, so the request gets a retryable 502 instead, and worker code cannot catch a key or account in the moment a service changes.

**Ambiguous paths are refused, not normalized.**
Path limits only hold if the broker and the upstream agree on what a path means.
Dot segments at any decoding layer (including Unicode look-alikes), NUL, invalid UTF-8, raw `//`, and `\` are refused because servers normalize them differently.

**Requests are rebuilt from what `h11` parsed, never copied byte for byte.**
This stops bytes that another parser would read as a second request from reaching an upstream.
Bodies are streamed rather than buffered, because buffering every body would cost memory and latency.

**`TRACE` and `TRACK` are refused on hosts with rules.**
They ask the server to echo the request, which would return the injected credential.

**A user service's connected account only goes to its provider's hosts.**
An OAuth token only works at its provider, so sending a GitHub or Google token anywhere else can only help someone who tricked the user into it.
The allowed hosts come from the presets bound to each provider.

**Every party's path limit must pass.**
An operator's `restrict_to_rules` and a user's limit both apply to a request; a user can narrow the operator's services for their own scope but never widen them, and a `restrict_only` limit never selects a credential.
This lets users restrict what an agent may do with an operator's key without gaining anything the operator did not grant.

## Secrets and identity

**Secrets are write-only.**
No API, page, log, audit record, refusal, or agent tool ever returns a stored key or token.
Anyone who could read a value back, including an agent with access to some API, would break the guarantee; credential managers can set and replace keys but not read them.

**Worker tokens are stateless HMAC tokens minted per call, valid for 7 days by default.**
There is no session table to store or clean up, long-running background commands keep working, and a leaked token stops working on its own.
The trade-off is that a single token cannot be revoked, only all of them at once; see [Resetting broker state](../deployment/egress-broker.md#resetting-broker-state).

**The proxy password is the fixed text `mindroom`.**
Python `urllib` and Node `fetch` only send `Proxy-Authorization` when both the user name (the token) and the password are non-empty.
The password is not a secret.

**Agents opt in with the `egress_credentials` tool, not by having a shell.**
Services with an `oauth_provider` reuse the scope's native OAuth connections with their full scopes, so if every `shell` or `python` agent used the broker, an agent whose native tools are read-only or ask for approval would get full access from its commands as soon as an account was connected.
The tool already tells the agent about the broker, so listing it is the opt-in instead of a separate config field, and one rule decides for the sandbox proxy, the pages, and the request log, so they cannot disagree.
A call without a configured agent gets no broker environment, because nothing shows that it opted in.

**A connected account is used only after an explicit, per-service opt-in that states the approval bypass.**
The tool opt-in says an agent may use Agent Keys, not that a user wants a connected account to work without the approvals of its native tools.
Without a second opt-in, connecting a tool whose writes ask for approval silently gave the agent's commands the same account with its full scopes.
So the broker reads a connection for a service only while an opt-in names that service and the connection's provider, and enabling one requires acknowledging that commands skip the tools' approvals.
The opt-in lives in the store of the connection it unlocks, and the broker and both APIs resolve that store through one function, so an opt-in cannot unlock another requester's or scope's account; a round-trip test per scope kind guards this.
Requester-scoped providers get one opt-in per requester and service name, covering all of that requester's agents, because the account itself is the requester's.
It names the provider, not the account: reconnecting another account of the same provider keeps it, while a changed `oauth_provider` turns it off.
Rejected: keeping connections already in use working after the upgrade, because that would silently keep the bypass this removes.

**Requester-scoped accounts (GitHub, Atlassian) are used only in a requester's own sandbox.**
In a shared sandbox, every requester's commands run together, so one user's code could act with another's account.
A shared sandbox can opt in with `oauth_on_shared_workers`, which only administrators can set.
Even a personal worker shares the workspace folder on a non-private agent, so personal accounts and keys are only isolated on `private` agents.

**A shared agent has one Google account, confirmed and visible to everyone who uses it.**
Everyone acts as that account, so connecting it asks for confirmation and every user sees which account it is.

**Configuration is trusted.**
Whoever can write the `egress_broker` config, including agents with `config_manager`, can point a service at another host and so send its key there.
User services can never take a config service's name, and a new one never inherits a key stored under its name, so users cannot do the same.
An entry saved while no config service had its name is ignored once one does, and becomes active again with the key stored under that name when the config service is removed; under the trust model below, the entry and the key both come from people who may change each other's keys.

**Credential managers of the same key scope trust each other.**
A shared agent's credential managers and the administrators act for one team.
Unscoped agents share one key store, so the managers of every unscoped agent form one group.
They can set, replace, and redirect each other's keys and services, for example by pointing a user service with a key at another host.
The boundary is elsewhere: agents and worker code never read a stored key, and users who are not credential managers or administrators cannot change keys or services.
Protecting managers from each other cost recurring complexity, such as deleting a key when its service's rules changed or an entry when a config service's key was saved, for a threat the deployment does not have.
Keys stay write-only in the UI for simplicity, not as a boundary between managers.

## Visibility

**Refusals carry a message the agent can relay, in several forms.**
Different clients surface different parts of a response: JSON clients read the body, Python `urllib` shows the reason phrase, and git prints only a pkt-line `ERR`.
Every form names who can fix the problem and where, and never includes a secret, token, query string, or connect link.

**The request log stores metadata only.**
Method, host, path without its query string, status, and refusal code are kept for 30 days.
Users see their own requests; on shared or unscoped agents, credential managers and administrators see everyone's, because they are responsible for that agent's keys.
