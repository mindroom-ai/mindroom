---
icon: lucide/layout-list
---

# Atlassian Cloud

The `atlassian` tool works with Jira and Confluence Cloud as the person who asked.
Each requester connects their own Atlassian account through OAuth 2.0 (3LO), so agents see and change only what that person can see and change in Atlassian.
MindRoom owns the OAuth state, callback, token refresh, and credential storage through its [OAuth framework](../oauth-framework.md).
Additional Atlassian sites can be added as separate, independently connected tools through a small plugin.

## What It Does

| Function | Product | Changes data | Purpose |
| --- | --- | --- | --- |
| `jira_search_issues` | Jira | no | Search issues with JQL, with `next_page_token` paging. |
| `jira_get_issue` | Jira | no | Read one issue's summary fields and description, or the requested `fields`, with the transitions available from its current status. |
| `jira_create_issue` | Jira | yes | Create an issue from a project key, summary, issue type, optional plain-text description, and optional extra fields. |
| `jira_update_issue` | Jira | yes | Update the summary, description, or other fields of an issue. |
| `jira_add_comment` | Jira | yes | Add a plain-text comment to an issue. |
| `jira_transition_issue` | Jira | yes | Move an issue through its workflow by transition ID, transition name, or target status name. |
| `confluence_search` | Confluence | no | Search with CQL, with `cursor` paging. |
| `confluence_get_page` | Confluence | no | Read a page with its storage-format body, space, and current version number. |
| `confluence_list_attachments` | Confluence | no | List a page's attachments with media type, size, and version, with `cursor` paging. |
| `confluence_download_attachment` | Confluence | no | Download one page attachment into the conversation as a context attachment. |
| `confluence_create_page` | Confluence | yes | Create and publish a page in a space, optionally under a parent page. |
| `confluence_update_page` | Confluence | yes | Replace a page's title and body as a new version. |
| `confluence_add_comment` | Confluence | yes | Add a footer comment to a page. |

Plain-text Jira descriptions and comments are converted to Atlassian Document Format, with blank lines separating paragraphs.
`jira_transition_issue` picks an exact transition ID first, then a transition name, then a target status that only one transition leads to, and name and status matches ignore case.
When several transitions share that name or target status, it returns `transition_ambiguous` with the candidates and changes nothing.
Confluence bodies use the Confluence storage format, which is XHTML such as `<p>Hello</p>`.
Every result is a JSON object with `status` set to `ok` or `error`.
Successful results name the `product` and the `site` that answered.

The existing `jira` and `confluence` tools are separate integrations that use a shared API token or password.
They are unchanged, and their function names do not collide with the `atlassian` functions, so an agent can have both.

## Set Up The Atlassian App

An administrator creates one Atlassian OAuth 2.0 (3LO) app for the MindRoom installation.

