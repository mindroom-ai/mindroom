# Project Management

Use these tools to work with source hosts, issue trackers, knowledge bases, kanban boards, task managers, and support help centers.

## What This Page Covers

This page documents the built-in tools in the `project-management` group.
Use these tools when you need repository context, issue tracking, documentation updates, board workflows, personal task management, or support article search.

## Tools On This Page

- \[`github`\] - GitHub repositories, issues, pull requests, files, branches, code search, and review requests.
- \[`bitbucket`\] - Bitbucket workspace and repository inspection for repositories, commits, pull requests, and issues.
- \[`jira`\] - Jira issue lookup, creation, JQL search, comments, and worklogs.
- \[`linear`\] - Linear GraphQL access for viewer info, teams, issues, and issue updates.
- \[`clickup`\] - ClickUp space, list, task, and task-lifecycle operations.
- \[`confluence`\] - Confluence page lookup, space discovery, page creation, and page updates.
- \[`notion`\] - Notion database page creation, page appends, and tag-based search.
- \[`trello`\] - Trello boards, lists, cards, and card moves.
- \[`todoist`\] - Todoist task creation, updates, completion, deletion, and project discovery.
- \[`zendesk`\] - Zendesk Help Center article search.

## Common Setup Notes

All tools on this page are registered as `status=requires_config`, so they stay unavailable in the dashboard until their required credentials or connection fields are present.
None of these tools declare an `auth_provider`, and `src/mindroom/api/integrations.py` currently only exposes Spotify OAuth routes, so project-management tools are configured through stored tool credentials or environment variables rather than a dedicated dashboard OAuth flow.
Password and token fields should be stored through the dashboard or credential store instead of inline YAML.
Most upstream SDKs also read environment variables, including `GITHUB_ACCESS_TOKEN`, `BITBUCKET_USERNAME`, `BITBUCKET_PASSWORD`, `BITBUCKET_TOKEN`, `JIRA_SERVER_URL`, `JIRA_USERNAME`, `JIRA_PASSWORD`, `JIRA_TOKEN`, `LINEAR_API_KEY`, `CLICKUP_API_KEY`, `MASTER_SPACE_ID`, `CONFLUENCE_URL`, `CONFLUENCE_USERNAME`, `CONFLUENCE_API_KEY`, `CONFLUENCE_PASSWORD`, `NOTION_API_KEY`, `NOTION_DATABASE_ID`, `TRELLO_API_KEY`, `TRELLO_API_SECRET`, `TRELLO_TOKEN`, `TODOIST_API_TOKEN`, `ZENDESK_USERNAME`, `ZENDESK_PASSWORD`, and `ZENDESK_COMPANY_NAME`.
Several registry fields on this page are marked optional in metadata even though the upstream tool effectively requires them at runtime, so the notes below call out the practical requirement level for each tool.
Missing optional dependencies can auto-install at first use unless `MINDROOM_NO_AUTO_INSTALL_TOOLS=1` is set.

## \[`github`\]

`github` is the broadest repository-hosting tool on this page, covering repository search, repository stats, issues, pull requests, files, branches, and code search.

### What It Does

`github` exposes repository discovery methods such as `search_repositories()`, `list_repositories()`, `get_repository()`, `get_repository_with_stats()`, `list_branches()`, `get_repository_languages()`, and `get_repository_stars()`.
It also exposes issue and pull request workflows such as `list_issues()`, `get_issue()`, `comment_on_issue()`, `edit_issue()`, `get_pull_request()`, `get_pull_request_comments()`, `create_pull_request()`, `create_pull_request_comment()`, and `create_review_request()`.
The file-management surface includes `create_file()`, `get_file_content()`, `update_file()`, `delete_file()`, `get_directory_content()`, and `get_branch_content()`.
`base_url` lets the same tool talk to GitHub Enterprise, but it must point at the API root rather than the normal web UI root.

### Configuration

| Option         | Type       | Required | Default | Notes                                                                                |
| -------------- | ---------- | -------- | ------- | ------------------------------------------------------------------------------------ |
| `access_token` | `password` | `no`     | `null`  | GitHub personal access token or GitHub App token.                                    |
| `base_url`     | `url`      | `no`     | `null`  | Optional GitHub Enterprise API base URL such as `https://github.example.com/api/v3`. |

### Example

