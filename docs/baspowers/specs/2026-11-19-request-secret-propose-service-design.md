# `request_secret` proposes a new Agent Keys service

Status: design decided by Bas on 2026-11-19 (brief `request-secret-propose-service.md`).
Paired branches: `mindroom-ai/mindroom` `feat/request-secret-propose-service` (backend) and `mindroom-ai/mindroom-chat` `feat/secret-card-propose-service` (card).
This file is the event contract for both.

## Problem

`egress_credentials.request_secret` asks only for a key of a service that already exists in `config.yaml` or on the personal page.
While dogfooding, the agent hand-edited `config.yaml` three times to define services (for example a GitHub service for one organization).
Top-level config edits are risky: they restart running minimal subagents, race other edits, and the user never saw which hosts a key goes to until the card.

## Decision

The agent proposes the service on the same card, and the user's Save creates the service and stores the key in one step.

- `request_secret` takes one new optional argument, `new_service`: a JSON object with the fields of a user service as the personal page saves it (`display_name`, `description`, `preset`, `rules`, `placeholder_env`, `restrict_to_rules`); `service` is then the new service's name.
- A proposed service is key-only by construction: `oauth_provider` is forced to null (a preset such as `github` carries one), and every other field is refused, including `agents`, `admin_key`, `github_app`, `oauth_on_shared_workers`, `restrict_only`, `oauth_separate_account`, `oauth_provider` and `read_only`.
- Who may answer is unchanged: the requester who may set this agent's keys (credential managers and administrators on shared agents). No new authority.
- No key-validity check (Bas rejected it as overengineering). The docstring tells the agent to make one request through Agent Keys after `saved` and, when it is refused, tell the user why and ask again.

Rejected: account login on the card, editing or deleting services from chat, a GitHub `owner` convenience parameter, a client version handshake.

## Refusals (before any card is posted, and again on Save)

The tool returns the existing `error` shape and posts no card when the proposal fails any check the personal page applies, reused from `egress_broker/user_services.py`:

1. A field outside the list above.
2. The name: invalid (`validate_egress_service_name`), a config service's (`UserServiceConflictError`), or an entry the scope already stores, inactive ones included.
3. The service model's own validators (`EgressService`), and the user-service caps (`_check_user_service`: rules, size).
4. Placeholder rules (`_placeholder_error`) and OAuth provider rules (`_oauth_provider_error`, a no-op once the provider is null).
5. The scope caps (service count, document size).
6. Hosts `unmatched_hosts: deny` keeps closed (`user_service_hosts_not_allowed`).

On Save the API re-runs the same checks against the config it serves now; any refusal there ends the request as `expired` (404), like a service that changed under an existing card.

## Save

1. The existing checks: same key store as the card's, requester may still manage keys.
2. The key is validated first, so a refused value (422) creates nothing and the request stays pending.
3. `save_user_service(..., only_new=True)` adds the service; a name taken meanwhile is `expired`.
4. Under `consistent_read`, the service must still have the card's hosts; then the key is saved.
5. When saving the key raises, the new service is deleted again (`delete_user_service`), so no keyless half-state remains.

Decline and expiry create nothing.

## Card content (`io.mindroom.secret_request`)

A key card for a proposed service carries, besides today's fields (`hosts` unchanged), a `new_service` object, metadata only and never a value:

```json
"new_service": {
  "name": "github-acme",
  "display_name": "GitHub (acme)",
  "rules": [
    {"host": "api.github.com", "path_prefix": "/repos/acme/", "auth": {"type": "bearer"}},
    {"host": "git.example.test", "path_prefix": "/", "port": 8443, "auth": {"type": "header", "name": "X-Api-Key"}},
    {"host": "api.example.test", "path_prefix": "/", "auth": {"type": "basic", "username": "x-access-token"}}
  ],
  "placeholder_env": {"GITHUB_TOKEN": "mindroom-brokered"}
}
```

- `port` is present only when the rule names one; `auth.name` only for `header` and `query`, `auth.username` only for `basic`.
- Never a template's text or a key.

Chat parses `new_service` strictly.
A card whose `new_service` is present but malformed shows no key field and no Save.
A valid one shows a compact "New service" block above the key field: the display name, one line per rule (`host[:port]path_prefix · auth`), and the variable names.

## Known limits

- An older Chat without this change shows the usual card (the hosts are listed) and its Save also creates the service. Accepted; the hosts are shown.
- Between the card and Save the user can create a service of the same name on the personal page; the answer then expires and the agent asks again.

## Checklist

- [ ] Spec committed.
- [ ] Backend tests red: tool refusals without posting, `new_service` on the card, Save creates service and key, config gained the name expires, key-save failure removes the service, decline creates nothing.
- [ ] Backend: `user_services` (`only_new`, `check_new_user_service`, `user_service_hosts_error`), `secret_requests` (`KeyTarget.new_service`, content), tool, API.
- [ ] Docs: `docs/deployment/egress-broker.md` "Asking in chat"; regenerated tool schema goldens and skill references.
- [ ] Chat tests red: parser accepts/rejects, preview renders, malformed `new_service` offers no Save.
- [ ] Chat: parser, card block, i18n in all 17 locale overlays.
