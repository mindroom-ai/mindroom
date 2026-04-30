# Web Search

Use these tools to search the public web, query paid search APIs, access Google- or Baidu-oriented results, or point agents at a self-hosted SearXNG instance.

## What This Page Covers

This page documents the built-in tools in the `web-search` group.
Use these tools when you need general web discovery, current-events search, answer-style search APIs, Google or Baidu specific results, or a self-hosted metasearch backend.

## Tools On This Page

- \[`duckduckgo`\] - No-key DuckDuckGo-backed web and news search through the shared DDGS backend.
- \[`googlesearch`\] - No-key Google-backed web and news search through the shared DDGS backend.
- \[`baidusearch`\] - No-key Baidu search tuned for Chinese-language discovery.
- \[`tavily`\] - API-backed current-information search with optional answer, context, and URL extraction modes.
- \[`exa`\] - API-backed research search with content fetching, similar-page lookup, answers, and deep research tasks.
- \[`serpapi`\] - API-backed Google and YouTube SERP access.
- \[`serper`\] - API-backed Google web, news, and scholar search plus lightweight webpage scraping.
- \[`searxng`\] - Self-hosted SearXNG search across web, images, maps, music, science, news, and video.
- \[`linkup`\] - API-backed web search that can return either raw search results or sourced answers.

## Common Setup Notes

`duckduckgo`, `googlesearch`, and `baidusearch` are `setup_type: none`, so they work out of the box once their optional Python dependencies are available.
`tavily`, `exa`, `serpapi`, `serper`, and `linkup` are `status=requires_config` and are intended to be configured with a stored `api_key`.
`searxng` is also `status=requires_config`, but it needs a reachable `host` URL instead of an API key.
None of the tools on this page declare an `auth_provider`, and `src/mindroom/api/integrations.py` currently only exposes Spotify OAuth routes, so these tools use ordinary tool credentials or SDK environment variables rather than a dedicated dashboard OAuth flow.
Password fields such as `api_key` should be stored through the dashboard or credential store instead of inline YAML.
Current upstream SDKs also support environment variables such as `TAVILY_API_KEY`, `TAVILY_API_BASE_URL`, `EXA_API_KEY`, `SERP_API_KEY`, `SERPER_API_KEY`, and `LINKUP_API_KEY`.
Missing optional dependencies can auto-install at first use unless `MINDROOM_NO_AUTO_INSTALL_TOOLS=1` is set.
`duckduckgo` and `googlesearch` are the simplest no-key defaults for general search and basic news lookups.
`baidusearch` is the better fit when you want Baidu indexing or Chinese-language defaults.
`tavily` and `linkup` are useful when you want answer-oriented search output instead of only result lists.
`exa` is the deepest research option on this page when you need domain filters, date filters, content fetches, find-similar, or a long-running research task.
`serpapi` and `serper` are Google-focused paid APIs, with `serpapi` covering Google and YouTube verticals and `serper` covering Google web, news, scholar, and a scrape endpoint.
`searxng` is the best fit when you control your own search stack or want SearXNG categories such as images, maps, music, science, and video.

## \[`duckduckgo`\]

`duckduckgo` is the simplest built-in web search option for general search and news without any API key setup.

### What It Does

`duckduckgo` wraps Agno's `DuckDuckGoTools`, which is a convenience layer over the shared `WebSearchTools` backend with `backend="duckduckgo"`.
It exposes `web_search(query, max_results=5)` and `search_news(query, max_results=5)`.
`modifier` prepends extra query text, `fixed_max_results` caps all calls, and `proxy`, `timeout`, and `verify_ssl` control the underlying DDGS client.
The tool returns JSON strings from DDGS rather than a MindRoom-specific normalized response format.

### Configuration

| Option              | Type      | Required | Default | Notes                                             |
| ------------------- | --------- | -------- | ------- | ------------------------------------------------- |
| `enable_search`     | `boolean` | `no`     | `true`  | Enable `web_search()`.                            |
| `enable_news`       | `boolean` | `no`     | `true`  | Enable `search_news()`.                           |
| `modifier`          | `text`    | `no`     | `null`  | Prepends fixed query text to every web search.    |
| `fixed_max_results` | `number`  | `no`     | `null`  | Caps result count for both web and news searches. |
| `proxy`             | `url`     | `no`     | `null`  | Optional proxy for DDGS requests.                 |
| `timeout`           | `number`  | `no`     | `10`    | Request timeout in seconds.                       |
| `verify_ssl`        | `boolean` | `no`     | `true`  | Verify TLS certificates for DDGS requests.        |