```
agents:
  maintainer:
    tools:
      - github:
          base_url: https://github.example.com/api/v3
```

```
get_repository("mindroom-ai/mindroom")
list_issues("mindroom-ai/mindroom", state="open", page=1, per_page=20)
get_pull_request("mindroom-ai/mindroom", 123)
```

### Notes

- `access_token` is marked optional in MindRoom metadata, but the upstream client raises at startup if neither `access_token` nor `GITHUB_ACCESS_TOKEN` is present.
- Use `base_url` only for GitHub Enterprise, and set it to the API endpoint such as `/api/v3` rather than the human-facing site root.
- `github` is the best fit on this page when you need repository file operations or rich pull-request inspection in addition to issue tracking.

## \[`bitbucket`\]

`bitbucket` is the Bitbucket repository tool for a configured workspace and repository slug.

### What It Does

`bitbucket` exposes `list_repositories()`, `get_repository_details()`, `create_repository()`, `list_repository_commits()`, `list_all_pull_requests()`, `get_pull_request_details()`, `get_pull_request_changes()`, and `list_issues()`.
The tool always authenticates with a configured `username` plus either `password` or `token`, and it scopes most operations to the configured `workspace` and `repo_slug`.
If `server_url` has no scheme, the upstream tool normalizes it to `https://<server_url>/<api_version>`.

### Configuration

| Option        | Type       | Required | Default             | Notes                                                  |
| ------------- | ---------- | -------- | ------------------- | ------------------------------------------------------ |
| `username`    | `text`     | `yes`    | `null`              | Bitbucket username.                                    |
| `password`    | `password` | `no`     | `null`              | App password, used when `token` is not supplied.       |
| `token`       | `password` | `no`     | `null`              | Access token, used instead of `password` when present. |
| `workspace`   | `text`     | `yes`    | `null`              | Bitbucket workspace name.                              |
| `repo_slug`   | `text`     | `yes`    | `null`              | Repository slug used by most repository-scoped calls.  |
| `server_url`  | `url`      | `no`     | `api.bitbucket.org` | Bitbucket host or full base URL.                       |
| `api_version` | `text`     | `no`     | `2.0`               | Bitbucket REST API version appended to `server_url`.   |

### Example

```
agents:
  maintainer:
    tools:
      - bitbucket:
          username: buildbot
          workspace: mindroom
          repo_slug: docs
```

```
get_repository_details()
list_all_pull_requests(state="OPEN")
list_repository_commits(count=10)
```

### Notes

- Provide either `password` or `token`, and use an app password for Bitbucket Cloud unless you have a reason to use token-based auth.
- `repo_slug` is not just a default, because most methods are hard-scoped to that repository and the current `create_repository()` call path also posts through the configured `repo_slug` endpoint on this branch.
- `list_repositories()` is the workspace-wide overview method, while the pull-request, commit, and issue methods all use the configured repository context.

## \[`jira`\]

`jira` is the issue-tracking toolkit for issue lookup, issue creation, JQL search, comments, and worklogs.

### What It Does

`jira` can expose `get_issue()`, `create_issue()`, `search_issues()`, `add_comment()`, and `add_worklog()` through individual enable flags.
`server_url` is required at runtime, and the upstream client authenticates with `username` plus `token` when both are present, falls back to `username` plus `password`, and otherwise attempts anonymous access.
`search_issues()` uses plain JQL, which makes it the main entry point for filtered issue lists and backlog queries.

### Configuration

| Option                 | Type       | Required | Default | Notes                                                                          |
| ---------------------- | ---------- | -------- | ------- | ------------------------------------------------------------------------------ |
| `server_url`           | `url`      | `no`     | `null`  | Jira base URL such as `https://example.atlassian.net`.                         |
| `username`             | `text`     | `no`     | `null`  | Jira username or Atlassian account email.                                      |
| `password`             | `password` | `no`     | `null`  | Jira password for self-hosted deployments.                                     |
| `token`                | `password` | `no`     | `null`  | Jira or Atlassian API token, preferred over `password` for cloud deployments.  |
| `enable_get_issue`     | `boolean`  | `no`     | `true`  | Enable `get_issue()`.                                                          |
| `enable_create_issue`  | `boolean`  | `no`     | `true`  | Enable `create_issue()`.                                                       |
| `enable_search_issues` | `boolean`  | `no`     | `true`  | Enable `search_issues()`.                                                      |
| `enable_add_comment`   | `boolean`  | `no`     | `true`  | Enable `add_comment()`.                                                        |
| `enable_add_worklog`   | `boolean`  | `no`     | `true`  | Enable `add_worklog()`.                                                        |
| `all`                  | `boolean`  | `no`     | `false` | Enable the full upstream Jira tool surface regardless of the per-method flags. |

