# GitHub App Credentials for Git Knowledge Sources

This record explains why Git knowledge sources can authenticate as a GitHub App installation and which secret-handling rules that design depends on.

## Why

Unattended Git-backed knowledge sources need GitHub access without storing a personal access token or a short-lived installation token in MindRoom's credential store.

## Decisions

- The stored credential holds only non-secret deployment metadata (the App and installation IDs) and the path of a read-only secret mount that holds the private key.
  MindRoom mints installation tokens on demand and reads the key file again whenever a token needs refreshing, so key rotation takes effect without rewriting runtime credential metadata.
- The private key and installation tokens are never copied into a credential file, Git configuration, checkout metadata, a refresh-child launch environment variable, or logs, and the clean remote URL is the only persisted URL.
- The control plane hands its cached token to each scheduled refresh child through the existing stdin request pipe, so refresh subprocess isolation does not cause another token mint.
  The handoff names the App, installation, repository, and key path that authorized the token, and a child whose current credentials no longer match ignores it and mints a token for its current credentials, so a rotation between the parent's mint and the child's startup never reuses a token minted for the old credentials.
- Deployments that provision a GitHub App key must keep credential seeds to metadata only, mount the private key read-only and only into the control plane, and never place key material in environment variables or ConfigMaps.

## Assumptions

- GitHub issues an installation access token in exchange for a short-lived App JWT signed with the App private key, lets the request limit the token to named repositories and permissions, and reports the token's expiry.
- Git over HTTPS accepts the installation token as HTTP Basic authentication with username `x-access-token`.
