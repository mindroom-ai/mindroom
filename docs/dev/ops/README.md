# Operations Guide (Start Here)

This repo supports two primary environments for running MindRoom:

- Local: for development and multi-instance testing on your machine
- Cluster: for Kubernetes + Terraform deployment of the SaaS platform

Use the `just` commands from the repo root to drive everything. Below is a quick decision table.

## Decision Table

- Core dev against local Matrix + DB
  - Commands: `just local-matrix-up|down|logs|reset`, then `run-backend.sh`, `run-frontend.sh`
  - Compose files: `local/matrix/docker-compose.yml`, assets in `local/matrix/docker/`

- Local multi-instance (Compose) with bridges
  - Commands:
    - `just local-instances-create [INSTANCE] [tuwunel|synapse|none]`
    - `just local-instances-start [INSTANCE]` or `just local-instances-start-backend [INSTANCE]`
    - `just local-instances-stop [INSTANCE]`
    - `just local-instances-remove [INSTANCE]`
    - `just local-instances-list`
    - `just local-instances-logs [INSTANCE]`
    - `just local-instances-shell [INSTANCE]`
    - `just local-instances-reset`
  - Location: `local/instances/deploy`

- Cluster (Kubernetes + Terraform) SaaS platform
  - Pre-req: Fill `saas-platform/.env` (see `.env.example`) including Porkbun DNS keys
  - Commands:
    - `just cluster-tf-up`
    - `just cluster-tf-status`
    - `just cluster-tf-destroy`
    - `just cluster-helm-template`, `just cluster-helm-lint`
    - `just cluster-db-backup`
  - Location:
    - Terraform: `cluster/terraform/terraform-k8s`
    - Helm charts: `cluster/k8s/platform`, `cluster/k8s/instance`
    - Ops scripts: `cluster/scripts`

## Local Kubernetes (kind)

Use kind to spin up a throwaway local K8s cluster and install the platform chart for smoke testing and development.

- Prereqs: `kind`, `kubectl`, `helm`, and Docker.
- With Nix: use the root dev shell (`nix-shell`) which now includes `kind`,
  or the focused one at `nix-shell cluster/k8s/kind/shell.nix`.
- Quickstart:
  - `just cluster-kind-up`
  - `just cluster-kind-build-load` (builds platform images and loads them into kind)
  - `just cluster-kind-install-platform`
  - Or run the one-shot script: `cluster/k8s/kind/start-fresh.sh`
  - Port-forward:
    - Backend: `just cluster-kind-port-backend` -> http://localhost:8000
    - Frontend: `just cluster-kind-port-frontend` -> http://localhost:3000

Notes:
- The Helm chart defaults reference a private registry. The `cluster-kind-build-load` step tags and loads images with those names so the cluster uses local images (no registry pull needed).
- Ingress and TLS: In kind we install ingress-nginx, but TLS certificates are not provisioned. Prefer port-forwarding for local testing. If you do use ingress, HTTP is mapped to `localhost:30080`.
- Secrets (Supabase, Stripe, etc.) default to empty; the backend handles missing values for local smoke tests.

- Platform app development
  - Backend dev: `just platform-app-backend-dev`
  - Frontend dev: `just platform-app-frontend-dev`

## Notes

- Instances in Cluster should be created via the platform provisioner API. Avoid direct Helm installs except for debugging.
- `saas-platform/.env` is the source of truth for Terraform + Helm deployment. It is not committed.
- Local artifacts (backups, wgcf, etc.) are ignored by git.
