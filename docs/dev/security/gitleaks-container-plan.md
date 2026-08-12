# Containerized Gitleaks Scan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Make the Gitleaks security scan independent of GitHub release-asset downloads.

**Architecture:** The workflow runs the official Gitleaks Docker image by immutable digest against a read-only checkout mount.
The scan retains its existing informational failure behavior.

**Tech Stack:** GitHub Actions, Docker, Gitleaks 8.18.4, pre-commit.

## Global Constraints

Do not use Gitleaks Action because organization repositories require a license secret.

Do not download or extract a Gitleaks release archive.

Pin the official Docker image to `v8.18.4@sha256:75bdb2b2f4db213cde0b8295f13a88d6b333091bbfbf3012a4e083d00d31caba`.

Keep Gitleaks findings informational with `--exit-code 0` while leaving Docker and scanner failures fatal.

Run the container without networking, Linux capabilities, or privilege escalation.

---

### Task 1: Replace the Gitleaks installation path

**Files:**

- Modify: `.github/workflows/security-scan.yml`

**Interfaces:**

- Consumes: The repository checkout in the GitHub Actions workspace.
- Produces: A Gitleaks scan of the mounted checkout with redacted output.

- [ ] **Step 1: Verify the old workflow fails the desired runtime contract**

Run the current workflow's release-asset download command with an unreachable local endpoint and confirm curl exits nonzero before any scan can execute.

```bash
curl --fail --location --silent --show-error --retry 1 --connect-timeout 1 --max-time 2 http://127.0.0.1:1/gitleaks.tar.gz --output /dev/null
```

Expected: nonzero exit caused by connection failure.

- [ ] **Step 2: Replace installation and scanning with one container step**

Use this workflow command:

```yaml
- name: Secret scan (gitleaks)
  run: |
    docker run --rm \
      --network none \
      --cap-drop ALL \
      --security-opt no-new-privileges \
      --volume "$PWD:/repo:ro" \
      --workdir /repo \
      zricethezav/gitleaks:v8.18.4@sha256:75bdb2b2f4db213cde0b8295f13a88d6b333091bbfbf3012a4e083d00d31caba \
      detect --no-banner --redact --exit-code 0 --source .
```

- [ ] **Step 3: Run the container scan**

Run the command from Step 2 locally.

When validating from a linked worktree, also mount its external Git metadata read-only at the absolute path referenced by the worktree's `.git` file.

Expected: Docker resolves the pinned digest, Gitleaks reports findings without failing, and the shell exits zero.

Run the pinned image with an invalid Gitleaks command.

Expected: the container exits nonzero, proving scanner startup failures remain fatal.

- [ ] **Step 4: Validate the workflow file**

```bash
uv run pre-commit run check-yaml --files .github/workflows/security-scan.yml
uv run pre-commit run --files .github/workflows/security-scan.yml docs/dev/security/gitleaks-container-design.md docs/dev/security/gitleaks-container-plan.md
git diff --check origin/main...HEAD
```

Expected: every command exits zero.

- [ ] **Step 5: Commit the workflow change and plan**

```bash
git add .github/workflows/security-scan.yml docs/dev/security/gitleaks-container-plan.md
git commit -m "ci: run Gitleaks from pinned container"
```

### Task 2: Validate through GitHub

**Files:**

- Modify: None unless a validated AI review finding requires a correction.

**Interfaces:**

- Consumes: The pushed branch and GitHub Actions workflow.
- Produces: A normal pull request with successful current-head checks and a five-out-of-five AI review.

- [ ] **Step 1: Push and open a normal pull request against `main`**

Use a concise repository-relative description that includes the root cause, replacement path, and validation evidence.

- [ ] **Step 2: Wait for both security workflow triggers and AI review**

Require the push-triggered and pull-request-triggered `security-scan` jobs to finish successfully on the exact head.

Require the AI reviewer to report five out of five.

- [ ] **Step 3: Validate review comments**

Check every current-head AI review comment against the workflow and address only correct in-scope findings.

After any push, repeat the exact-head CI and AI review gate.

- [ ] **Step 4: Merge**

Squash merge only when all required checks pass, the AI review is five out of five, no actionable review thread remains, and the PR head still matches the reviewed SHA.
