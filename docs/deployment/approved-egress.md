---
icon: lucide/shield-check
---

# Approved Egress

Approved egress blocks direct internet access from dedicated Kubernetes workers and sends their traffic through a policy-enforcing HTTP proxy.
A worker can reach a hostname only when it is on the static allowlist or a human has approved a temporary grant in Matrix.
Use it when worker-routed tools such as `shell` and `python` must not reach arbitrary hosts.

## Enable with the Runtime Chart

```yaml
workers:
  backend: kubernetes
  sandbox:
    proxyToken:
      existingSecret: mindroom-sandbox-proxy
      key: MINDROOM_SANDBOX_PROXY_TOKEN

approvedEgress:
  enabled: true
  image:
    tag: v0.1.10
  allowlist:
    domains:
      - example.com
      - .docs.example.com
  maxTtlSeconds: 21600
```

The chart deploys the proxy, points worker proxy settings at it, and adds NetworkPolicies so workers cannot bypass it.
Approved egress requires `workers.backend: kubernetes`, a pinned `approvedEgress.image.tag` or `approvedEgress.image.digest`, and a token from `approvedEgress.token.existingSecret` or `workers.sandbox.proxyToken`.
Use proxy image `v0.1.10` or later, because browser tools tunnel every connection with `CONNECT` and older images refuse plain-HTTP pages with `TCP_DENIED/403`.
`approvedEgress.maxTtlSeconds` caps every temporary grant and defaults to `21600` (6 hours).

The chart also enables the built-in `approved_egress` toolkit at runtime, including with `config.data` or `config.existingConfigMap`, so you do not edit `config.yaml`.
It appends `approved_egress` to `defaults.tools`, or to the built-in default tools when `defaults.tools` is omitted, and adds a `tool_approval` rule that requires Matrix approval for `request_network_access`.
Agents with `include_default_tools: false` do not get the toolkit.
These runtime additions are not written to `config.yaml` when you save from the dashboard or API.
Set `approvedEgress.manageRuntimeConfig: false` to keep the proxy wiring without these additions, for example when your config assigns `approved_egress` to specific agents.

### Static Allowlist

