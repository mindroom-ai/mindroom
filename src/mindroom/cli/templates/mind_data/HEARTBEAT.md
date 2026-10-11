---
title: "HEARTBEAT.md Template"
summary: "Workspace template for HEARTBEAT.md"
read_when:
  - Bootstrapping a workspace manually
---

# HEARTBEAT.md

This file is optional checklist context for requested or scheduled checks.
Adding tasks here does not create a schedule, and leaving it empty does not disable model calls.
Use the `scheduler` tool to create periodic checks explicitly.
For a silent scheduled task, set `silent=True` and return an empty final response or exactly `NO_REPLY` when there is nothing to report.
That suppresses the routine final response only for silent scheduled runs; findings, failures, and independent tool messages remain visible.
It does not suppress ordinary room replies.