### Example

```
agents:
  researcher:
    tools:
      - duckduckgo:
          enable_news: true
          fixed_max_results: 8
```

```
web_search("latest Matrix client features", max_results=5)
search_news("Matrix ecosystem", max_results=5)
```

### Notes

- Pick `duckduckgo` when you want the lowest-friction no-key option for general web and news search.
- Pick `googlesearch` instead when you want Google-style ranking but still do not want a paid API.
- Pick `tavily`, `exa`, `serper`, or `serpapi` when you need provider-backed APIs, answer generation, or more vertical-specific search behavior.

## \[`googlesearch`\]

`googlesearch` uses the same DDGS-powered search surface as `duckduckgo`, but it hardwires the backend to Google.

### What It Does

MindRoom registers `googlesearch` as a custom wrapper around Agno's `WebSearchTools` with `backend="google"`.
It exposes `web_search(query, max_results=5)` and `search_news(query, max_results=5)`.
Runtime behavior matches the `duckduckgo` tool surface, including `modifier`, `fixed_max_results`, `proxy`, `timeout`, and `verify_ssl`.
This is still a DDGS-backed scraper-style search path rather than an official Google paid search API.

### Configuration

| Option              | Type      | Required | Default | Notes                                                                                                              |
| ------------------- | --------- | -------- | ------- | ------------------------------------------------------------------------------------------------------------------ |
| `enable_search`     | `boolean` | `no`     | `true`  | Enable `web_search()`. The current registry metadata marks this field as text, but the wrapper expects a boolean.  |
| `enable_news`       | `boolean` | `no`     | `true`  | Enable `search_news()`. The current registry metadata marks this field as text, but the wrapper expects a boolean. |
| `modifier`          | `text`    | `no`     | `null`  | Prepends fixed query text to every web search.                                                                     |
| `fixed_max_results` | `number`  | `no`     | `null`  | Caps result count for both web and news searches. The current registry metadata marks this field as text.          |
| `proxy`             | `url`     | `no`     | `null`  | Optional proxy for DDGS requests. The current registry metadata marks this field as text.                          |
| `timeout`           | `number`  | `no`     | `10`    | Request timeout in seconds. The current registry metadata marks this field as text.                                |
| `verify_ssl`        | `boolean` | `no`     | `true`  | Verify TLS certificates for DDGS requests. The current registry metadata marks this field as text.                 |

### Example

```
agents:
  researcher:
    tools:
      - googlesearch:
          modifier: site:docs.mindroom.chat
          fixed_max_results: 6
```

```
web_search("MindRoom Matrix threads", max_results=5)
search_news("open source Matrix news", max_results=5)
```

### Notes

- Pick `googlesearch` when you want Google-backed ranking without introducing an API key dependency.
- If you need a first-party paid Google SERP API with more predictable structure, use `serper` or `serpapi` instead.
- The current MindRoom wrapper makes this tool available without dedicated dashboard integration or OAuth.

## \[`baidusearch`\]

`baidusearch` is the Baidu-specific search tool for Chinese-language search and Baidu-indexed results.

### What It Does

`baidusearch` exposes one method, `baidu_search(query, max_results=5, language="zh")`.
`fixed_language` overrides the per-call `language`, and non-two-letter language values are normalized through `pycountry` when possible.
If language normalization fails, the upstream tool falls back to `zh`.
The returned payload is a JSON array with `title`, `url`, `abstract`, and `rank`.

### Configuration

| Option                | Type      | Required | Default | Notes                                                                                                                             |
| --------------------- | --------- | -------- | ------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `fixed_max_results`   | `number`  | `no`     | `null`  | Caps result count for every call.                                                                                                 |
| `fixed_language`      | `text`    | `no`     | `null`  | Forces a default search language, with `zh` as the upstream fallback.                                                             |
| `headers`             | `text`    | `no`     | `null`  | Exposed in MindRoom metadata, but the current installed upstream call path on this branch does not pass it through to `search()`. |
| `proxy`               | `url`     | `no`     | `null`  | Exposed in MindRoom metadata, but the current installed upstream call path on this branch does not pass it through to `search()`. |
| `timeout`             | `number`  | `no`     | `10`    | Exposed in MindRoom metadata, but the current installed upstream call path on this branch does not pass it through to `search()`. |
| `debug`               | `boolean` | `no`     | `false` | Exposed in MindRoom metadata, but the current installed upstream call path on this branch does not pass it through to `search()`. |
| `enable_baidu_search` | `boolean` | `no`     | `true`  | Enable `baidu_search()`.                                                                                                          |
| `all`                 | `boolean` | `no`     | `false` | Enable the full upstream toolkit surface.                                                                                         |

