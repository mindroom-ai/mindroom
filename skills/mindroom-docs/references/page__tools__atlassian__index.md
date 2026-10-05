# Atlassian Cloud

The `atlassian` tool searches, reads, and updates Jira and Confluence Cloud as the person who asked.
Each requester connects their own Atlassian account through OAuth 2.0 (3LO), so agents see and change only what that person can in Atlassian.
Additional Atlassian sites can be added as separate, independently connected tools through a small plugin.
OAuth connection mechanics shared with other providers are on [OAuth Integration Framework](https://docs.mindroom.chat/oauth-framework/).

<video controls playsinline preload="metadata" aria-label="The agent answers a travel policy question from Confluence and Drive, with sources" style="width: 100%" poster="https://github.com/user-attachments/assets/01040947-ffd8-4649-a7bd-baf96130d1a7" data-poster-light="https://github.com/user-attachments/assets/01040947-ffd8-4649-a7bd-baf96130d1a7" data-poster-dark="https://github.com/user-attachments/assets/95022cb5-336c-4abc-93e3-430d7dd8eb07">
  <source src="https://github.com/user-attachments/assets/e0f957e4-d610-4408-b30b-87b9e553ee53" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/a5a4917a-a373-499e-95d2-e8c30f354368" type="video/mp4">
</video>

The separate `jira` and `confluence` tools on [Project Management](https://docs.mindroom.chat/tools/project-management/) use a shared API token or password and also support Jira and Confluence Data Center.
Their function names do not collide with the `atlassian` functions, so an agent can have both.

## What It Does

| Function | Product | Changes data | Purpose |
| --- | --- | --- | --- |
| `jira_search_issues` | Jira | no | Search issues with JQL, with `next_page_token` paging. |
| `jira_get_issue` | Jira | no | Read one issue's summary fields and description, or the requested `fields` (`["*all"]` for every field), with the transitions available from its current status. |
| `jira_create_issue` | Jira | yes | Create an issue from a project key, summary, issue type, optional plain-text description, and optional extra fields. |
| `jira_update_issue` | Jira | yes | Update the summary, description, or other fields of an issue. |
| `jira_add_comment` | Jira | yes | Add a plain-text comment to an issue. |
| `jira_transition_issue` | Jira | yes | Move an issue through its workflow by transition ID, transition name, or target status name. |
| `confluence_search` | Confluence | no | Search with CQL, with `cursor` paging, naming who created, owns, and last edited each result and when it was created. |
| `confluence_get_page` | Confluence | no | Read a page with its storage-format body, space, current version number, and who created, owns, and last edited it. |
| `confluence_list_attachments` | Confluence | no | List a page's attachments with ID, title, media type, size, comment, and version, with `cursor` paging. |
| `confluence_download_attachment` | Confluence | no | Download one page attachment into the conversation, see [Attachments](#attachments). |
| `confluence_create_page` | Confluence | yes | Create and publish a page in a space, optionally under a parent page. |
| `confluence_update_page` | Confluence | yes | Replace a page's title and whole body as a new version. |
| `confluence_add_comment` | Confluence | yes | Add a footer comment to a page. |

Jira descriptions and comments are plain text, with blank lines separating paragraphs.
Confluence bodies use the Confluence storage format, which is XHTML such as `<p>Hello</p>`.
`confluence_update_page` needs the page's current version number plus one, taken from `confluence_get_page`.
`jira_transition_issue` matches an exact transition ID first, then a transition name, then a target status that only one transition leads to, ignoring case for names and statuses.
When several transitions share that name or target status, it returns `transition_ambiguous` with the candidates and changes nothing.
Every result is a JSON object with `status` set to `ok` or `error`, and successful results name the `product` and the `site` that answered.

## Set Up The Atlassian App

An administrator creates one Atlassian OAuth 2.0 (3LO) app for the MindRoom installation.

1. Open the [Atlassian developer console](https://developer.atlassian.com/console/myapps/) and create an **OAuth 2.0 integration**.
2. Under **Authorization**, configure OAuth 2.0 (3LO) with the callback URL `https://mindroom.example.com/api/oauth/atlassian/callback`, replacing the origin with your public MindRoom origin.
3. Under **Permissions**, add the Jira API and the Confluence API, and enable the scopes listed in [Scopes](#scopes).
4. Under **Distribution**, enable sharing so that people other than the app owner can connect.
5. Under **Settings**, copy the client ID and secret.

For a local installation, the callback URL is `http://localhost:8765/api/oauth/atlassian/callback`.
A hosted installation must set `MINDROOM_PUBLIC_URL` or `MINDROOM_BASE_URL`, from which MindRoom derives the callback URL, and the URL registered on the app must match exactly.
MindRoom always uses this callback and ignores any `redirect_uri` stored with the app credentials.

Store the client ID and secret in the `atlassian_oauth_client` credential service through the dashboard credentials page.
For non-interactive deployments, seed it at startup with a [credential seed](https://docs.mindroom.chat/oauth-framework/#credential-seeds):

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

## Scopes

MindRoom requests only the scopes that the enabled functions call, plus `offline_access` for token refresh.
Scopes are classic wherever the API accepts them; Confluence page and comment writes accept only granular scopes.

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

A stored connection that lacks any requested scope counts as disconnected, and the tool asks the requester to connect again.
Adding a product or enabling writes therefore requires each user whose connection lacks the new scopes to reconnect once, while removing a product or disabling writes does not.

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
| `site_url` | `url` | `null` | Atlassian Cloud site as its `https://<name>.atlassian.net` URL; any path such as `/wiki` is ignored. |
| `cloud_id` | `text` | `null` | Atlassian cloud ID (a UUID) of the site; when `site_url` is also set, the site must match both. |

Both options are optional and can also be set for every agent through `defaults.tools` or the tool's dashboard settings.
A site URL that is not HTTPS or a cloud ID that is not a UUID keeps the tool from loading and logs a warning.

Which site the tool uses:

- With neither option set, Jira and Confluence calls each use the only site of that product the connected account can reach, so one Jira site and a different Confluence site work without a pin.
- When the account can reach several sites for the product being called, it returns `site_selection_required` with the available sites; set `site_url` or `cloud_id` to choose one.
- When the configured site is not reachable with the connected account, or `site_url` and `cloud_id` name different sites, it returns `site_not_found` with the `available_sites` Atlassian reports and never falls back to another site.

Atlassian reports every site by its atlassian.net URL, so a custom-domain `site_url` never matches; use the site's atlassian.net URL or its `cloud_id` instead.

## Connect An Account

When a requester has not connected Atlassian yet, every function returns `oauth_connection_required: true` with a `connect_url` for that requester.
The requester opens the link, signs in to Atlassian, approves the requested scopes, and retries the request.
Users can also connect and disconnect from the dashboard Integrations page.

Tokens belong to the requester and are never shared with other users or stored globally.
A call without a concrete requester always asks for a connection instead of using another account.
A connection that has not been used for 90 days expires, and the requester then reconnects.

## Add More Connections

Use `AtlassianConnectionConfig` to add another Atlassian site, such as a partner's Confluence, next to the default connection.
Each connection gets its own OAuth provider, stored credentials, site pin, requested scopes, and prefixed function names, and no credential ever crosses connections.

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
| `cloud_id` | `null` | Cloud ID (a UUID) of the site; when `site_url` is also set, both must name the same site. |
| `products` | `("jira", "confluence")` | Non-empty, duplicate-free products whose functions and scopes this connection enables. |
| `write` | `True` | Set to `False` to register only read functions and request only read scopes. |
| `client_config_service` | `<name>_atlassian_oauth_client` | Credential service holding the connection's app client ID and secret; the name must end in `_oauth_client`. |

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
A connection without its own client credentials stays unconfigured and never falls back to the default app.

To reuse the default app instead, set `client_config_service="atlassian_oauth_client"` and register the connection's callback URL on that app, if the developer console accepts an additional one.
Atlassian records consent per user and app, so with a shared app the default connection can also reach the other site, and an unpinned `atlassian` tool returns `site_selection_required` until it gets a `site_url` or `cloud_id`.
Revoking the shared app in Atlassian disconnects every connection that uses it.

Each connection has its own card on the Integrations page and its own connect link.

## Approvals For Writes

Use [tool approval](https://docs.mindroom.chat/tool-approval/) rules to require a Matrix approval card for the functions that change data while reads run immediately:

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

The leading `*` also matches the prefixed functions of additional connections, such as `partner_confluence_create_page`.
Under a stricter `tool_approval.default: require_approval`, add `auto_approve` rules for `*jira_search_issues`, `*jira_get_issue`, `*confluence_search`, `*confluence_get_page`, `*confluence_list_attachments`, and `*confluence_download_attachment` instead.
To give an agent read access only, hide the write functions with [`exclude_tools`](https://docs.mindroom.chat/tools/#filtering-toolkit-functions), or register a connection with `write=False` so that it never requests write scopes.

## Attachments

`confluence_list_attachments` returns attachment IDs such as `att123456`; when more results exist, it returns `has_more: true` with a `next_cursor` to pass back unchanged.

`confluence_download_attachment` stores the file in MindRoom's [attachment storage](https://docs.mindroom.chat/attachments/) for the current room and thread and returns a context attachment ID such as `att_0123456789abcdef`.
The ID is usable in the same turn, for example with `get_attachment(attachment_id)` to inspect the file, `get_attachment(attachment_id, mindroom_output_path=...)` to save it to the workspace, or `matrix_message` to send it.
In a later turn, download the attachment again.

Downloads are limited to 64 MiB, the limit for [registered files](https://docs.mindroom.chat/attachments/), which cannot be raised.
An operator can lower the download limit with [`MINDROOM_ATTACHMENT_INLINE_SAVE_MAX_BYTES`](https://docs.mindroom.chat/deployment/sandbox-proxy/#environment-variable-reference).

## Limits

- Search and listing functions return at most 50 results per call.
- Each API call other than an attachment download must finish within 60 seconds and return at most 32 MiB; an attachment download must finish within 120 seconds.
- Page reads and attachment listings go through Confluence search, so a page or attachment created moments ago can be missing briefly.
- Archived and draft pages are not returned.
- Only Atlassian Cloud is supported.
- The tool does not upload attachments, delete content, or manage spaces and projects.

## Troubleshooting

| Result | Cause and fix |
| --- | --- |
| `scope_mismatch` | Atlassian rejected a request for a scope the app does not grant; reconnecting grants the same scopes again, so an administrator must add the scopes in [Scopes](#scopes) to the app. |
| `page_not_found` | The page does not exist, is not visible to the account, or is too new for search to have indexed it. |
| `not_a_page` | `confluence_get_page` reads pages only, and the ID belongs to a blog post or other content named in `content_type`. |
| `response_too_large` | The API response exceeded the size limit; narrow the request, for example with fewer fields or a smaller limit. |
| `attachment_too_large` | The attachment exceeds the download limit in [Attachments](#attachments); the message names the limit and the setting that raises it. |
| `attachment_unavailable` | The attachment does not exist or the connected account may not view it. |
| `attachment_context_unavailable` | The call ran outside a conversation with attachment storage. |

Connection and site results are described in [Configure The Default Connection](#configure-the-default-connection) and [Connect An Account](#connect-an-account).
When Atlassian rejects the connection's access token, the result includes a reconnect link.
`permission_denied` means the connected account lacks Atlassian access to that project, space, or item, which reconnecting does not fix.
Other Atlassian errors keep Atlassian's message with URLs removed.

## Security Notes

- The tool runs in the primary MindRoom runtime, so worker containers never receive the app client configuration or user tokens.
- The tool takes no local file paths and writes no workspace files; downloads go only to MindRoom's attachment storage.
- Tokens are sent only to Atlassian's API for the pinned site, never to another host.
- Error results never contain tokens, signed download links, or raw response bodies.

## Related Docs

- [OAuth Integration Framework](https://docs.mindroom.chat/oauth-framework/)
- [Project Management](https://docs.mindroom.chat/tools/project-management/)
- [Plugins](https://docs.mindroom.chat/plugins/)
- [Matrix & Attachments](https://docs.mindroom.chat/tools/matrix-and-attachments/)
