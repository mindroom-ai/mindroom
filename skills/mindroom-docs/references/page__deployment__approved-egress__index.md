# Approved Egress

Approved egress lets worker-routed tools reach external hostnames through a policy-enforcing HTTP proxy.
Use it when dedicated Kubernetes workers should be blocked from direct internet egress unless a hostname is statically allowlisted or temporarily approved by a human.

## Runtime Chart

The runtime chart can deploy the proxy and wire MindRoom to the built-in `approved_egress` toolkit.

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
    tag: v0.1.0
  allowlist:
    domains:
      - example.com
      - .docs.example.com
```

The chart renders the proxy Deployment, Service, ServiceAccount, RBAC, allowlist ConfigMap, persistence PVC, worker egress NetworkPolicy, and proxy ingress NetworkPolicy.
The chart also sets `MINDROOM_APPROVED_EGRESS_ENABLED`, `MINDROOM_APPROVED_EGRESS_API_URL`, `MINDROOM_APPROVED_EGRESS_ALLOWLIST_PATH`, `MINDROOM_APPROVED_EGRESS_TOKEN`, and `MINDROOM_APPROVED_EGRESS_MAX_TTL_SECONDS` on the MindRoom container.
When `MINDROOM_APPROVED_EGRESS_ENABLED=true`, MindRoom adds `approved_egress` to defaults and requires Matrix approval for `request_network_access` at runtime.
These runtime-derived entries are not written back to `config.yaml` by dashboard or API saves.

## Custom Config

The runtime chart handles custom `config.data` and `config.existingConfigMap` through `MINDROOM_APPROVED_EGRESS_ENABLED=true`, so you do not need to duplicate this block there.
Use this block only when enabling the built-in toolkit outside the runtime chart.

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

You can assign `approved_egress` to individual agents instead of `defaults.tools` if only some agents should request network access.
Do not add a `plugins:` entry for approved egress in new deployments.
The toolkit is built into MindRoom and uses the chart-provided policy API URL, token, allowlist path, and TTL settings.

## Runtime Behavior

Agents call `request_network_access(hostname, ttl_minutes, reason)` when a worker needs one blocked external hostname.
The tool rejects schemes, ports, paths, wildcards, IP literals, single-label names, localhost names, cluster-local names, and known metadata hostnames before it calls the policy API.
If the hostname already matches the static allowlist, the tool reports that no dynamic grant is needed.
For `worker_scope: user_agent`, the tool creates a `worker_key` grant for the exact requester-owned worker.
For shared or unscoped workers, the tool creates an `agent` grant.
For `worker_scope: user`, the tool rejects the request because one user-scoped worker can serve multiple agents.

## Secure Minimum

Use `workers.backend: kubernetes`.
Keep `workers.kubernetes.networkPolicy.create` and `egressProxy.networkPolicy.create` enabled.
Provide `approvedEgress.token.existingSecret` or `workers.sandbox.proxyToken`.
Pin `approvedEgress.image.tag` or `approvedEgress.image.digest`.
Keep `request_network_access` behind `tool_approval`.
Use a static allowlist for hostnames that should never require approval.
Use short `approvedEgress.maxTtlSeconds` values for temporary grants.

## Migration

Deployments that manually installed an approved egress plugin can remove the plugin install step.
Remove the approved egress `plugins:` entry after the MindRoom runtime includes the built-in toolkit.
Keep the proxy image, token Secret, allowlist, worker proxy env, NetworkPolicy, and tool approval rule.
Existing manual plugin entries are tolerated so upgrades do not fail immediately, but the built-in toolkit is the source of truth for new deployments.