### Example

```
agents:
  delivery:
    tools:
      - jira:
          server_url: https://mindroom.atlassian.net
          username: bot@example.com
          enable_add_worklog: false
```

```
get_issue("PROJ-123")
search_issues("project = PROJ AND status != Done", max_results=20)
add_comment("PROJ-123", "Reviewed and ready for testing.")
```

### Notes

- `server_url` is marked optional in metadata, but the upstream client raises if neither `server_url` nor `JIRA_SERVER_URL` is available.
- For Atlassian Cloud, use `username` plus `token` instead of `password`.
- If your Jira deployment allows anonymous API access, the tool can still work without credentials, but most hosted installations do not permit that.

## \[`linear`\]

`linear` is the GraphQL-backed issue tracker tool for viewer info, teams, issues, and issue updates.

### What It Does

`linear` exposes `get_user_details()`, `get_teams_details()`, `get_issue_details()`, `create_issue()`, `update_issue()`, `get_user_assigned_issues()`, `get_workflow_issues()`, and `get_high_priority_issues()`.
All calls go to `https://api.linear.app/graphql`, and the tool expects a Linear API key in either `api_key` or `LINEAR_API_KEY`.
The read methods are useful for discovering the IDs you need before calling `create_issue()` or `update_issue()`.

### Configuration

| Option    | Type       | Required | Default | Notes           |
| --------- | ---------- | -------- | ------- | --------------- |
| `api_key` | `password` | `no`     | `null`  | Linear API key. |

### Example

```
agents:
  delivery:
    tools:
      - linear
```

```
get_user_details()
get_teams_details()
get_high_priority_issues()
```

### Notes

- `api_key` is marked optional in metadata, but the upstream client raises if neither `api_key` nor `LINEAR_API_KEY` is present.
- `get_issue_details()` takes a Linear issue ID rather than an issue key, so use `get_teams_details()` or other Linear discovery steps first when you only know the human-readable issue key from the UI.
- `linear` is the best fit on this page when your workflow is already centered on Linear IDs, teams, and workflow states rather than repository-native pull requests.

## \[`clickup`\]

`clickup` is the ClickUp task-management tool for spaces, lists, tasks, and task lifecycle operations.

### What It Does

`clickup` exposes `list_tasks()`, `create_task()`, `get_task()`, `update_task()`, `delete_task()`, `list_spaces()`, and `list_lists()`.
The tool uses `master_space_id` to call ClickUp's `team/{id}/space` endpoints, so this field is effectively the team or workspace identifier used to discover spaces.
Name-based space and list lookup is case-insensitive and also supports regex-style matching in the current upstream implementation.
`list_tasks()` aggregates tasks across all lists in a space, while `create_task()` creates into the first list returned for the matched space.

### Configuration

| Option            | Type       | Required | Default | Notes                                                  |
| ----------------- | ---------- | -------- | ------- | ------------------------------------------------------ |
| `api_key`         | `password` | `yes`    | `null`  | ClickUp API key.                                       |
| `master_space_id` | `text`     | `yes`    | `null`  | ClickUp team or workspace ID used to enumerate spaces. |

### Example

```
agents:
  delivery:
    tools:
      - clickup:
          master_space_id: "90123456"
```

```
list_spaces()
list_lists("Engineering")
create_task("Engineering", "ISSUE-075", "Draft the project-management tool page")
```

### Notes

- The runtime also checks `CLICKUP_API_KEY` and `MASTER_SPACE_ID`, so you can keep both values in stored credentials or environment instead of YAML.
- `create_task()` always uses the first list returned for the matching space on this branch, so use `list_lists()` first if list placement matters.
- `update_task()` passes arbitrary keyword updates through to the ClickUp API, which makes it the most flexible write method once you have a task ID.

## \[`confluence`\]

`confluence` is the Atlassian wiki tool for space discovery and page retrieval, creation, and updates.