1. Open the [Atlassian developer console](https://developer.atlassian.com/console/myapps/) and create an **OAuth 2.0 integration**.
2. Under **Authorization**, configure OAuth 2.0 (3LO) with the callback URL `https://mindroom.example.com/api/oauth/atlassian/callback`, replacing the origin with your public MindRoom origin.
3. Under **Permissions**, add the Jira API and the Confluence API, and enable the scopes listed in [Scopes](#scopes).
4. Under **Distribution**, enable sharing so that people other than the app owner can connect.
5. Under **Settings**, copy the client ID and secret.

For a local installation, the callback URL is `http://localhost:8765/api/oauth/atlassian/callback`.
MindRoom derives callback URLs from `MINDROOM_PUBLIC_URL` or `MINDROOM_BASE_URL`, so set one of them for a hosted installation.
The callback URL registered on the app must match exactly.

Store the client ID and secret in the `atlassian_oauth_client` credential service through the dashboard credentials page.
For non-interactive deployments, seed it at startup with a [credential seed](../configuration/index.md#credential-seeds):

```json
[
  {
    "service": "atlassian_oauth_client",
    "credentials": {
      "client_id": {"env": "ATLASSIAN_CLIENT_ID"},
      "client_secret": {"env": "ATLASSIAN_CLIENT_SECRET"}
    }
  }
]
```

The client configuration stays in the primary runtime and is never mirrored into worker containers.
MindRoom ignores any stored `redirect_uri` on this service and always uses each provider's own callback.

## Scopes

MindRoom requests only the scopes that the enabled functions call, plus `offline_access` so that tokens can be refreshed.
Atlassian recommends classic scopes, and MindRoom uses them for every API that accepts them.
Confluence page and comment writes exist only in the Confluence v2 API, which accepts granular scopes only, so those three scopes are granular.

| Scope | Type | Used by |
| --- | --- | --- |
| `offline_access` | classic | Token refresh |
| `read:jira-work` | classic | `jira_search_issues`, `jira_get_issue`, `jira_transition_issue` |
| `write:jira-work` | classic | `jira_create_issue`, `jira_update_issue`, `jira_add_comment`, `jira_transition_issue` |
| `search:confluence` | classic | `confluence_search`, `confluence_get_page`, `confluence_list_attachments` |
| `read:confluence-content.all` | classic | `confluence_get_page`, `confluence_list_attachments` |
| `readonly:content.attachment:confluence` | classic | `confluence_download_attachment` |
| `read:space:confluence` | granular | `confluence_create_page`, to resolve a space key |
| `write:page:confluence` | granular | `confluence_create_page`, `confluence_update_page` |
| `write:comment:confluence` | granular | `confluence_add_comment` |

MindRoom treats a stored grant that lacks any requested scope as disconnected, and the tool asks the requester to connect again.
Changing a connection's products or write access therefore requires every user of that connection to reconnect once.

## Configure The Default Connection

Add the tool to an agent:

```yaml
agents:
  assistant:
    tools:
      - atlassian:
          site_url: https://example.atlassian.net
```

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `site_url` | `url` | `null` | Atlassian Cloud site to use, as its `https://<name>.atlassian.net` URL such as `https://example.atlassian.net`; any path such as `/wiki` is ignored. |
| `cloud_id` | `text` | `null` | Atlassian cloud ID of the site; when `site_url` is also set, both must name the same site. |

Both options are optional and can also be set once for every agent through `defaults.tools` or the tool's dashboard settings.
When neither is set and the connected account can reach exactly one Jira or Confluence site, the tool uses that site.
When the account can reach several sites, the tool returns `site_selection_required` with the available sites instead of choosing one.
When a configured site is not reachable with the connected account, the tool returns `site_not_found` and never falls back to a different site.
Atlassian reports every site by its `https://<name>.atlassian.net` URL, even when the site is served under a custom domain, so a custom-domain `site_url` never matches.
Use the site's atlassian.net URL or its `cloud_id` instead.
The `available_sites` in a `site_not_found` result list the URLs and cloud IDs that Atlassian reports.
When both `site_url` and `cloud_id` are set, the site must match both, and a mismatch returns `site_not_found`.
Site URLs must use HTTPS, and cloud IDs must be UUIDs, so a misconfigured value keeps the tool from loading and logs a warning.

## Connect An Account

When a requester has not connected Atlassian yet, every function returns a structured result with `oauth_connection_required: true` and a `connect_url` for that requester.
The requester opens the link, signs in to Atlassian, approves the requested scopes, and retries the request.
Users can also connect and disconnect from the dashboard Integrations page.

Tokens are stored per requester in that person's `user` credential scope, like GitHub.
They are never stored in a shared or global scope, and a call without a concrete requester always asks for a connection instead of using another account.
Atlassian rotates refresh tokens, and MindRoom stores each new refresh token through its OAuth lifecycle.
A refresh token that has not been used for 90 days expires, and the requester then reconnects.

## Add More Connections

Use `AtlassianConnectionConfig` to add another Atlassian site, such as a partner's Confluence, next to the default connection.
Each connection gets its own OAuth provider, stored credentials, site pin, requested scopes, and prefixed function names.
The default `atlassian` tool and its stored connections are unchanged, and no credential ever crosses connections.

A plugin can use one module for both registrations:

```json
{
  "name": "atlassian-sites",
  "tools_module": "sites.py",
  "oauth_module": "sites.py"
}
```

```python
# sites.py
from mindroom.tool_system.atlassian_connections import (
    AtlassianConnectionConfig,
    atlassian_connection_oauth_provider,
    register_atlassian_connection_tools,
)

CONNECTIONS = (
    AtlassianConnectionConfig(
        name="partner",
        display_name="Partner Confluence",
        site_url="https://acme.atlassian.net",
        cloud_id="00000000-0000-0000-0000-000000000000",
        products=("confluence",),
    ),
)

for connection in CONNECTIONS:
    register_atlassian_connection_tools(connection)


def register_oauth_providers(settings, runtime_paths):
    return [atlassian_connection_oauth_provider(connection) for connection in CONNECTIONS]
```

Enable the plugin and add the connection's tool to agents:

```yaml
plugins:
  - ./plugins/atlassian-sites

agents:
  assistant:
    tools:
      - atlassian
      - partner_atlassian
```

| Field | Default | Notes |
| --- | --- | --- |
| `name` | required | Lowercase letters, digits, and underscores, starting with a letter, at most 16 characters. |
| `display_name` | required | Name shown on connect links and the Integrations page. |
| `site_url` | `null` | The site's `https://<name>.atlassian.net` URL, not a custom domain; only its origin is kept. |
| `cloud_id` | `null` | Cloud ID of the site; when `site_url` is also set, both must name the same site. |
| `products` | `("jira", "confluence")` | Products whose functions and scopes this connection enables. |
| `write` | `True` | Set to `False` to register only read functions and request only read scopes. |
| `client_config_service` | `<name>_atlassian_oauth_client` | Credential service holding the connection's app client ID and secret. |

Every connection requires `site_url` or `cloud_id`, so a named connection always acts on one pinned site.
The `name` determines every identifier, so keep it stable once users have connected.

| Identifier | Value for `name="partner"` |
| --- | --- |
| Tool and provider ID | `partner_atlassian` |
| Callback URL | `https://mindroom.example.com/api/oauth/partner_atlassian/callback` |
| Token service | `partner_atlassian_oauth` |
| App client service | `partner_atlassian_oauth_client` |
| Function names | `partner_confluence_search`, `partner_confluence_get_page`, and so on |

By default, each connection uses its own Atlassian app, because Atlassian documents one callback URL per app.
Create an app for the connection as in [Set Up The Atlassian App](#set-up-the-atlassian-app), register the connection's callback URL on it, and store its client ID and secret in the connection's app client service.
A connection that never received its own client credentials stays unconfigured and never falls back to the default app.

To reuse the default app instead, set `client_config_service="atlassian_oauth_client"` and register the connection's callback URL on that app, if the developer console accepts an additional one.
A shared app has two side effects.
Atlassian records consent per user and app, so the default connection can then also reach the other site, and an unpinned `atlassian` tool starts returning `site_selection_required` until it gets a `site_url` or `cloud_id`.
Revoking the shared app in Atlassian disconnects every connection that uses it.

Each connection has its own card on the Integrations page, and its functions return their own connect link when they need a login.

## Approvals For Writes

Functions that create, update, comment on, or transition Atlassian data are listed as changing data in [What It Does](#what-it-does).
Use MindRoom's `tool_approval` rules to require a Matrix approval card for them while reads run immediately:

```yaml
tool_approval:
  rules:
    - match: "*jira_create_issue"
      action: require_approval
    - match: "*jira_update_issue"
      action: require_approval
    - match: "*jira_add_comment"
      action: require_approval
    - match: "*jira_transition_issue"
      action: require_approval
    - match: "*confluence_create_page"
      action: require_approval
    - match: "*confluence_update_page"
      action: require_approval
    - match: "*confluence_add_comment"
      action: require_approval
```

The leading `*` makes each rule also match the prefixed functions of additional connections, such as `partner_confluence_create_page`.
To approve reads automatically under a stricter `tool_approval.default: require_approval`, add `auto_approve` rules for `*jira_search_issues`, `*jira_get_issue`, `*confluence_search`, `*confluence_get_page`, `*confluence_list_attachments`, and `*confluence_download_attachment` instead.
To give an agent read access only, use `exclude_tools` for the write functions, or register a connection with `write=False` so that it never requests write scopes.

## Attachments

`confluence_list_attachments` returns each attachment's ID, such as `att123456`, with its title, media type, size, comment, and version.
When more results exist, it returns `has_more: true` with a `next_cursor` to pass back unchanged, and a `warning` if Atlassian linked another page without a usable cursor.

`confluence_download_attachment` stores the file in MindRoom's managed attachment storage for the current room and thread and returns a context attachment ID such as `att_0123456789abcdef`.
The ID is usable in the same turn, for example with `get_attachment(attachment_id)` to inspect the file, `get_attachment(attachment_id, mindroom_output_path=...)` to save it to the workspace, or `matrix_message` to send it.
In a later turn, download the attachment again.
Downloads require a conversation with attachment storage and return `attachment_context_unavailable` otherwise.
A download of an attachment that does not exist, or that the connected account may not view, returns `attachment_unavailable`.
The display filename comes from the RFC 5987 `filename*` parameter when present, then `filename`, then the optional `filename` argument, and it is stripped of directories and control characters.
The stored file name is always generated from the attachment ID.

## Security Notes

- The tool always runs in the primary MindRoom runtime, so worker containers never receive OAuth client configuration or user tokens.
- The tool takes no local file paths and reads or writes no workspace files; downloads go only to MindRoom's managed attachment storage.
- The bearer goes only to `https://api.atlassian.com/oauth/token/accessible-resources` to find the connected sites and to `https://api.atlassian.com/ex/{product}/{cloud_id}/...` for the pinned site, and cloud IDs from Atlassian are accepted only in UUID form.
- Page, attachment, issue, and cursor arguments are validated before any authentication or network access, so they cannot change a request path.
- The OAuth bearer is sent only to the pinned site's gateway path for the product being called, never to the Atlassian media service or any other host.
- Downloads follow at most three redirects, only over HTTPS on the default port without URL credentials, and only to that gateway path or `api.media.atlassian.com`.
- A gateway path containing dot segments, encoded or repeatedly encoded slashes, backslashes, or `;` parameters is treated as foreign and rejected.
- Cookies are cleared before every download hop, so a cookie set by one hop never reaches another.
- Every download hop asks for `Accept-Encoding: identity`, and content-encoded responses are rejected instead of decoded.
- Error results carry a code, a fixed message, and a status code, and never a request URL, signed download link, token, or raw response body.
- HTTP request logs drop the signature query from media service URLs.
- At debug level, each download hop logs only its host, its path with identifiers masked and without query or parameters, its status, and whether the bearer was sent.
- Jira and Confluence error messages are kept for failed API requests, with URLs replaced and control characters removed.
- A gateway 401 whose message reports a scope mismatch returns `scope_mismatch` with Atlassian's message and no reconnect link, because reconnecting grants the same scopes again.
- Any other gateway 401, for an API call or a download, returns a reconnect link instead of Atlassian's error text.

## Limits

- Search and listing functions return at most 50 results per call.
- Each Jira or Confluence API call other than an attachment download must finish within 60 seconds and return at most 32 MiB, and a larger response returns `response_too_large`.
- Attachment downloads are limited to the inline worker transfer size, 16 MiB by default, and many real attachments are larger.
- A larger attachment returns `attachment_too_large`, whose message names the limit and the `MINDROOM_ATTACHMENT_INLINE_SAVE_MAX_BYTES` setting that raises it.
- Raising that setting also raises the inline limit for `get_attachment(..., mindroom_output_path=...)`, and MindRoom's fixed 64 MiB limit for [registered files](../attachments.md) still applies.
- A download must finish within 120 seconds, including every redirect.
- Page reads and attachment listings go through Confluence search, so they see only content that search has indexed.
- A page or attachment created moments ago can take a short time to appear, so `confluence_get_page` returns `page_not_found` and `confluence_list_attachments` omits the new attachment until the index catches up.
- Archived and draft pages are not returned.
- `confluence_get_page` reads pages only, and the ID of a blog post or other content returns `not_a_page` with its `content_type`.
- Only Atlassian Cloud is supported; Jira and Confluence Data Center use the existing `jira` and `confluence` tools.
- The tool does not upload attachments, delete content, or manage spaces and projects.
- `confluence_update_page` replaces the whole body and needs the page's current version number plus one from `confluence_get_page`.

## Related Docs

- [OAuth Integration Framework](../oauth-framework.md)
- [Project Management](project-management.md)
- [Plugins](../plugins.md)
- [Matrix & Attachments](matrix-and-attachments.md)
