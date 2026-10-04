# Web Scraping & Browser

This page covers the built-in tools that read web pages, extract article text, crawl sites, call hosted scraping APIs, and drive a browser.
Use it to pick a tool, configure it, and understand why a URL was refused.

## Choosing a Tool

| Tool | Use it for | Credentials |
| --- | --- | --- |
| [`website`](#website) | Reading one page quickly | None |
| [`trafilatura`](#trafilatura) | Local text, metadata, HTML-to-text, batch extraction, and small crawls | None |
| [`newspaper`](#newspaper) | News articles and blog posts with title, authors, and publish date | None |
| [`crawl4ai`](#crawl4ai) | Local browser-rendered reads of one or a few URLs, optionally focused on a query | None |
| [`jina`](#jina) | Jina Reader page reads and optional search | Optional `JINA_API_KEY` |
| [`firecrawl`](#firecrawl) | Hosted scrape, crawl, map, and search | `FIRECRAWL_API_KEY` |
| [`spider`](#spider) | Spider Cloud search, scrape, and crawl | `SPIDER_API_KEY` |
| [`scrapegraph`](#scrapegraph) | Prompt-driven structured extraction | `SGAI_API_KEY` |
| [`apify`](#apify) | Running an Apify Actor as a tool | `APIFY_API_TOKEN` |
| [`brightdata`](#brightdata) | Markdown scraping, screenshots, SERP queries, and data feeds | `BRIGHT_DATA_API_KEY` |
| [`oxylabs`](#oxylabs) | Google SERP, Amazon product data, and generic scraping | `OXYLABS_USERNAME` and `OXYLABS_PASSWORD` |
| [`agentql`](#agentql) | AgentQL query extraction in a local browser | `AGENTQL_API_KEY` |
| [`browserbase`](#browserbase) | Hosted remote browser sessions for navigation, screenshots, and page reads | `BROWSERBASE_API_KEY` and `BROWSERBASE_PROJECT_ID` |
| [`browser`](#browser) | Multi-step automation of MindRoom's browser or the user's own signed-in browser | None |
| [`web_browser_tools`](#web_browser_tools) | Opening a URL in the host computer's browser for a person | None |

Each credential can be stored as the tool's `api_key` (or `apify_api_token`, `username`, and `password`) through the dashboard or credential store, or supplied as the environment variable shown.
Keep password fields out of inline YAML.
`crawl4ai`, `agentql`, `browserbase`, and `browser` need a working Playwright browser runtime.
For a visible, persistent browser that the user can watch or control in MindRoom Chat, see [Worker Computer](https://docs.mindroom.chat/tools/worker-computer/) and [Agent Chat UI Actions](https://docs.mindroom.chat/tools/chat-ui/).

## Network Access

Tools that fetch pages from MindRoom itself (`website`, `trafilatura`, `newspaper`, `crawl4ai`, `agentql`, and the `browser` host target) reach only public HTTP(S) addresses.
Private, loopback, link-local, multicast, reserved, and cloud-metadata targets are refused, and so are redirects, crawled links, and page resources that lead to them.
Unless a worker egress proxy is set, DNS answers that change after a URL is checked cannot redirect the connection to an internal service.
Only the `browser` tool can opt into private networks, with [`allow_private_networks`](#browser-configuration).
In Computer mode, the `browser` tool also opens worker-local loopback previews such as `http://localhost:5173` without that option; see [Preview a local web app](https://docs.mindroom.chat/tools/worker-computer/#preview-a-local-web-app).

### Egress Proxy for Browsers

`crawl4ai`, `agentql`, the `browser` host target, and [`browser_mcp`](https://docs.mindroom.chat/tools/worker-computer/#native-playwright-mcp-provider) in a Worker Computer send every connection through an operator egress proxy set with `http_proxy`, `https_proxy`, or `all_proxy`, using HTTP `CONNECT`.
In the primary process, `http_proxy` applies to port 80, `https_proxy` to every other port, and `all_proxy` to either when its own variable is unset.
The proxy must allow `CONNECT` to ports 80 and 443, because plain-HTTP pages are tunneled too; Squid's default `http_access deny CONNECT !SSL_ports` rule refuses plain HTTP.
In the primary process it must also allow `CONNECT` to IP addresses, so proxies that allow only hostnames are unsupported there.
Loopback destinations are always dialed directly, and `no_proxy` is honored only when `allow_private_networks` is enabled; it never makes a refused destination reachable.
A refused tunnel fails that connection without a direct fallback and logs `browser_egress_proxy_refused_tunnel`.
The primary process logs a warning and dials directly when the proxy is SOCKS, has credentials or a path in its URL, or is set through `auto_proxy` or `socks_server`.
In a worker, every set proxy variable must name the same HTTP(S) proxy, or the browser does not start; hostnames must also resolve in the worker's DNS.
That proxy receives hostnames and resolves them itself, so it must block private and metadata addresses and resist DNS rebinding.
WebRTC cannot send UDP from these browsers.

## No-Key Scrapers

### [`website`]

`website` exposes `read_url(url)` and returns JSON page documents from MindRoom's WebsiteReader variant, which drops search UI, navigation, headers, footers, sidebars, hidden content, and modals before choosing the page text.
Pages larger than 2 MiB fail, as do pages a server sends compressed.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `knowledge` | `object` | `null` | Programmatic `Knowledge` object only; it replaces `read_url()` with `add_website_to_knowledge(url)`. |

```yaml
agents:
  assistant:
    tools:
      - website
```

### [`trafilatura`]

`trafilatura` exposes `extract_text()`, `extract_metadata_only()`, `crawl_website()`, `html_to_text()`, and `extract_batch()`.
`extract_batch()` returns one JSON payload listing successes and failures.
`crawl_website()` stops with an error when it reaches a refused target, while `extract_content=True` reports each refused page individually.
A server that sends a compressed body fails the download.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `output_format` | `text` | `txt` | `txt`, `json`, `markdown`, `xml`, `csv`, or `html`. |
| `include_comments` | `boolean` | `true` | Include comments. |
| `include_tables` | `boolean` | `true` | Include tables. |
| `include_images` | `boolean` | `false` | Include image information where supported. |
| `include_formatting` | `boolean` | `false` | Preserve formatting markers. |
| `include_links` | `boolean` | `false` | Preserve links. |
| `with_metadata` | `boolean` | `false` | Include metadata in extraction output. |
| `favor_precision` | `boolean` | `false` | Bias extraction toward precision. |
| `favor_recall` | `boolean` | `false` | Bias extraction toward recall. |
| `target_language` | `text` | `null` | ISO 639-1 language filter such as `en`. |
| `deduplicate` | `boolean` | `false` | Remove repeated segments. |
| `max_tree_size` | `number` | `null` | Parser tree-size limit. |
| `max_crawl_urls` | `number` | `10` | Maximum URLs visited per crawl. |
| `max_known_urls` | `number` | `100000` | Maximum discovered URLs tracked per crawl. |
| `enable_extract_text` | `boolean` | `true` | Enable `extract_text()`. |
| `enable_extract_metadata_only` | `boolean` | `true` | Enable `extract_metadata_only()`. |
| `enable_html_to_text` | `boolean` | `true` | Enable `html_to_text()`. |
| `enable_extract_batch` | `boolean` | `true` | Enable `extract_batch()`. |
| `enable_crawl_website` | `boolean` | `true` | Enable `crawl_website()`. |
| `all` | `boolean` | `false` | Enable every function. |

```yaml
agents:
  analyst:
    tools:
      - trafilatura:
          output_format: markdown
          with_metadata: true
          include_links: true
```

### [`newspaper`]

`newspaper` exposes `read_article(url)` and returns JSON with whichever of title, authors, text, and publish date were extracted.
It is tuned for article pages, not arbitrary sites, and returns no image fields.
Use `newspaper` in `tools:`, not `newspaper4k`.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `article_length` | `number` | `null` | Truncate article text to this many characters. |
| `enable_read_article` | `boolean` | `true` | Enable `read_article()`. |
| `all` | `boolean` | `false` | Enable every function. |

### [`crawl4ai`]

`crawl4ai` exposes `crawl(url, search_query=None)`, where `url` is one URL or a list, and returns readable text for each page from a local headless browser.
With `search_query`, the text is filtered toward that query; without one, `use_pruning` trims noisy content.
Results skip Crawl4AI's cache and are truncated to `max_length`.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `max_length` | `number` | `5000` | Maximum returned characters. |
| `timeout` | `number` | `60` | Crawl timeout in seconds. |
| `use_pruning` | `boolean` | `false` | Prune noisy content when no `search_query` is given. |
| `pruning_threshold` | `number` | `0.48` | Pruning threshold. |
| `bm25_threshold` | `number` | `1.0` | Query-filter threshold when `search_query` is given. |
| `headless` | `boolean` | `true` | Run the browser headless. |
| `wait_until` | `text` | `domcontentloaded` | Playwright wait condition before extraction. |
| `enable_crawl` | `boolean` | `true` | Enable `crawl()`. |
| `all` | `boolean` | `false` | Enable every function. |

```yaml
agents:
  researcher:
    tools:
      - crawl4ai:
          max_length: 8000
          use_pruning: true
          wait_until: networkidle
```

### [`jina`]

`jina` exposes `read_url(url)` through Jina Reader and, when enabled, `search_query(query)`.
It works without a key for public reads; a key helps with paid plans and rate limits.
Returned content is truncated to `max_content_length`.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Optional; falls back to `JINA_API_KEY`. |
| `base_url` | `url` | `https://r.jina.ai/` | Reader endpoint for `read_url()`. |
| `search_url` | `url` | `https://s.jina.ai/` | Search endpoint for `search_query()`. |
| `max_content_length` | `number` | `10000` | Maximum returned characters. |
| `timeout` | `number` | `null` | Jina timeout in seconds. |
| `search_query_content` | `boolean` | `true` | Include full page content in search results; `false` returns summaries only. |
| `enable_read_url` | `boolean` | `true` | Enable `read_url()`. |
| `enable_search_query` | `boolean` | `false` | Enable `search_query()`. |
| `all` | `boolean` | `false` | Enable every function. |

## Hosted Scraping APIs

### [`firecrawl`]

`firecrawl` exposes `scrape_website()`, `crawl_website()`, `map_website()`, and `search_web()`.
It requires `api_key` or `FIRECRAWL_API_KEY`.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Falls back to `FIRECRAWL_API_KEY`. |
| `enable_scrape` | `boolean` | `true` | Enable `scrape_website()`. |
| `enable_crawl` | `boolean` | `false` | Enable `crawl_website()`. |
| `enable_mapping` | `boolean` | `false` | Enable `map_website()`. |
| `enable_search` | `boolean` | `false` | Enable `search_web()`. |
| `all` | `boolean` | `false` | Enable every function. |
| `formats` | `string[]` | `null` | Output formats for scrape, crawl, and search, such as `markdown` or `html`; check them against your plan. |
| `limit` | `number` | `10` | Default result cap for crawl and search. |
| `poll_interval` | `number` | `30` | Seconds between crawl status checks. |
| `api_url` | `url` | `https://api.firecrawl.dev` | Firecrawl API base URL. |

```yaml
agents:
  research:
    tools:
      - firecrawl:
          enable_crawl: true
          enable_search: true
          limit: 5
```

### [`spider`]

`spider` exposes `search_web(query, max_results=5)`, `scrape(url)`, and `crawl(url, limit=None)`, returning Markdown.
Search results do not include full page content.
It requires `SPIDER_API_KEY` in the environment even though the dashboard lists it as needing no setup, and it has no `api_key` field.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `max_results` | `number` | `null` | Default result count for `search_web()`. |
| `url` | `url` | `null` | Unused; `scrape()` and `crawl()` always take the URL as an argument. |
| `enable_search` | `boolean` | `true` | Enable `search_web()`. |
| `enable_scrape` | `boolean` | `true` | Enable `scrape()`. |
| `enable_crawl` | `boolean` | `true` | Enable `crawl()`. |
| `all` | `boolean` | `false` | Enable every function. |

### [`scrapegraph`]

`scrapegraph` turns pages into structured answers from natural-language prompts.
`smartscraper(url, prompt)` extracts data from one page, `markdownify()` converts a page to Markdown, `crawl()` applies a prompt and JSON schema across a crawl, `searchscraper()` searches the web before extracting, and `scrape()` returns raw HTML.
It requires `api_key` or `SGAI_API_KEY`.
Keep at least one function enabled, or set `all: true`.
The upstream `headers` option is not supported in `config.yaml`.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Falls back to `SGAI_API_KEY`. |
| `enable_smartscraper` | `boolean` | `true` | Enable `smartscraper()`. |
| `enable_markdownify` | `boolean` | `false` | Enable `markdownify()`. |
| `enable_crawl` | `boolean` | `false` | Enable `crawl()`. |
| `enable_searchscraper` | `boolean` | `false` | Enable `searchscraper()`. |
| `enable_scrape` | `boolean` | `false` | Enable `scrape()`. |
| `render_heavy_js` | `boolean` | `false` | Render heavy JavaScript in `scrape()` only. |
| `crawl_poll_interval` | `number` | `3` | Seconds between crawl status checks. |
| `crawl_max_wait` | `number` | `180` | Maximum seconds to wait for a crawl. |
| `all` | `boolean` | `false` | Enable every function. |

### [`apify`]

`apify` registers one tool function per configured Apify Actor, with parameters from the Actor's input schema, and returns the Actor's dataset items as JSON.
Without `actors`, it provides no functions.
Function names derive from the Actor ID, so check the agent's tool list for the exact name.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `apify_api_token` | `password` | `null` | Falls back to `APIFY_API_TOKEN`. |
| `actors` | `text` | `null` | One Actor ID, such as `apify/rag-web-browser`; a comma-separated list is treated as a single ID. |

```yaml
agents:
  extractor:
    tools:
      - apify:
          actors: apify/rag-web-browser
```

### [`brightdata`]

`brightdata` exposes `scrape_as_markdown()`, `get_screenshot()`, `search_engine()` for Google, Bing, and Yandex, and `web_data_feed()` for Bright Data's supported source types.
`get_screenshot()` returns the image directly to the model rather than a file path.
It requires `api_key` or `BRIGHT_DATA_API_KEY`.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Falls back to `BRIGHT_DATA_API_KEY`. |
| `enable_scrape_markdown` | `boolean` | `true` | Enable `scrape_as_markdown()`. |
| `enable_screenshot` | `boolean` | `true` | Enable `get_screenshot()`. |
| `enable_search_engine` | `boolean` | `true` | Enable `search_engine()`. |
| `enable_web_data_feed` | `boolean` | `true` | Enable `web_data_feed()`. |
| `all` | `boolean` | `false` | Enable every function. |
| `serp_zone` | `text` | `serp_api` | SERP zone; `BRIGHT_DATA_SERP_ZONE` overrides it. |
| `web_unlocker_zone` | `text` | `web_unlocker1` | Web unlocker zone; `BRIGHT_DATA_WEB_UNLOCKER_ZONE` overrides it. |
| `verbose` | `boolean` | `false` | Log extra request detail. |
| `timeout` | `number` | `600` | Timeout in seconds. |

### [`oxylabs`]

`oxylabs` exposes `search_google()`, `get_amazon_product()`, `search_amazon_products()`, and `scrape_website()`.
`search_google()` returns organic results with title, URL, description, and position.
Pass `domain_code`, such as `com` or `de`, to choose the regional Google or Amazon domain.
It requires both a username and password.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `username` | `text` | `null` | Falls back to `OXYLABS_USERNAME`. |
| `password` | `password` | `null` | Falls back to `OXYLABS_PASSWORD`. |
| `markdown` | `boolean` | `false` | Return `scrape_website()` content as Markdown instead of parsed HTML. |

## Browser Tools

### [`agentql`]

`agentql` exposes `scrape_website(url)`, which extracts generic page text, and `custom_scrape_website(url)`, which runs your `agentql_query` and returns the extracted values as JSON.
Setting `agentql_query` registers `custom_scrape_website()` even when `enable_custom_scrape_website` is `false`.
It opens only HTTP(S) URLs and launches a visible browser window, so it needs a GUI-capable runtime or virtual display.
It requires `api_key` or `AGENTQL_API_KEY`; AgentQL SDK settings and CLI credential files do not override that key.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Falls back to `AGENTQL_API_KEY`. |
| `enable_scrape_website` | `boolean` | `true` | Enable `scrape_website()`. |
| `enable_custom_scrape_website` | `boolean` | `false` | Enable `custom_scrape_website()`. |
| `all` | `boolean` | `false` | Enable every function. |
| `agentql_query` | `text` | `""` | AgentQL query for `custom_scrape_website()`. |

```yaml
agents:
  extractor:
    tools:
      - agentql:
          agentql_query: |
            {
              title
              links[]
            }
```

### [`browserbase`]

`browserbase` exposes `navigate_to()`, `screenshot()`, `get_page_content()`, and `close_session()` against a hosted Browserbase session that it creates automatically.
It still needs local Playwright to connect to the remote browser.
It requires an API key and project ID.
Use it when you need remote navigation, screenshots, and page reads without the full action set of `browser`.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Falls back to `BROWSERBASE_API_KEY`. |
| `project_id` | `text` | `null` | Falls back to `BROWSERBASE_PROJECT_ID`. |
| `base_url` | `url` | `null` | Browserbase API endpoint, not the site to visit; falls back to `BROWSERBASE_BASE_URL`. |
| `enable_navigate_to` | `boolean` | `true` | Enable `navigate_to()`. |
| `enable_screenshot` | `boolean` | `true` | Enable `screenshot()`. |
| `enable_get_page_content` | `boolean` | `true` | Enable `get_page_content()`. |
| `enable_close_session` | `boolean` | `true` | Enable `close_session()`. |
| `all` | `boolean` | `false` | Enable every function. |
| `parse_html` | `boolean` | `true` | Return visible text instead of raw HTML. |
| `max_content_length` | `number` | `100000` | Maximum returned characters of page content. |

### [`browser`]

`browser` exposes one function, `browser_control(action=...)`, for multi-step browser sessions.
It can drive two targets:

- `target="host"` (default) controls MindRoom's own Chromium on the MindRoom host, or in the agent's worker when `browser` is listed in `worker_tools`.
- `target="desktop"` controls the user's own signed-in Chrome or Brave profile through the [Matrix Desktop Bridge](https://docs.mindroom.chat/tools/desktop/), which owns extension installation, the local control lease, and the trust model.

Call `action="help"` or `action="actions"` to list the actions and `act` request kinds.

| Action | Host | Desktop |
| --- | --- | --- |
| `status`, `start`, `stop`, `profiles`, `tabs`, `open`, `snapshot`, `screenshot`, `console` | Yes | Yes |
| `focus`, `close`, `navigate`, `pdf`, `upload`, `dialog`, `act` | Yes | No |
| `help`, `actions` | Yes | Yes |

On the host target, `snapshot()` returns `ai` or `aria` format with element refs that later `act()` and `screenshot()` calls can use.
`act()` takes `request.kind` set to `click`, `type`, `press`, `hover`, `drag`, `select`, `fill`, `resize`, `wait`, `evaluate`, or `close`.
The desktop target returns the browser's native accessibility snapshot and rejects `targetId` and host-only options such as `profile`, snapshot format hints, `inputRef`, and `timeoutMs`.
Desktop-target calls always run in the primary process, so routing `browser` to a worker isolates only host-target calls.
To show the worker browser to the user, see [Agent Chat UI Actions](https://docs.mindroom.chat/tools/chat-ui/#control-the-browser-then-show-it).

#### Screenshots and Files

Screenshots are shown to the model by default; host screenshots are also saved, and `saveOnly=True` saves without showing.
Large images may be resized before the model sees them.
Model-visible screenshots can be kept in the agent's session history.
On the desktop target, `returnAttachment=True` with `action="screenshot"` also returns an `att_*` handle, valid until the turn ends, that `matrix_message` can send.

Host screenshots, PDFs, and other artifacts go to `output_dir`.
In the primary process it defaults to `browser/` in the agent's state root: `<storage>/agents/<agent>/browser` for a shared agent, or the requester's private-instance root for a private agent.
In a worker it defaults to `browser/` in the worker workspace, and a custom `output_dir` must stay inside that workspace.
`output_dir` must not point at runtime state, and it does not affect the desktop target.

Which files `upload` may read follows the agent's [`file_access`](https://docs.mindroom.chat/architecture/security-posture/#file-access) setting.
With the default `workspace`, `upload` accepts files in the artifact directory, files in the agent's workspace, and `att_*` IDs of attachments in the current conversation; use `./` for a workspace file whose name starts with `att_`.
Everything else is refused, including credentials, encryption keys, Matrix state, sessions, and other agents' workspaces; in a worker only the worker workspace is readable.
With `unrestricted`, `upload` accepts any file its process can read.
One browser holds at most 256 MiB of uploaded files until their tabs close, so a larger upload is refused until a tab is closed.

On the host target, `open` also takes one HTML file in `paths` instead of `targetUrl`, read under the same rules as `upload`, so an agent can check a page it wrote with `screenshot`, `console`, and `act`.
The file is UTF-8 text of at most 16 MiB.
The page cannot load or navigate to other local files, and relative links in it do not resolve; its network requests follow the same checks as any page.

```python
browser_control(action="open", paths=["slides/deck.html"])
browser_control(action="screenshot")
```

#### Profiles and Signed-In Sessions

Host-target profiles are named, with `mindroom` as the default; names starting with a dot are rejected.
In the primary process a profile lives at `browser-profiles/<profile>` under the agent's state root, so every requester of a shared agent shares its signed-in sessions, while different agents never share them.
This holds even with `worker_scope: user` or `user_agent` unless `browser` is in `worker_tools`, which gives each worker scope its own profiles.
Use a private agent when requesters need separate browser sessions.
Chromium is taken from `BROWSER_EXECUTABLE_PATH`, `chromium`, or `google-chrome-stable`.

#### Browser Configuration

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `output_dir` | `text` | `null` | Host-target artifact directory; defaults to `browser/` in the agent's state root, or in the worker workspace when routed to a worker. |
| `allow_private_networks` | `boolean` | `false` | Let the host target, and URLs passed to desktop-target `open`, reach trusted private and loopback addresses; metadata and link-local addresses stay blocked. Pages on the desktop target keep the profile's normal network access either way. |
| `default_target` | `select` | `host` | `host` or `desktop`. |
| `device_user_id` | `text` | `null` | Required for the desktop target: Matrix user the desktop bridge signs in as. |
| `device_id` | `text` | `null` | Required for the desktop target: device ID printed by `mindroom desktop login`. |
| `device_ed25519` | `text` | `null` | Required for the desktop target: Ed25519 fingerprint of that device. |
| `timeout_seconds` | `number` | `90` | Desktop-target timeout, from 1 to 120 seconds. |

```yaml
agents:
  browser_worker:
    tools:
      - browser:
          default_target: desktop
          device_user_id: "@my-laptop:example.org"
          device_id: "ABCDEFGHIJ"
          device_ed25519: "desktop-device-fingerprint"
```

```python
browser_control(action="start", target="desktop")
browser_control(action="open", target="desktop", targetUrl="https://matrix.org/blog/")
browser_control(action="screenshot", target="desktop", fullPage=True, returnAttachment=True)
```

### [`web_browser_tools`]

`web_browser_tools` exposes `open_page(url, new_window=False)`, which opens an `http` or `https` URL in a tab or window of the host computer's browser for a person to use.
It refuses `file:` paths, other schemes, and scheme-less strings, and returns no page content, so it is not a scraper.
It only works on a host with a desktop browser.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `enable_open_page` | `boolean` | `true` | Enable `open_page()`. |
| `all` | `boolean` | `false` | Enable every function. |

## Related Docs

- [Tools Overview](https://docs.mindroom.chat/tools/)
- [Per-Agent Tool Configuration](https://docs.mindroom.chat/tools/#per-agent-tool-configuration)