### What It Does

`confluence` exposes `get_page_content()`, `get_space_key()`, `create_page()`, `update_page()`, `get_all_space_detail()`, and `get_all_page_from_space()`.
The tool resolves a space by human-readable name or by key, and other space-scoped methods depend on that resolution step.
`get_page_content()` defaults to `expand="body.storage"`, and `create_page()` and `update_page()` pass raw body content to the Confluence API.
At runtime the tool accepts either `api_key` or `password`, with the current implementation preferring `api_key` or `CONFLUENCE_API_KEY` when both are present.

### Configuration

| Option       | Type       | Required | Default | Notes                                                                |
| ------------ | ---------- | -------- | ------- | -------------------------------------------------------------------- |
| `url`        | `url`      | `no`     | `null`  | Confluence base URL.                                                 |
| `username`   | `text`     | `no`     | `null`  | Confluence username or Atlassian account email.                      |
| `password`   | `password` | `no`     | `null`  | Confluence password for self-hosted deployments.                     |
| `api_key`    | `password` | `no`     | `null`  | Confluence API key, preferred over `password` for cloud deployments. |
| `verify_ssl` | `boolean`  | `no`     | `true`  | Verify TLS certificates when connecting to Confluence.               |

### Example

```
agents:
  docs:
    tools:
      - confluence:
          url: https://mindroom.atlassian.net/wiki
          username: docs@example.com
```

```
get_all_space_detail()
get_page_content("Engineering", "Runbook")
create_page("Engineering", "Release Notes", "<p>Initial draft</p>")
```

### Notes

- `url`, `username`, and one of `api_key` or `password` are all required in practice even though the registry marks them optional.
- For Atlassian Cloud, use `username` plus `api_key`, and reserve `password` for self-hosted or older installations.
- Set `verify_ssl: false` only for self-signed or internal deployments where you understand the TLS tradeoff.

## \[`notion`\]

`notion` is the Notion database tool for page creation, content appends, and tag-based search.

### What It Does

`notion` can expose `create_page()`, `update_page()`, and `search_pages()` through individual enable flags.
The current upstream implementation assumes the target database has a title property named `Name` and a select property named `Tag`.
`create_page()` creates a page with a title, a tag, and one initial paragraph block.
`update_page()` appends a paragraph block to an existing page instead of rewriting the whole page.
`search_pages()` queries the database directly over HTTP and filters by the `Tag` select value.

### Configuration

| Option                | Type       | Required | Default | Notes                                                                            |
| --------------------- | ---------- | -------- | ------- | -------------------------------------------------------------------------------- |
| `api_key`             | `password` | `yes`    | `null`  | Notion integration token.                                                        |
| `database_id`         | `text`     | `yes`    | `null`  | Notion database ID.                                                              |
| `enable_create_page`  | `boolean`  | `no`     | `true`  | Enable `create_page()`.                                                          |
| `enable_update_page`  | `boolean`  | `no`     | `true`  | Enable `update_page()`.                                                          |
| `enable_search_pages` | `boolean`  | `no`     | `true`  | Enable `search_pages()`.                                                         |
| `all`                 | `boolean`  | `no`     | `false` | Enable the full upstream Notion tool surface regardless of the per-method flags. |

### Example

```
agents:
  docs:
    tools:
      - notion:
          database_id: 0123456789abcdef0123456789abcdef
          enable_update_page: false
```

```
search_pages("docs")
create_page("ISSUE-075", "docs", "Draft the project-management tool page")
update_page("PAGE_ID", "Added rollout notes")
```

### Notes

- The integration must be shared with the target database before the tool can create or search pages.
- The database schema must include a `Name` title property and a `Tag` select property, because those names are hard-coded in the current upstream implementation.
- `all: true` overrides the individual enable flags when you want the full Notion surface.

## \[`trello`\]

`trello` is the board-management tool for boards, lists, cards, and card moves.

### What It Does

`trello` exposes `create_card()`, `get_board_lists()`, `move_card()`, `get_cards()`, `create_board()`, `create_list()`, and `list_boards()`.
`create_card()` looks up the target list by case-insensitive `list_name` within a board and then creates the card there.
`move_card()` works by card ID and destination list ID, which makes `get_board_lists()` and `get_cards()` the normal discovery helpers before edits.
If the Trello client cannot initialize, the current upstream methods return `"Trello client not initialized"` instead of structured JSON.

