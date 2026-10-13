# Security Review: Monitoring & Incident Response

September 2025 review of the SaaS platform's security monitoring and incident response.
It keeps the gaps still open when re-checked against the code and cluster configuration on 2026-10-12.

## Open findings

- Alerts reach no one: the platform chart defines Prometheus rules for blocked IPs, unauthorized admin attempts, and blocked-request floods, but the Terraform monitoring stack configures no Alertmanager receivers, so these alerts are seen only by someone looking at Prometheus.
- The authentication audit trail cannot reconstruct every authentication, because cache-hit, admin, and SSO verifications are not fully audited.
- Data access patterns are not monitored: bulk exports, cross-tenant access attempts, and admin access to customer data go undetected.
- Configuration changes are not alerted on, whether to Kubernetes resources, database schema and policies, or application secrets and settings.
- Attack detection stops at authentication brute force: injection, XSS, and path-traversal probes are not detected, and there is no WAF.
- The platform database has only a manual backup (`just cluster-db-backup`) and no documented or tested restore procedure, so recovery from data loss is unproven.
- There is no owned, tested incident-response playbook, no monitored security contact, and no `security.txt`, so breach detection and notification depend on manual review.

The review treated alert routing to on-call destinations, the incident-response playbook with a security contact, and baseline dashboards for authentication failures, admin activity, and anomalous access as launch requirements, and advanced detections such as data-exfiltration and webhook-anomaly alerts as post-launch work.

## Verifying live alert state

These findings come from the repository configuration, not a running cluster.
With the default `monitoring` release and namespace, run `kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090` and open http://localhost:9090 to inspect scrape targets, the platform metrics, and alert status.