### Example

```
agents:
  cn_research:
    tools:
      - baidusearch:
          fixed_language: zh
          fixed_max_results: 8
```

```
baidu_search("Matrix 协议 新闻", max_results=5, language="zh")
```

### Notes

- Pick `baidusearch` when Chinese-language search quality matters more than Google-style ranking.
- Use `duckduckgo` or `googlesearch` for simpler English-centric general search defaults.
- The current installed upstream `baidu_search()` path only forwards keyword and result count, so `headers`, `proxy`, `timeout`, and `debug` are best treated as placeholders until the wrapper or upstream call path is tightened.

## \[`tavily`\]

`tavily` is the built-in current-information search API with optional context mode and URL extraction.

### What It Does

`tavily` can expose `web_search_using_tavily(query, max_results=5)`, `web_search_with_tavily(query)`, and `extract_url_content(urls)`, depending on the enable flags.
`enable_search_context` switches the search surface from the normal result-list call to the context-oriented call, so you get one search method or the other instead of both.
`web_search_using_tavily()` can include an AI-generated answer and returns either JSON or Markdown depending on `format`.
`extract_url_content()` accepts one URL or a comma-separated URL list and formats extracted page content as Markdown or plain text depending on `extract_format`.

### Configuration

| Option                  | Type       | Required | Default    | Notes                                                                           |
| ----------------------- | ---------- | -------- | ---------- | ------------------------------------------------------------------------------- |
| `api_key`               | `password` | `yes`    | `null`     | Tavily API key. The upstream SDK also checks `TAVILY_API_KEY`.                  |
| `api_base_url`          | `url`      | `no`     | `null`     | Optional base URL override. The upstream SDK also checks `TAVILY_API_BASE_URL`. |
| `enable_search`         | `boolean`  | `no`     | `true`     | Enable Tavily search.                                                           |
| `enable_search_context` | `boolean`  | `no`     | `false`    | Use `web_search_with_tavily()` instead of `web_search_using_tavily()`.          |
| `enable_extract`        | `boolean`  | `no`     | `false`    | Enable `extract_url_content()`.                                                 |
| `all`                   | `boolean`  | `no`     | `false`    | Enable the full upstream toolkit surface.                                       |
| `max_tokens`            | `number`   | `no`     | `6000`     | Token budget for context output and filtered result formatting.                 |
| `include_answer`        | `boolean`  | `no`     | `true`     | Include the answer field in search output when available.                       |
| `search_depth`          | `text`     | `no`     | `advanced` | Tavily search depth, currently `basic` or `advanced`.                           |
| `extract_depth`         | `text`     | `no`     | `basic`    | Tavily extract depth, currently `basic` or `advanced`.                          |
| `include_images`        | `boolean`  | `no`     | `false`    | Include images in extract responses when supported.                             |
| `include_favicon`       | `boolean`  | `no`     | `false`    | Include favicons in extract responses when supported.                           |
| `extract_timeout`       | `number`   | `no`     | `null`     | Optional extraction timeout in seconds.                                         |
| `extract_format`        | `text`     | `no`     | `markdown` | Extraction output format, currently `markdown` or `text`.                       |
| `format`                | `text`     | `no`     | `markdown` | Search output format, currently `json` or `markdown`.                           |

### Example

```
agents:
  newsdesk:
    tools:
      - tavily:
          enable_extract: true
          include_answer: true
          search_depth: advanced
          format: markdown
```

```
web_search_using_tavily("latest Matrix bridge updates", max_results=5)
extract_url_content("https://matrix.org/blog/")
```

### Notes

- Pick `tavily` when you want current-information search plus an optional synthesized answer or URL extraction in the same toolkit.
- Use `enable_search_context` when you want a compact context blob rather than a normal result list.
- If you want deeper research features such as similar-page search, date filters, and long-running structured research tasks, use `exa` instead.

