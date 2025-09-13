# Operations Guide (Start Here)

This repo supports two primary environments for running MindRoom:

- Local: for development and multi-instance testing on your machine
- Cluster: for Kubernetes + Terraform deployment of the SaaS platform

Use the `just` commands from the repo root to drive everything. Below is a quick decision table.

## Decision Table

- Core dev against local Matrix + DB
  - Commands: `just local-matrix-up|down|logs|reset`, then `quickstart.sh`, `run-backend.sh`, `run-frontend.sh`
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
  - Pre-req: Fill `saas-platform/.env` (see `.env.example`)
  - Commands:
    - `just cluster-tf-up` (or `just cluster-tf-up-dns` to force DNS)
    - `just cluster-tf-status`
    - `just cluster-tf-destroy`
    - `just cluster-helm-template`, `just cluster-helm-lint`
    - `just cluster-db-backup`
  - Location:
    - Terraform: `cluster/terraform/terraform-k8s`
    - Helm charts: `cluster/k8s/platform`, `cluster/k8s/instance`
    - Ops scripts: `cluster/scripts`

- Platform app development
  - Backend dev: `just platform-app-backend-dev`
  - Frontend dev: `just platform-app-frontend-dev`

## Notes

- Instances in Cluster should be created via the platform provisioner API. Avoid direct Helm installs except for debugging.
- `saas-platform/.env` is the source of truth for Terraform + Helm deployment. It is not committed.
- Local artifacts (backups, wgcf, etc.) are ignored by git.
