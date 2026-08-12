# Gitleaks Container Design

## Problem

The security workflow downloads a Gitleaks release archive directly from GitHub.

GitHub returned HTTP 503 for every bounded retry, so installation failed before Gitleaks ran.

The earlier retry change reduced sensitivity to brief outages but retained the same failing distribution path.

## Design

Remove the download and archive extraction step.

Run the official `zricethezav/gitleaks:v8.18.4` Docker image by immutable multi-platform digest instead.

Mount the checked-out repository read-only and run `detect --no-banner --redact --exit-code 0` against it.

Disable container networking, drop Linux capabilities, and prevent privilege escalation because the scan only reads local files.

Keep findings informational with Gitleaks' dedicated exit-code option while leaving image acquisition and scanner startup failures fatal.

This avoids the GitHub release-asset path without requiring the organization license used by Gitleaks Action.

## Validation

Validate the workflow YAML with the repository's existing hook.

Run the pinned container command locally against the checkout.

Open a normal pull request and require both push-triggered and pull-request-triggered security jobs to pass.

Inspect current-head AI review comments and address only validated findings.