## \[`exa`\]

`exa` is the research-heavy search toolkit for web search, content retrieval, similar-page discovery, answer generation, and deep research tasks.

### What It Does

`exa` can expose `search_exa(query, num_results=5, category=None)`, `get_contents(urls)`, `find_similar(url, num_results=5)`, `exa_answer(query, text=False)`, and `research(instructions, output_schema=None)`.
Search results can include title, author, published date, URL, and truncated page text.
The toolkit supports domain allowlists and denylists, crawl-date and publish-date filters, category and type filters, answer-model selection, and a separate `research_model` for long-running research tasks.
`enable_research` is off by default, so deep research is opt-in even when the rest of the toolkit is enabled.

### Configuration

| Option                 | Type       | Required | Default        | Notes                                                                                                                                  |
| ---------------------- | ---------- | -------- | -------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `enable_search`        | `boolean`  | `no`     | `true`         | Enable `search_exa()`.                                                                                                                 |
| `enable_get_contents`  | `boolean`  | `no`     | `true`         | Enable `get_contents()`.                                                                                                               |
| `enable_find_similar`  | `boolean`  | `no`     | `true`         | Enable `find_similar()`.                                                                                                               |
| `enable_answer`        | `boolean`  | `no`     | `true`         | Enable `exa_answer()`.                                                                                                                 |
| `enable_research`      | `boolean`  | `no`     | `false`        | Enable `research()`.                                                                                                                   |
| `all`                  | `boolean`  | `no`     | `false`        | Enable the full upstream toolkit surface.                                                                                              |
| `text`                 | `boolean`  | `no`     | `true`         | Include page text in results.                                                                                                          |
| `text_length_limit`    | `number`   | `no`     | `1000`         | Maximum text length per result.                                                                                                        |
| `summary`              | `boolean`  | `no`     | `false`        | Request result summaries where supported.                                                                                              |
| `api_key`              | `password` | `yes`    | `null`         | Exa API key. The upstream SDK also checks `EXA_API_KEY`.                                                                               |
| `num_results`          | `number`   | `no`     | `null`         | Default result count override.                                                                                                         |
| `livecrawl`            | `text`     | `no`     | `always`       | Exposed in MindRoom metadata, but the current installed upstream call path on this branch does not pass it through to search requests. |
| `start_crawl_date`     | `text`     | `no`     | `null`         | Include results crawled on or after this date.                                                                                         |
| `end_crawl_date`       | `text`     | `no`     | `null`         | Include results crawled on or before this date.                                                                                        |
| `start_published_date` | `text`     | `no`     | `null`         | Include results published on or after this date.                                                                                       |
| `end_published_date`   | `text`     | `no`     | `null`         | Include results published on or before this date.                                                                                      |
| `type`                 | `text`     | `no`     | `null`         | Optional content type filter such as article, blog, or video.                                                                          |
| `category`             | `text`     | `no`     | `null`         | Optional category filter such as `news`, `github`, or `research paper`.                                                                |
| `include_domains`      | `string[]` | `no`     | `null`         | Domain allowlist. The current registry metadata exposes this as a text field, but runtime expects a list of domains.                   |
| `exclude_domains`      | `string[]` | `no`     | `null`         | Domain denylist. The current registry metadata exposes this as a text field, but runtime expects a list of domains.                    |
| `show_results`         | `boolean`  | `no`     | `false`        | Emit debug logs with raw parsed results.                                                                                               |
| `model`                | `text`     | `no`     | `null`         | Answer model for `exa_answer()`, currently `exa` or `exa-pro`.                                                                         |
| `timeout`              | `number`   | `no`     | `30`           | Timeout in seconds for API operations.                                                                                                 |
| `research_model`       | `text`     | `no`     | `exa-research` | Model for `research()`, currently `exa-research` or `exa-research-pro`.                                                                |

### Example

```
agents:
  analyst:
    tools:
      - exa:
          enable_research: true
          category: news
          include_domains:
            - matrix.org
            - element.io
          research_model: exa-research
```

```
search_exa("Matrix sliding sync adoption", num_results=5)
find_similar("https://matrix.org/blog/")
exa_answer("What changed in the Matrix ecosystem this week?")
research("Compare hosted Matrix bridges for small teams.")
```

### Notes

