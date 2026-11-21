---
icon: lucide/kanban
---

# Project Management

This page covers the built-in tools for source hosts, issue trackers, wikis, kanban boards, task managers, help centers, and MindRoom's own per-thread work plans.
Use it to choose a tool, connect its account, and understand its limits.

## Tools On This Page

- [`github`] - GitHub repositories, issues, pull requests, files, branches, code search, and review requests.
- [`todo`] - Per-thread MindRoom work plans with priorities, dependencies, assignments, templates, and automatic nudges for idle agents.
- [`bitbucket`] - One Bitbucket repository's details, commits, pull requests, and issues.
- [`atlassian`](atlassian.md) - Jira and Confluence Cloud as each requester through per-user OAuth, documented on its own page.
- [`jira`] - Jira issue lookup, creation, JQL search, comments, and worklogs with one shared account.
- [`linear`] - Linear teams, issues, and issue updates.
- [`clickup`] - ClickUp spaces, lists, and tasks.
- [`confluence`] - Confluence spaces and page lookup, creation, and updates with one shared account.
- [`notion`] - Pages in one Notion database.
- [`trello`] - Trello boards, lists, cards, and card moves.
- [`todoist`] - Todoist tasks and projects.
- [`zendesk`] - Zendesk Help Center article search.

## Setup

