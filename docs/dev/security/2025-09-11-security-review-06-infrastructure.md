# Infrastructure Security Review

September 2025 review of the SaaS platform's Kubernetes charts (pod privileges, network isolation, RBAC, images, and TLS), updated by a March 2026 audit note.
It reviews the tracked charts, not a live deployment, so verify against the live manifests before acting on it; secret storage and etcd encryption are covered in [review 03](2025-09-11-security-review-03-secrets.md).
Findings that the current charts fix are omitted.

## Open

- Neither platform deployment (frontend or backend) sets a pod or container security context, the backend image runs as root, and tenant Synapse containers start as root to fix their data-volume ownership; a root process widens the impact of a container compromise through privilege escalation or container escape.
- The platform namespace has no NetworkPolicy, unlike tenant instances, so nothing at the network layer limits traffic to or from platform pods.
- Chart defaults use `:latest` images; the release deploy pins the platform and MindRoom images to the release tag, but tenant Synapse stays on `matrixdotorg/synapse:latest` unless an operator sets an image, which leaves tenants exposed to upstream drift and supply-chain changes.
- Only the instance namespace enforces a Pod Security level (`baseline`); the platform namespace enforces none, and no admission policy (for example Kyverno or Gatekeeper) restricts image registries or privileged role bindings.
- The backend's namespaced Role can create, read, update, and delete every Secret in the instance namespace, which the instance chart and Helm release storage need; narrowing those verbs to what each operation needs is open.
- Ingress terminates TLS, but in-cluster service-to-service traffic is plain HTTP; internal TLS or mTLS, for example through a service mesh, remains a deferred decision.
- Per-instance egress allows any public address on ports 80 and 443; whether to narrow it to a destination allowlist is open.