- Pick `exa` when you need the richest research surface on this page rather than a simple search box.
- `model` only affects `exa_answer()`, and `research_model` only affects `research()`.
- The current wrapper exposes `livecrawl`, but the installed upstream call path in this worktree does not apply that setting to the search requests, so do not rely on it yet for behavior changes.

## \[`serpapi`\]

`serpapi` is the Google and YouTube search toolkit for agents that need a paid SERP provider instead of DDGS-backed scraping.

### What It Does

`serpapi` exposes `search_google(query, num_results=10)` and `search_youtube(query)`.
`search_google()` returns a JSON payload with `search_results`, `recipes_results`, `shopping_results`, `knowledge_graph`, and `related_questions`.
`search_youtube()` returns `video_results`, `movie_results`, and `channel_results`.
MindRoom does not add extra behavior here beyond registering the tool metadata and dependency set.

### Configuration

| Option                  | Type       | Required | Default | Notes                                                     |
| ----------------------- | ---------- | -------- | ------- | --------------------------------------------------------- |
| `api_key`               | `password` | `yes`    | `null`  | SerpApi key. The upstream SDK also checks `SERP_API_KEY`. |
| `enable_search_google`  | `boolean`  | `no`     | `true`  | Enable `search_google()`.                                 |
| `enable_search_youtube` | `boolean`  | `no`     | `false` | Enable `search_youtube()`.                                |
| `all`                   | `boolean`  | `no`     | `false` | Enable the full upstream toolkit surface.                 |

### Example

```
agents:
  researcher:
    tools:
      - serpapi:
          enable_search_youtube: true
```

```
search_google("Matrix bridges", num_results=10)
search_youtube("Matrix conference talks")
```

### Notes

- Pick `serpapi` when you specifically want Google plus YouTube search from one paid provider.
- `serpapi` is a better fit than `googlesearch` when you want a provider-backed API instead of DDGS-backed scraping.
- `serper` is the better fit when you need Google news, Google Scholar, or a scrape endpoint instead of YouTube search.

## \[`serper`\]

`serper` is the Google API toolkit for web, news, scholar, and lightweight scrape calls.

### What It Does

`serper` exposes `search_web(query, num_results=None)`, `search_news(query, num_results=None)`, `search_scholar(query, num_results=None)`, and `scrape_webpage(url, markdown=False)`.
`location`, `language`, and `date_range` become shared request parameters across the search endpoints.
The search methods return raw JSON responses from Serper.
`scrape_webpage()` hits Serper's scrape endpoint and can optionally request Markdown output.

### Configuration

| Option                  | Type       | Required | Default | Notes                                                          |
| ----------------------- | ---------- | -------- | ------- | -------------------------------------------------------------- |
| `api_key`               | `password` | `yes`    | `null`  | Serper API key. The upstream SDK also checks `SERPER_API_KEY`. |
| `location`              | `text`     | `no`     | `us`    | Google location code sent as `gl`.                             |
| `language`              | `text`     | `no`     | `en`    | Search language code sent as `hl`.                             |
| `num_results`           | `number`   | `no`     | `10`    | Default result count for search calls.                         |
| `date_range`            | `text`     | `no`     | `null`  | Shared date-range filter sent as `tbs`.                        |
| `enable_search`         | `boolean`  | `no`     | `true`  | Enable `search_web()`.                                         |
| `enable_search_news`    | `boolean`  | `no`     | `true`  | Enable `search_news()`.                                        |
| `enable_search_scholar` | `boolean`  | `no`     | `true`  | Enable `search_scholar()`.                                     |
| `enable_scrape_webpage` | `boolean`  | `no`     | `true`  | Enable `scrape_webpage()`.                                     |
| `all`                   | `boolean`  | `no`     | `false` | Enable the full upstream toolkit surface.                      |

### Example

```
agents:
  analyst:
    tools:
      - serper:
          location: us
          language: en
          enable_search_scholar: true
```

```
search_web("latest Matrix rooms UX", num_results=5)
search_news("Matrix foundation news", num_results=5)
search_scholar("Matrix protocol paper", num_results=5)
scrape_webpage("https://matrix.org/blog/", markdown=True)
```

### Notes

- Pick `serper` when you want Google news and scholar in the same paid toolkit.
- `serper` also covers quick scrape calls, which makes it a good bridge between search and light extraction workflows.
- If you want YouTube search instead of scholar or scraping, use `serpapi` instead.