`todo` works without credentials.
The dashboard shows every other tool on this page as needing configuration until its credentials are saved, except that `github` is also ready when `GITHUB_ACCESS_TOKEN` is set.
Most tools also read the environment variables named in their sections.
Connect GitHub per requester through OAuth, and use [`atlassian`](atlassian.md) for per-requester Jira and Confluence Cloud access.
The other tools use one shared set of credentials, saved on the dashboard's Tools page or in the credential store rather than as inline YAML.
Missing Python dependencies are installed at first use, as described in [Automatic Dependency Installation](index.md#automatic-dependency-installation).

## [`github`]

`github` covers repository search and stats, branches, files, issues, labels, pull requests, review requests, and code search.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `access_token` | `password` | `no` | `null` | Explicit GitHub token; takes precedence over OAuth. |
| `base_url` | `url` | `no` | `null` | GitHub Enterprise API root such as `https://github.example.com/api/v3`, not the web UI root. |

```yaml
agents:
  maintainer:
    tools:
      - github:
          base_url: https://github.example.com/api/v3
```

### OAuth Setup

Choose **Connect with GitHub** on the Tools dashboard to connect your own GitHub account through GitHub App user OAuth.
For a self-hosted installation, save the GitHub App client ID and client secret under the `github_oauth_client` credential service and register `/api/oauth/github/callback` on the installation's public URL as the app's callback URL.
MindRoom requests no classic OAuth scopes, so the token has the GitHub App's fine-grained permissions.
Each requester's GitHub connection is their own, regardless of the agent's worker scope, and `github` always runs in the primary MindRoom runtime.
Until a requester connects, `github` calls return an `OAuthConnectionRequired` result containing that requester's connect link.

To use one token for everyone instead, choose **Use access token** and save it, or set `GITHUB_ACCESS_TOKEN` in the runtime environment.
A saved `access_token` wins over `GITHUB_ACCESS_TOKEN`, which wins over a requester's OAuth connection, and blank or whitespace-only values count as unset.

### GitHub In Client Plugins

MindRoom Chat plugins, such as the pull request side panel, can read and write GitHub with the user's own GitHub account instead of a token kept in the browser, and the token never leaves MindRoom.
That account is either the user's GitHub App connection (**Connect with GitHub**, above) or a personal access token the user saved in MindRoom.
A saved personal access token wins over the GitHub App connection; removing it falls back to the connection.
Both are the user's own, kept in their own credential store, and the token sits under its own `github_user_token` credential service.
A personal access token reaches whatever its own scopes and repository access allow, even where the GitHub App is not installed, and needs no GitHub App client.

A plugin calls `GET /api/github/status` with its client session for `{"connected": true, "via": "token" | "app", "login": "<login>"}` or `{"connected": false}`.
`via` names the account requests go out with, and `login` is absent when an App connection has no verified login.
`"app_connected": true` is added while a token is in use and a GitHub App connection stands behind it.

`PUT /api/github/token` with `{"token": "<personal access token>"}` saves the user's token.
MindRoom first checks it with GitHub's `GET /user`, following no redirect and giving up after 10 seconds, and stores it with the login GitHub names.
The answer is `{"connected": true, "via": "token", "login": "<login>"}`.
A token that is empty, longer than 512 characters, or holds anything but visible ASCII characters answers 422 `{"error": "github_request_invalid"}`, as any malformed body of these routes does, naming nothing it was sent (a body that is not UTF-8 gets FastAPI's own 400, which echoes nothing either), and a token GitHub refuses with 401 or 403 answers 400 `{"error": "github_token_rejected"}`.
A rate limit on the check, which is any 429 and a 403 with `x-ratelimit-remaining: 0` or a `retry-after` header, says nothing about the token and answers 502 `{"error": "github_rate_limited"}`.
When GitHub is unreachable, fails, or answers without a well-formed login or with more than 64 KiB, it answers 502 `{"error": "github_unreachable"}`, and a session that names no user gets 409 `{"error": "github_not_connected"}`.
When the token cannot be written it answers 503 `{"error": "github_connection_unavailable"}`.
In every failing case nothing is stored and an earlier token stays.
`DELETE /api/github/token` removes the saved token, if any, and answers the status that remains, in the shape of `GET /api/github/status`, or 503 `{"error": "github_connection_unavailable"}` when the token cannot be removed.
A user's saves and removals run one at a time, so a `DELETE` sent while a `PUT` is still checking with GitHub waits for that `PUT` and then removes its token; this holds within one MindRoom process.

The plugin then calls `POST /api/github/request` with `{"method": ..., "path": ..., "body": ..., "accept": ..., "max_bytes": ...}`, where `method` is `GET`, `POST`, `PUT`, `PATCH`, or `DELETE`, `path` is `/graphql` or under `/repos/<owner>/<repo>` with an optional query, `body` is optional JSON, and `accept` is `application/vnd.github+json` (the default), `application/vnd.github.full+json`, `application/vnd.github.diff`, or `application/vnd.github.raw+json`.
`max_bytes` is optional: set it to an integer from 1 to 10,485,760 to cap the streamed response body, or omit it to use the 10 MiB default.
The answer is HTTP 200 with `{"status", "body", "headers"}` holding GitHub's own status, its JSON or text, and its `x-ratelimit-remaining`, `x-ratelimit-reset`, `retry-after`, and `x-github-request-id` headers.
A successful answer to `application/vnd.github.raw+json` has a text `body` decoded as strict UTF-8, even when the contents are valid JSON.
Use this raw media type for file contents (`/contents` or `/git/blobs`); with it, other endpoints return successful JSON responses as strings.
GitHub refusing a saved token comes back as its own 401 in that answer, and the token stays saved until the user removes or replaces it.
The route follows up to 3 redirects that stay on `https://api.github.com`, so a renamed or transferred repository's old name still works; any other redirect, and a 301 or 302 answer to a write, comes back as GitHub's redirect status.
The route answers 409 `{"error": "github_not_connected"}` while the user has neither a saved token nor a usable GitHub App connection (which also needs the GitHub App client saved), 400 for any other path or one longer than 4096 characters, 503 `{"error": "github_token_unreadable"}` when the saved token could not be read, 503 `{"error": "github_connection_unavailable"}` when the App connection could not be read or refreshed and a retry may help, and 502 when GitHub is unreachable.
A body larger than the cap answers 502 `{"error": "github_response_too_large"}`; a successful raw answer whose body is not valid UTF-8 answers 502 `{"error": "github_response_not_text"}`.
A saved token the store cannot read or decrypt, or whose token or login no longer has the shape a save checks, counts as unreadable.
A store path that is not a plain file or directory, such as a link or a FIFO, reads as no token saved.
`GET /api/github/status` answers both 503s too, the App one only when no saved token answers for the user.
A saved token that is unreadable never falls back to the App connection, since that would act as another account; no retry helps, but saving a new token replaces it in one step, and removing it lets the App connection answer.
These routes use only the user's own token or connection, never a saved `access_token` or `GITHUB_ACCESS_TOKEN`, and never log the token or what GitHub says about it.
The GitHub App connection reaches only repositories where the GitHub App is installed, with the App's permissions: merging, updating, and deleting pull request branches need Pull requests read and write, Contents read and write, Checks read, and Metadata read.

## [`todo`]

`todo` keeps a work plan for the current Matrix room and thread, so separate threads carry independent plans.
It exposes `plan()`, `add_todo()`, `list_todos()`, `update_todo()`, `apply_template()`, and `list_templates()`, and has no configuration fields.
Plans are stored under `mindroom_data/todo/` and survive restarts.

```yaml
agents:
  maintainer:
    tools:
      - todo
```

```python
plan("[high] Inspect issue\nWrite failing test\nImplement fix")
add_todo("Run focused regression test", depends_on="a1b2c3d4", priority="high")
update_todo("a1b2c3d4", status="done")
apply_template("mindroom-dev", {"ISSUE_REF": "ISSUE-123", "REPO": "mindroom"})
```

- `plan()` creates one item per non-empty line, with optional `[low]`, `[medium]`, `[high]`, or `[critical]` prefixes.
- Each item has a priority, a dependency list, a status of `open`, `done`, or `cancelled`, and an optional assigned agent.
- An open item stays blocked until each of its dependencies is `done` or `cancelled`, so cancelling a prerequisite also unblocks the items that depend on it.
- `plan()`, `add_todo()` without an assignee, and template items without an assignee assign the work to the calling agent, so that agent is [auto-poked](#auto-poke) about it.
- `update_todo()` changes the title, priority, status, dependencies, or assignee, and `list_todos(show_all=True)` includes finished items.
- Assigning work to an agent, or changing work already assigned to one, is refused unless the requester may address that agent in this room under its [`access`](../authorization.md#responder-access) policy.
- The assigned agent works on an item as the requester, human or agent, who last added or changed it.

### Templates

`list_templates()` shows the available templates and `apply_template(name, params, dry_run=True)` previews an expansion without writing it.
MindRoom ships a `mindroom-dev` template, which includes a nested `parallel-review-loop` template.
Agents can add their own templates under `todo/templates` in their workspace, and a workspace template hides a built-in template of the same name.
A template file is named `<name>.yaml.j2` and needs a matching `name`, a string `version`, a `description`, and a nonempty `todos` list.
Each entry has a `title` with optional `priority`, `depends_on`, and `assigned_agent`, or a `sub_template` with optional `params` and `depends_on`.
`depends_on` lists one-based entry numbers within the same template.

```yaml
# todo/templates/release.yaml.j2
name: release
version: "1.0"
description: Cut a release.
todos:
  - title: "Update changelog for {{ VERSION }}"
  - title: "Tag {{ VERSION }} on {{ BRANCH | default('main') }}"
    depends_on: [1]
    priority: high
```

Values can use inline Jinja, such as `{{ NAME }}`, `{% if REPO == 'cinny' %}...{% endif %}`, and `{{ BRANCH | default('main') }}`.
Outside Linux, workspace templates may only substitute `{{ NAME }}`.
YAML aliases, `%TAG` directives, and collections nested more than 64 levels deep are refused.

Workspace templates have these limits:

| Limit | Value |
| --- | --- |
| Size of one template file | 64 KiB |
| Template text read by one `apply_template` call, sub-templates included | 64 KiB |
| Rendered output of one `apply_template` call | 65,536 characters |
| Rendering time of one `apply_template` call | 5 seconds |
| Todos created by one `apply_template` call | 100 |

### Auto-Poke

MindRoom wakes an idle agent that has open, unblocked work assigned to it, with no plugin or configuration needed.
An agent is poked once its actionable work in a thread has been quiet for the quiet period, and each scan pokes a given agent at most once.
A pending scheduled task for the same thread suppresses the poke.
Changed work is poked again after a cooldown, and unchanged work gets at most three more pokes, one hour apart.
The poke mentions only the assigned agent and quotes todo titles as plain text.

Work whose requester is a human is poked on that human's behalf, so the agent applies its normal reply access and tool authorization to that human.
Work whose requester is an agent, team, or the router is poked as the assigned agent's own turn.
Work is not poked when its requester is a human, agent, team, or router the assigned agent may not currently reply to, or a non-agent account such as a configured bot account.
Items whose requester was never recorded are skipped and logged once as `todo_poke_requester_unrecorded` until someone allowed to address the assigned agent updates them.

| Environment variable | Default | Behavior |
| --- | --- | --- |
| `MINDROOM_TODO_POKE_INTERVAL_SECONDS` | `120` | Scan interval in seconds; `0` disables auto-poke, and enabled values must be at least `1`. |
| `MINDROOM_TODO_POKE_QUIET_SECONDS` | `300` | Minimum quiet period in seconds before assigned work is poked. |

## [`bitbucket`]

`bitbucket` works on one configured repository: its details, commits, pull requests, pull request changes, and issues.
`list_repositories()` lists the whole workspace.
It authenticates with HTTP Basic using `username` plus `token`, or `username` plus `password` when no token is set.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `username` | `text` | `yes` | `null` | Atlassian account email for Bitbucket Cloud; the Basic-auth username for other deployments. Also read from `BITBUCKET_USERNAME`. |
| `password` | `password` | `no` | `null` | Basic-auth password for deployments that support it. Also read from `BITBUCKET_PASSWORD`. |
| `token` | `password` | `no` | `null` | Scoped Bitbucket Cloud API token; takes precedence over `password`. Also read from `BITBUCKET_TOKEN`. |
| `workspace` | `text` | `yes` | `null` | Bitbucket workspace name. |
| `repo_slug` | `text` | `yes` | `null` | Repository that every repository-scoped call uses. |
| `server_url` | `url` | `no` | `api.bitbucket.org` | Bitbucket host or full base URL; a bare host gets `https://`. |
| `api_version` | `text` | `no` | `2.0` | REST API version appended to `server_url`. |
| `timeout` | `number` | `no` | `30` | Per-request HTTP timeout in seconds. |

```yaml
agents:
  maintainer:
    tools:
      - bitbucket:
          username: buildbot@example.com
          # Store the scoped API token in the token credential field.
          workspace: mindroom
          repo_slug: docs
```

- For Bitbucket Cloud, [create a scoped API token](https://support.atlassian.com/bitbucket-cloud/docs/create-an-api-token/) and store it in `token`, with your Atlassian account email in `username`.
- Other deployments must accept the credentials through Basic authentication, because the tool has no bearer-token mode.
- `create_repository()` creates the repository at the configured `repo_slug`, so set `repo_slug` to the new repository's slug first.

## [`jira`]

`jira` provides `get_issue()`, `create_issue()`, `search_issues()` with JQL, `add_comment()`, and `add_worklog()` through one shared Jira account.
For per-requester Jira Cloud access, use [`atlassian`](atlassian.md) instead.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `server_url` | `url` | `no` | `null` | Jira base URL such as `https://example.atlassian.net`; required unless `JIRA_SERVER_URL` is set. |
| `username` | `text` | `no` | `null` | Jira username or Atlassian account email. Also read from `JIRA_USERNAME`. |
| `password` | `password` | `no` | `null` | Password for self-hosted Jira. Also read from `JIRA_PASSWORD`. |
| `token` | `password` | `no` | `null` | Jira or Atlassian API token; used instead of `password` when both are set. Also read from `JIRA_TOKEN`. |
| `enable_get_issue` | `boolean` | `no` | `true` | Enable `get_issue()`. |
| `enable_create_issue` | `boolean` | `no` | `true` | Enable `create_issue()`. |
| `enable_search_issues` | `boolean` | `no` | `true` | Enable `search_issues()`. |
| `enable_add_comment` | `boolean` | `no` | `true` | Enable `add_comment()`. |
| `enable_add_worklog` | `boolean` | `no` | `true` | Enable `add_worklog()`. |
| `all` | `boolean` | `no` | `false` | Enable every function regardless of the flags above. |

```yaml
agents:
  delivery:
    tools:
      - jira:
          server_url: https://example.atlassian.net
          username: bot@example.com
          enable_add_worklog: false
```

- For Atlassian Cloud, use `username` plus `token`.
- Without `username` and a `token` or `password`, the tool connects anonymously, which most Jira sites reject.

## [`linear`]

`linear` reads the current user, teams, issues, assigned issues, workflow-state issues, and high-priority issues, and creates and updates issues.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `api_key` | `password` | `no` | `null` | Linear API key; required unless `LINEAR_API_KEY` is set. |
| `timeout` | `number` | `no` | `30` | Per-request HTTP timeout in seconds. |

```yaml
agents:
  delivery:
    tools:
      - linear
```

- `get_issue_details()` accepts an issue UUID or a key such as `BLA-123`.
- `create_issue()` needs a `team_id` from `get_teams_details()`; to assign an issue to yourself, pass the ID from `get_user_details()` as `assignee_id`.

## [`clickup`]

`clickup` lists spaces, lists, and tasks, and creates, reads, updates, and deletes tasks.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `api_key` | `password` | `yes` | `null` | ClickUp API key from ClickUp Settings > Apps. Also read from `CLICKUP_API_KEY`. |
| `master_space_id` | `text` | `yes` | `null` | ClickUp team (workspace) ID whose spaces the tool works with. Also read from `MASTER_SPACE_ID`. |
| `timeout` | `number` | `no` | `30` | Per-request HTTP timeout in seconds. |

```yaml
agents:
  delivery:
    tools:
      - clickup:
          master_space_id: "90123456"
```

- Spaces and lists are matched by name, case-insensitively.
- `list_tasks()` returns tasks from every list in a space.
- `create_task()` always creates the task in the first list of the matched space, so check `list_lists()` when placement matters.
- `update_task()` passes any field updates through to the ClickUp API.

## [`confluence`]

`confluence` lists spaces and their pages, and reads, creates, and updates pages through one shared account.
Spaces can be named by title or key.
For per-requester Confluence Cloud access, use [`atlassian`](atlassian.md) instead.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `url` | `url` | `no` | `null` | Confluence base URL; required unless `CONFLUENCE_URL` is set. |
| `username` | `text` | `no` | `null` | Confluence username or Atlassian account email; required unless `CONFLUENCE_USERNAME` is set. |
| `password` | `password` | `no` | `null` | Password for self-hosted Confluence. Also read from `CONFLUENCE_PASSWORD`. |
| `api_key` | `password` | `no` | `null` | Confluence API key; used instead of `password` when both are set. Also read from `CONFLUENCE_API_KEY`. |
| `verify_ssl` | `boolean` | `no` | `true` | Verify TLS certificates; disable only for self-signed internal deployments. |

```yaml
agents:
  docs:
    tools:
      - confluence:
          url: https://example.atlassian.net/wiki
          username: docs@example.com
```

- One of `api_key` or `password` is required; use `api_key` for Atlassian Cloud.
- Page bodies are read and written in Confluence storage format, such as `<p>Initial draft</p>`.

## [`notion`]

`notion` creates pages in one database, appends a paragraph to an existing page, and searches the database by tag.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `api_key` | `password` | `yes` | `null` | Notion internal integration token. Also read from `NOTION_API_KEY`. |
| `database_id` | `text` | `yes` | `null` | Notion database ID. Also read from `NOTION_DATABASE_ID`. |
| `enable_create_page` | `boolean` | `no` | `true` | Enable `create_page()`. |
| `enable_update_page` | `boolean` | `no` | `true` | Enable `update_page()`. |
| `enable_search_pages` | `boolean` | `no` | `true` | Enable `search_pages()`. |
| `all` | `boolean` | `no` | `false` | Enable every function regardless of the flags above. |

```yaml
agents:
  docs:
    tools:
      - notion:
          database_id: 0123456789abcdef0123456789abcdef
          enable_update_page: false
```

- Create the integration at [Notion Developers](https://www.notion.so/my-integrations) and share the database with it, or the tool cannot create or find pages.
- The database must have a title property named `Name` and a select property named `Tag`.
- `create_page()` sets a title, a tag, and one paragraph, and `update_page()` appends a paragraph rather than rewriting the page.

## [`trello`]

`trello` lists, creates, and inspects boards, lists, and cards, and moves cards between lists.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `api_key` | `password` | `no` | `null` | Trello API key. Also read from `TRELLO_API_KEY`. |
| `api_secret` | `password` | `no` | `null` | Trello API secret. Also read from `TRELLO_API_SECRET`. |
| `token` | `password` | `no` | `null` | Trello user token. Also read from `TRELLO_TOKEN`. |

```yaml
agents:
  planner:
    tools:
      - trello
```

- All three credentials are needed; if any is missing, Trello rejects the calls with an authentication error.
- `create_card()` finds the target list by name, case-insensitively, while `move_card()` needs the card ID and destination list ID from `get_cards()` and `get_board_lists()`.
- `list_boards()` accepts the filters `all`, `open`, `closed`, `organization`, `public`, and `starred`.

## [`todoist`]

`todoist` creates, reads, updates, completes, and deletes tasks, and lists active tasks and projects.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `api_token` | `password` | `no` | `null` | Todoist API token; required unless `TODOIST_API_TOKEN` is set. |

```yaml
agents:
  planner:
    tools:
      - todoist
```

- `create_task()` accepts an optional `project_id` from `get_projects()`, a natural-language `due_string` such as `tomorrow`, a `priority`, and `labels`.
- `update_task()` can also change the description, due date or time, due language, assignee, and section.
- `priority` runs from `1` to `4`, where `4` is the highest.
- `close_task()` marks a task complete, while `delete_task()` removes it permanently.

## [`zendesk`]

`zendesk` searches Help Center articles with `search_zendesk()` and returns their body text without titles, URLs, or HTML.
It cannot read or update tickets.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `username` | `text` | `no` | `null` | Zendesk username; required unless `ZENDESK_USERNAME` is set. |
| `password` | `password` | `no` | `null` | Zendesk password; required unless `ZENDESK_PASSWORD` is set. |
| `company_name` | `text` | `no` | `null` | Zendesk subdomain, as in `<company_name>.zendesk.com`, not the display name; required unless `ZENDESK_COMPANY_NAME` is set. |
| `enable_search_zendesk` | `boolean` | `no` | `true` | Enable `search_zendesk()`. |
| `all` | `boolean` | `no` | `false` | Enable every function regardless of the flag above. |
| `timeout` | `number` | `no` | `30` | Per-request HTTP timeout in seconds. |

```yaml
agents:
  support:
    tools:
      - zendesk:
          username: support@example.com
          company_name: acme
```

## Related Docs

- [Tools Overview](index.md)
- [Per-Agent Tool Configuration](index.md#per-agent-tool-configuration)
- [Dashboard](../dashboard.md)
