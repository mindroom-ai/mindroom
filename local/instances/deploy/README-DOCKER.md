# SaaS Platform Docker Setup

The SaaS platform's local Docker setup lives in [`saas-platform/docker-compose.yml`](../../../saas-platform/docker-compose.yml).
It runs the platform backend and frontend using the Dockerfiles in that directory and settings from `saas-platform/.env`.
Use [`saas-platform/.env.example`](../../../saas-platform/.env.example) for the platform environment and the [SaaS platform overview](../../../saas-platform/README.md) for its Supabase and Kubernetes architecture.
See the [Kubernetes deployment guide](../../../docs/deployment/kubernetes.md) for hosted deployment instructions.

The former standalone Stripe handler, Dokku provisioner, platform PostgreSQL, and Redis walkthrough is obsolete.
Billing and Helm-based instance provisioning are handled by the platform backend; the local Compose file does not create a Supabase service or a Kubernetes cluster.

For the local MindRoom instance manager in this directory, use [README.md](README.md).