## \[`searxng`\]

`searxng` points an agent at your own SearXNG instance instead of a hosted paid API.

### What It Does

`searxng` exposes `search_web(query, max_results=5)`, `image_search(query, max_results=5)`, `it_search(query, max_results=5)`, `map_search(query, max_results=5)`, `music_search(query, max_results=5)`, `news_search(query, max_results=5)`, `science_search(query, max_results=5)`, and `video_search(query, max_results=5)`.
All of those calls route through the same `/search?format=json` endpoint on the configured `host`.
If `engines` is set, the tool appends those engine names to the SearXNG request.
`fixed_max_results` truncates every category response to a consistent maximum.

### Configuration

| Option              | Type       | Required | Default | Notes                                                                                                                              |
| ------------------- | ---------- | -------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `host`              | `url`      | `yes`    | `null`  | Base URL for the SearXNG instance. Use the instance root, not a prebuilt `/search` URL.                                            |
| `engines`           | `string[]` | `no`     | `[]`    | Optional engine allowlist. The current registry metadata exposes this as a text field, but runtime expects a list of engine names. |
| `fixed_max_results` | `number`   | `no`     | `null`  | Caps result count for all categories.                                                                                              |

### Example

```
agents:
  privacy_research:
    tools:
      - searxng:
          host: https://search.example.com
          engines:
            - duckduckgo
            - wikipedia
          fixed_max_results: 6
```

```
search_web("Matrix federation guide", max_results=5)
news_search("Matrix news", max_results=5)
science_search("decentralized messaging protocol", max_results=5)
image_search("Matrix logo", max_results=5)
```

### Notes

- Pick `searxng` when you want a self-hosted or privacy-preserving search backend under your own control.
- `searxng` is the only tool on this page that exposes image, map, music, science, and video categories through the same configuration.
- If your SearXNG deployment needs auth or reverse-proxy policy, handle that at the instance or network layer because the current MindRoom tool metadata only exposes `host`, `engines`, and `fixed_max_results`.

## \[`linkup`\]

`linkup` is a web-search API that can return either search-result lists or sourced answers.

### What It Does

`linkup` exposes `web_search_with_linkup(query, depth=None, output_type=None)`.
`depth` controls how aggressively Linkup searches, and `output_type` controls whether the response is a `searchResults` list or a `sourcedAnswer`.
The configured defaults are applied when the call does not override them.
The tool returns the raw response from the Linkup SDK rather than a MindRoom-specific normalized envelope.

### Configuration

| Option                          | Type       | Required | Default         | Notes                                                                                                                                                     |
| ------------------------------- | ---------- | -------- | --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `api_key`                       | `password` | `yes`    | `null`          | Linkup API key. The upstream SDK documents `LINKUP_API_KEY`, but the current MindRoom wrapper is safest when you store the key explicitly in tool config. |
| `depth`                         | `text`     | `no`     | `standard`      | Default search depth, currently `standard` or `deep`.                                                                                                     |
| `output_type`                   | `text`     | `no`     | `searchResults` | Default output type, currently `searchResults` or `sourcedAnswer`.                                                                                        |
| `enable_web_search_with_linkup` | `boolean`  | `no`     | `true`          | Enable `web_search_with_linkup()`.                                                                                                                        |
| `all`                           | `boolean`  | `no`     | `false`         | Enable the full upstream toolkit surface.                                                                                                                 |

### Example

```
agents:
  briefings:
    tools:
      - linkup:
          depth: deep
          output_type: sourcedAnswer
```

```
web_search_with_linkup(
    "Summarize the latest Matrix bridge announcements",
    depth="deep",
    output_type="sourcedAnswer",
)
```

### Notes

- Pick `linkup` when you want a sourced answer directly from the search provider instead of stitching one together downstream.
- Pick `tavily` when you also want built-in extract calls, and pick `exa` when you need broader research primitives such as `find_similar()` or `research()`.
- The current MindRoom wrapper initializes the Linkup client from the explicit `api_key` argument, so a stored tool credential is more reliable than relying on environment-only fallback on this branch.

## Related Docs

- [Tools Overview](https://docs.mindroom.chat/tools/index.md)
- [Per-Agent Tool Configuration](https://docs.mindroom.chat/configuration/agents/#per-agent-tool-configuration)