### Configuration

| Option       | Type       | Required | Default | Notes              |
| ------------ | ---------- | -------- | ------- | ------------------ |
| `api_key`    | `password` | `no`     | `null`  | Trello API key.    |
| `api_secret` | `password` | `no`     | `null`  | Trello API secret. |
| `token`      | `password` | `no`     | `null`  | Trello user token. |

### Example

```
agents:
  planner:
    tools:
      - trello
```

```
list_boards(board_filter="open")
get_board_lists("BOARD_ID")
create_card("BOARD_ID", "To Do", "Write docs", "Draft the new tool page")
```

### Notes

- The registry marks all three fields optional, but a working Trello client effectively needs `api_key`, `api_secret`, and `token`.
- `list_boards()` accepts filters such as `all`, `open`, `closed`, `organization`, `public`, and `starred`.
- Use `get_board_lists()` first when you need list IDs for `move_card()` or when you want to confirm the exact list names present on a board.

## \[`todoist`\]

`todoist` is the personal task-management tool for creating, updating, completing, deleting, and listing tasks and projects.

### What It Does

`todoist` exposes `create_task()`, `get_task()`, `update_task()`, `close_task()`, `delete_task()`, `get_active_tasks()`, and `get_projects()`.
`create_task()` supports optional `project_id`, natural-language `due_string`, `priority`, and `labels`.
`update_task()` is the richest write method, with support for content, description, labels, priority, `due_string`, `due_date`, `due_datetime`, `due_lang`, `assignee_id`, and `section_id`.
`close_task()` marks a task complete, while `delete_task()` permanently removes it.

### Configuration

| Option      | Type       | Required | Default | Notes              |
| ----------- | ---------- | -------- | ------- | ------------------ |
| `api_token` | `password` | `no`     | `null`  | Todoist API token. |

### Example

```
agents:
  planner:
    tools:
      - todoist
```

```
create_task("Write project-management docs", due_string="tomorrow", priority=4)
get_active_tasks()
close_task("TASK_ID")
```

### Notes

- `api_token` is marked optional in metadata, but the upstream client raises if neither `api_token` nor `TODOIST_API_TOKEN` is present.
- Use `get_projects()` first when you want to target a specific project with `project_id`.
- `priority` follows Todoist's `1` to `4` scale, where `4` is the highest priority.

## \[`zendesk`\]

`zendesk` is the help-center search tool on this page.

### What It Does

`zendesk` can expose `search_zendesk()` through the `enable_search_zendesk` flag.
The current upstream implementation calls the Zendesk Help Center articles search endpoint at `https://<company_name>.zendesk.com/api/v2/help_center/articles/search.json`.
Search results are reduced to cleaned article body text with HTML tags removed.
This tool does not expose ticket lookup or ticket updates on this branch.

### Configuration

| Option                  | Type       | Required | Default | Notes                                                                             |
| ----------------------- | ---------- | -------- | ------- | --------------------------------------------------------------------------------- |
| `username`              | `text`     | `no`     | `null`  | Zendesk username.                                                                 |
| `password`              | `password` | `no`     | `null`  | Zendesk password.                                                                 |
| `company_name`          | `text`     | `no`     | `null`  | Zendesk subdomain used to build the API URL.                                      |
| `enable_search_zendesk` | `boolean`  | `no`     | `true`  | Enable `search_zendesk()`.                                                        |
| `all`                   | `boolean`  | `no`     | `false` | Enable the full upstream Zendesk tool surface regardless of the per-method flags. |

### Example

```
agents:
  support:
    tools:
      - zendesk:
          username: support@example.com
          company_name: acme
```

```
search_zendesk("Matrix onboarding")
```

### Notes

- `username`, `password`, and `company_name` are all required in practice even though the registry marks them optional.
- `company_name` is the Zendesk subdomain, not the human-readable company display name.
- Because the current tool returns cleaned article body text without titles or URLs, it is better for knowledge lookup than for navigational link retrieval.

## Related Docs

- [Tools Overview](https://docs.mindroom.chat/tools/index.md)
- [Per-Agent Tool Configuration](https://docs.mindroom.chat/configuration/agents/#per-agent-tool-configuration)
- [Dashboard](https://docs.mindroom.chat/dashboard/index.md)