Set allowlist entries in `approvedEgress.allowlist.domains`, or provide them in `approvedEgress.allowlist.existingConfigMap` under the key named by `approvedEgress.allowlist.key` (default `allowed-domains.txt`); you cannot set both.
A plain hostname matches exactly, and a leading dot such as `.docs.example.com` matches the domain and all its subdomains.
Allowlisted hostnames never need approval.
Changing `approvedEgress.allowlist.domains` restarts the proxy automatically when you apply the chart, but edits to an `existingConfigMap` reach the proxy only after you restart it.
MindRoom keeps the allowlist it started with until its own pod restarts, so until then a removed domain is still reported as allowed and gets no grant; the [runtime chart README](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/runtime/README.md#worker-egress-proxy) names the Deployments to restart.

## Requesting Access

Agents call `request_network_access(hostnames, ttl_minutes, reason)` with every blocked external hostname the task needs, up to 20 per call, and one Matrix approval covers the whole batch.
Hostnames must be exact external DNS names; schemes, ports, paths, wildcards, IP literals, single-label names, `localhost`, cluster-local names, and cloud metadata hostnames are rejected, and one invalid hostname rejects the whole request.
Hostnames already on the static allowlist are skipped, and when every requested hostname is allowlisted the tool replies that no grant is needed without asking for approval.
The granted TTL is the requested `ttl_minutes`, capped at the deployment maximum, and the reply says when the cap applied.

Who a grant covers depends on the agent's effective [worker scope](sandbox-proxy.md#worker-scopes), which comes from `private.per` for private agents, then the agent's `worker_scope`, then `defaults.worker_scope`:

| Effective scope | Grant applies to |
|---|---|
| `user_agent` | The requester's own worker for that agent |
| `shared` or unset | That agent's worker |
| `user` | Not supported; the request fails with `approved egress is not supported for worker_scope=user` |

### Temporary Full Access

The `allow_full_access` tool option defaults to `false`.
Enable it in the tool's settings or in an authored tool entry, on `defaults.tools` or on an individual agent:

```yaml
defaults:
  tools:
    - approved_egress:
        allow_full_access: true
```

Agents can then call `request_network_access(hostnames=["*"], ttl_minutes=5, reason="Install dependencies")`.
The `"*"` must be the only entry.
Full-access requests always require Matrix approval, use the same TTL cap, and apply to the same workers as hostname grants.
They open all public hostnames on ports 80 and 443 through the proxy, while private networks, internal hostnames, and other ports and protocols stay blocked.
The proxy image must support full-access grants; older images reject them.
When a grant expires, new connections need an allowlist match or another grant, but established connections, including HTTPS tunnels, stay open.

## Without the Runtime Chart

Enable the toolkit by setting `MINDROOM_APPROVED_EGRESS_ENABLED=true`, or author the same configuration yourself:

```yaml
defaults:
  tools:
    - approved_egress

tool_approval:
  default: auto_approve
  rules:
    - match: request_network_access
      action: require_approval
```

You can assign `approved_egress` to individual agents instead of `defaults.tools`.
The toolkit reads these environment variables, which the runtime chart sets for you:

| Variable | Purpose | Default |
|---|---|---|
| `MINDROOM_APPROVED_EGRESS_ENABLED` | Add the toolkit and approval rule at runtime, as described above | unset |
| `MINDROOM_APPROVED_EGRESS_API_URL` | Proxy policy API URL; plain `http` is allowed only for loopback or in-cluster service names | `http://mindroom-egress-proxy:8080` |
| `MINDROOM_APPROVED_EGRESS_TOKEN` | Bearer token for the policy API | required |
| `MINDROOM_APPROVED_EGRESS_ALLOWLIST` | Comma-separated static allowlist, used instead of the file when set | unset |
| `MINDROOM_APPROVED_EGRESS_ALLOWLIST_PATH` | Static allowlist file with one entry per line and `#` comments | `/etc/mindroom-egress/allowed-domains.txt` |
| `MINDROOM_APPROVED_EGRESS_MAX_TTL_SECONDS` | Maximum grant TTL | `21600` |

## Agent Vault Chaining

When approved egress and [Agent Vault](sandbox-proxy.md) are used together, the chain must be:

```text
worker -> approved egress (Squid) -> Agent Vault MITM parent -> internet
```

Squid must be first because temporary grants are matched to the worker by its source IP.
If traffic goes through Agent Vault first, Squid sees only the vault's IP, so temporary grants never match while static allowlist entries still work.

Before enabling the Agent Vault server and bootstrap job, create the Secret named by `workers.kubernetes.agentVault.bootstrapSecretName` (default `agent-vault-bootstrap`) with both `AGENT_VAULT_MASTER_PASSWORD` and `AGENT_VAULT_OWNER_PASSWORD`.
Create it in the worker namespace too when that differs from the release namespace.

```yaml
workers:
  backend: kubernetes
  sandbox:
    proxyToken:
      existingSecret: mindroom-sandbox-proxy
      key: MINDROOM_SANDBOX_PROXY_TOKEN
  kubernetes:
    agentVault:
      enabled: true
      cliImage: infisical/agent-vault:<pinned>
      ownerEmail: owner@example.test
      server:
        enabled: true
      bootstrap:
        enabled: true
        kubectlImage: bitnami/kubectl:<pinned>
      workerCaConfigMapName: agent-vault-ca

approvedEgress:
  enabled: true
  image:
    tag: v0.1.10
  parentProxy:
    enabled: true
    host: agent-vault
    port: 14322
```

With `approvedEgress.parentProxy.enabled: true`, workers send Agent Vault traffic to approved egress first.
After the allowlist and grant check, requests carrying the worker's Agent Vault token go on to the vault, which still validates the token and injects credentials, while requests without a token go directly to the internet.

Use `approvedEgress.parentProxy.bypassDomains` (default empty) for destinations such as signed URLs that must skip credential injection:

```yaml
approvedEgress:
  parentProxy:
    enabled: true
    bypassDomains:
      - downloads.example.test
      - .objects.example.test
```

Entries match like allowlist entries: exact hostnames, or a leading dot for a domain and its subdomains.
Use ASCII domain names without schemes, ports, paths, `*` wildcards, or whitespace.
Bypassed destinations still need an allowlist match or grant, and changing `bypassDomains` restarts the proxy automatically.

Do not point Agent Vault tool traffic directly at the chart-managed Agent Vault proxy while approved egress is enabled.
The chart refuses that combination with `approvedEgress with Agent Vault must be squid-first: enable approvedEgress.parentProxy so Squid sees worker source IPs for dynamic grants, or set workers.kubernetes.agentVault.proxyUrl to an external/custom proxy URL explicitly`.
An external or custom `workers.kubernetes.agentVault.proxyUrl` remains allowed.

## Secure Minimum

Beyond the chart's required settings:

- Keep `workers.kubernetes.networkPolicy.create`, `egressProxy.networkPolicy.create`, and `approvedEgress.networkPolicy.create` enabled.
- Keep `request_network_access` behind `tool_approval`.
- Put hostnames that should never need approval on the static allowlist.
- Keep `approvedEgress.maxTtlSeconds` short.
