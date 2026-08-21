# GitHub App Credentials for Git Knowledge Sources

## Goal

Allow a Git-backed knowledge source to authenticate as a GitHub App installation without storing a personal access token or a short-lived installation token in MindRoom's credential store.

## Credential contract

A named shared credential service may select GitHub App authentication with these fields:

```json
{
  "auth_type": "github_app",
  "app_id": 12345,
  "installation_id": 67890,
  "private_key_file": "/var/run/secrets/github-app/private-key.pem"
}
```

`app_id` and `installation_id` are non-secret deployment metadata.
`private_key_file` points to a read-only secret mount.
MindRoom never copies the private key or an installation access token into a credential file, Git configuration, checkout metadata, or logs.
Existing username/password/token credential services keep their current behavior.

## Token flow

Before an HTTPS clone, fetch, or Git LFS pull, MindRoom resolves the configured credential service.
For `auth_type: github_app`, it validates that the remote is an ordinary `https://github.com/<owner>/<repository>` URL, reads the private key, creates a short-lived app JWT, and requests an installation access token from GitHub.

The token request names exactly the repository from the remote and requests only `contents: read`.
The returned token is supplied to Git as process-local HTTP Basic authentication with username `x-access-token`.
The clean remote URL remains the only persisted URL.

Each knowledge source caches its repository-scoped token until five minutes before GitHub's reported expiry.
A refresh reads the private-key file again, so key rotation takes effect without rewriting runtime credential metadata.
Concurrent resolutions for one source share one refresh operation.

## Failure behavior

Malformed GitHub App credentials fail closed with a field-specific configuration error.
GitHub App credentials reject non-GitHub hosts and remotes that do not identify exactly one owner and repository.
Missing or unreadable key files fail without including key contents.
GitHub token endpoint failures report HTTP status and installation ID, never response bodies or tokens.

If a cached token is still valid, a transient GitHub API failure does not occur because no refresh is attempted.
Once refresh is required, authentication fails rather than falling back to anonymous Git or stale static credentials.

## Structure

- `mindroom.knowledge.github_app_auth` owns credential parsing, repository extraction, JWT creation, token minting, expiry handling, and caching.
- `mindroom.knowledge.git_source` selects static credentials or the GitHub App provider and injects the resulting secret into each Git subprocess.
- `KnowledgeGitConfig.credentials_service` remains the public configuration surface; no repository configuration schema change is required.

## Tests

Unit tests cover credential validation, GitHub URL validation, JWT/token request shape, least-privilege repository and permission scoping, cache reuse, expiry refresh, key rereading, concurrency, and redacted errors.
Git source tests prove clone/fetch/LFS request fresh credentials through the resolver while static credentials and secret non-persistence remain unchanged.

Deployment tests must prove credential seeds contain metadata only, the private key is mounted read-only only in the control plane, and rendered resources never place private-key material in environment variables or ConfigMaps.
