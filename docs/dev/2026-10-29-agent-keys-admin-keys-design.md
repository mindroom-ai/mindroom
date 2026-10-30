# Administrator Keys and Agent Lists for Agent Keys Services

This record explains how an administrator gives agents one API key that users can see is set but can never read, replace, or remove, and how a service is limited to some agents.

## Why

An administrator wanted to give everyone access to one API, such as a paid search or data API, through Agent Keys without each user pasting a key of their own.
Keys were already write-only for everyone, so hiding the value needed nothing new.
What was missing was reach: an administrator could set keys only for `shared` agents and for agents without a worker scope, while `user` and `user_agent` agents read only the requester's own store.
The administrator also wanted to limit such a key to some agents instead of every agent with `egress_credentials`.

## Decisions

- **`admin_key: true` on a config service means one key, set only by an administrator.**
  The key lives in the global store under a name of its own that no scope's key can take, and every scope reads it there for that service instead of its own store, so the key that agents without a worker scope share never becomes the administrator's when `admin_key` is turned on.
  The dashboard's key routes write it whichever scope is selected; the Connections page's key routes refuse it with 403 for everyone, credential managers and administrators included, so changing it stays a dashboard action.
  The service takes no connected account, so a preset's `oauth_provider` and `read_only` are dropped like a `restrict_only` service drops them, and authoring either next to `admin_key` is an error.
- **`agents: [...]` on a config service limits which agents get its credential.**
  The broker still routes the service's requests for every agent, so path limits and `unmatched_hosts` behave as before, and a request from another agent is refused with 403 `agent_not_allowed` instead of being sent without a key.
  The Connections page, the `egress_credentials` tool, and placeholder variables leave the service out for other agents.
  The dashboard lists every config service in every scope with the agents it is limited to, because an administrator must reach an `admin_key` service even when no listed agent has a scope the dashboard can select.
  The agent is the token's routing agent, the same agent whose tools decide that a call uses Agent Keys at all.
- **Both fields are config-only and independent.**
  User services refuse them, so a user cannot widen who gets a key, and `agents` also works on a service with per-scope keys.
- **Users see that the key is set.**
  Hiding the row would show no key while requests succeed, and the agent tool lists the service anyway.

## Rejected

- **A deployment-wide fallback key that a scope's own key overrides.** Users could then change which key their agent uses, and every row needed precedence rules between two keys.
- **Reading the key from an environment variable named in `config.yaml` (`secret_env`).** Anyone who can edit the config, `config_manager` agents included, could name any variable of the primary, such as `MINDROOM_API_KEY`, and point the service at their own host.
  Credential seeds already fill the global store from the environment.
- **Falling back from personal scopes to the store of agents without a scope.** Credential managers of any unscoped agent could then set keys for every user's agents, and existing deployments would change behavior without a config edit.
- **Leaving the service out of routing for agents it does not list.** Every place that reads the operator's config for a request, such as `unmatched_hosts: deny` and `restrict_to_rules`, would need a per-agent config, and a request would go out without a key under `passthrough` with an upstream error instead of a refusal that says why.

## Limitations

- An agent list decides which agents get the key, not which code can reach it, where agents share a sandbox: on the static runner every agent's calls share one process, and a requester's `user` agents share one worker, so code there can use another call's proxy token until it expires.
- A user's own service on the hosts of a service limited to other agents loses the tie to it, so an unlisted agent's request uses it only when it names it with `X-Agent-Keys-Service`; this follows from refusing instead of leaving the service out of routing.
- Anyone who can edit the config can still point an `admin_key` service at another host and receive the key, as with every config service.
- Every user of a listed agent spends the administrator's key; there is no per-user quota, and the request log records who made each call.
