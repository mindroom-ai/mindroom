---
name: overleaf
description: Use when the user asks you to read, edit, or push changes to an Overleaf LaTeX project, or shares an overleaf.com project link.
---

# Overleaf

Overleaf has no public API for agents. You work on a project through Overleaf's Git bridge with `git` in your shell.
Agent Keys adds the user's Overleaf Git token to every request to `git.overleaf.com`, so you never see or type the token.

## Before you start

You need the `shell` and `egress_credentials` tools, and an Agent Keys service for Overleaf.

1. Call `list_egress_credentials` and look for a service on Overleaf (usually named `overleaf`, display name "Overleaf").
2. No such service: the user adds it on the Agent Keys page (the `manage_url` from `list_egress_credentials`) with **Add service**, preset **Overleaf**.
   Send them that link and wait; you cannot add the service yourself. If `manage_url` is `null`, follow the `note` instead.
3. The service has `configured: false`: call
   `request_secret(service="overleaf", reason="I need your Overleaf Git token to clone and update your project.", url="https://www.overleaf.com/user/settings", placeholder="olp_…")`.
   Use the service's real `name` from step 1. Tell the user the token is under **Git integration** in Overleaf's Account Settings, and that Git access needs an Overleaf premium plan.
   Never ask the user to paste the token into the chat, and never put a token in a URL or a command.

## Clone

The project id is the last part of the project's link: `https://www.overleaf.com/project/<project_id>`.
Work in your workspace, one folder per project:

```bash
git clone https://git.overleaf.com/<project_id> overleaf-<project_id>
cd overleaf-<project_id>
```

If the service you use has `source: user` in `list_egress_credentials`, name it on every request, or a service of
the administrator's on the same host may win. Clone with the header instead, which the folder then keeps for pulls
and pushes:

```bash
git clone -c 'http.https://git.overleaf.com/.extraHeader=X-Agent-Keys-Service: <service name>' https://git.overleaf.com/<project_id> overleaf-<project_id>
```

Never send this header for a service without `source: user`: the broker then refuses the request.

The main file is usually `main.tex`; otherwise it is the `.tex` file with `\documentclass`.

## Edit and push

1. Pull first, before you edit: `git pull --rebase origin master`.
2. Edit the `.tex`, `.bib`, and other source files with your file tools.
3. Stage only the files you changed: `git add <files>`. Never `git add -A`.
4. `git commit -m "<what you changed>"`. If git asks who you are, set it for this folder only:
   `git config user.name "<your name>"` and `git config user.email "<your name>@mindroom.invalid"`.
5. `git push origin master`.

Overleaf has only the `master` branch. Never force push; Overleaf refuses it anyway.
A push is rejected when someone changed the project in Overleaf meanwhile: run `git pull --rebase origin master` and push once more.
If the rebase stops on a conflict, or the second push is rejected too, stop and tell the user which files conflict.

## Compile

Do not compile the project in your shell: `latexmk` (also `latexmk -c`) runs a project's `latexmkrc`, which is code
anyone with edit access can write, and your shell can reach the user's keys.
After you push, ask the user to recompile in Overleaf and to tell you the first error, if any.
Never commit build outputs such as `*.aux`, `*.log`, or a `.pdf`.

## When it fails

- `401`, `Authentication failed`, or `could not read Username for 'https://git.overleaf.com'`:
  the token is missing, expired, or revoked, or the account has no premium plan.
  Check `list_egress_credentials`; if the service shows `configured: false`, ask with `request_secret` as above.
  If it shows `configured: true`, the token is wrong or expired: ask the user to create a new one and call `request_secret` again.
- A refusal from Agent Keys (a JSON body with an `error` such as `credential_not_configured` or
  `service_not_matched`) is not a project problem: follow its `message`, check `list_egress_credentials`, and use
  `request_secret` for a missing key.
- Otherwise `403` or `404` / `repository not found`: the project id is wrong, or the token's account cannot open
  the project. Ask the user for the project link and to check they are a collaborator.
- `429` / too many requests: wait a minute, then retry once.
- A push refused for a large file or a file name: Overleaf limits file sizes and some names; tell the user which file.
- Self-hosted Overleaf (Server Pro) uses its own host, not `git.overleaf.com`.
  The user adds a **Custom** service on the Agent Keys page for that host, with basic auth and username `git`.
