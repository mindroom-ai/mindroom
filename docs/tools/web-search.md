---
icon: lucide/wrench
---

# Web Search

Use these tools to give agents general web search, news search, answer-style search APIs, Google- or Baidu-oriented results, or a self-hosted SearXNG backend.

## Tools On This Page

| Tool | Needs | Best for |
| --- | --- | --- |
| [`duckduckgo`] | Nothing | Lowest-friction web and news search. |
| [`googlesearch`] | Nothing | Web and news search that asks for the Google engine first. |
| [`baidusearch`] | Nothing | Baidu-indexed and Chinese-language results. |
| [`tavily`] | API key | Current-information search with an optional synthesized answer, a compact context mode, and URL extraction. |
| [`exa`] | API key | Research: domain and date filters, page contents, similar-page lookup, answers, and long-running research tasks. |
| [`serpapi`] | API key | Paid Google and YouTube results. |
| [`serper`] | API key | Paid Google web, news, and Scholar results plus a webpage scrape call. |
| [`searxng`] | Your SearXNG `host` | Self-hosted search, including image, IT, map, music, science, news, and video categories. |
| [`linkup`] | API key | Raw results or a sourced answer from one call. |

## Setup

Add the tool to an agent's `tools` list; see [Per-Agent Tool Configuration](index.md#per-agent-tool-configuration) for inline options and `include_tools`/`exclude_tools`.
`duckduckgo`, `googlesearch`, and `baidusearch` work without any setup.
`searxng` needs only a reachable `host` URL.
The API-backed tools need an `api_key`, set through the dashboard or credential store (see [Security Restrictions](index.md#security-restrictions)).
Each API-backed tool also reads its key from the environment when no `api_key` is stored:

| Tool | Environment variable |
| --- | --- |
| `tavily` | `TAVILY_API_KEY` (and `TAVILY_API_BASE_URL` for `api_base_url`) |
| `exa` | `EXA_API_KEY` |
| `serpapi` | `SERP_API_KEY` |
| `serper` | `SERPER_API_KEY` |
| `linkup` | `LINKUP_API_KEY` |

Missing Python dependencies install on first use; see [Automatic Dependency Installation](index.md#automatic-dependency-installation).
Every toolkit that has an `all` option enables all of its functions when `all: true`, regardless of the individual `enable_*` flags, except that [`tavily`] still provides only one search function.

## [`duckduckgo`]

`duckduckgo` searches the web and news through DuckDuckGo with no API key.
It provides `web_search(query, max_results=5)` and `search_news(query, max_results=5)`, both returning DDGS results as JSON.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `enable_search` | `boolean` | `true` | Enable `web_search()`. |
| `enable_news` | `boolean` | `true` | Enable `search_news()`. |
| `modifier` | `text` | `null` | Text prepended to every `web_search()` query, for example `site:docs.example.com`. |
| `fixed_max_results` | `number` | `null` | Result count for every call, replacing the per-call `max_results`. |
| `timelimit` | `text` | `null` | Age filter: `d`, `w`, `m`, or `y`. |
| `region` | `text` | `null` | DDGS region code, such as `us-en`. |
| `backend` | `text` | `duckduckgo` | DDGS backend override. |
| `proxy` | `url` | `null` | Proxy for search requests. |
| `timeout` | `number` | `10` | Request timeout in seconds. |
| `verify_ssl` | `boolean` | `true` | Verify TLS certificates. |

```yaml
agents:
  researcher:
    tools:
      - duckduckgo:
          fixed_max_results: 8
          timelimit: w
```

## [`googlesearch`]

`googlesearch` provides the same `web_search()` and `search_news()` functions, options, and output as [`duckduckgo`], except that it has no `backend` option and requests the Google engine.
It is not an official Google API.
DDGS currently falls back to automatic engine selection for Google requests, so results and ranking are not guaranteed to come from Google.
Use [`serper`] or [`serpapi`] when you need real Google results.

```yaml
agents:
  researcher:
    tools:
      - googlesearch:
          modifier: site:docs.mindroom.chat
          fixed_max_results: 6
```

## [`baidusearch`]

`baidusearch` provides `baidu_search(query, max_results=5, language="zh")`, which returns a JSON array of results with `title`, `url`, `abstract`, and `rank`.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `enable_baidu_search` | `boolean` | `true` | Enable `baidu_search()`. |
| `all` | `boolean` | `false` | Enable all functions. |
| `fixed_max_results` | `number` | `null` | Result count for every call, replacing the per-call `max_results`. |
| `fixed_language` | `text` | `null` | Replaces the per-call `language`; has no effect on results. |
| `headers` | `password` | `null` | Has no effect. |
| `proxy` | `url` | `null` | Has no effect. |
| `timeout` | `number` | `10` | Has no effect. |
| `debug` | `boolean` | `false` | Has no effect. |

Only the query and result count reach Baidu, so `language`, `fixed_language`, `headers`, `proxy`, `timeout`, and `debug` do not change the search.

```yaml
agents:
  cn_research:
    tools:
      - baidusearch:
          fixed_max_results: 8
```

## [`tavily`]

`tavily` searches current information through the Tavily API and can extract page content from URLs.
It provides one search function, `web_search_using_tavily(query, max_results=5)` by default or `web_search_with_tavily(query)` when `enable_search_context: true`, plus `extract_url_content(urls)` when `enable_extract: true`.
`web_search_using_tavily()` returns results and, with `include_answer`, a synthesized answer, as Markdown or JSON according to `format`.
`web_search_with_tavily()` returns one compact context block instead of a result list.
`extract_url_content()` accepts one URL or a comma-separated list and returns page content as Markdown or plain text according to `extract_format`.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Tavily API key; required unless `TAVILY_API_KEY` is set. |
| `api_base_url` | `url` | `null` | API base URL override. |
| `enable_search` | `boolean` | `true` | Enable the search function. |
| `enable_search_context` | `boolean` | `false` | Provide `web_search_with_tavily()` instead of `web_search_using_tavily()`. |
| `enable_extract` | `boolean` | `false` | Enable `extract_url_content()`. |
| `all` | `boolean` | `false` | Enable search and `extract_url_content()`; `enable_search_context` still selects the search function. |
| `max_tokens` | `number` | `6000` | Output budget for search results and context mode. |
| `include_answer` | `boolean` | `true` | Include Tavily's answer in search output. |
| `search_depth` | `text` | `advanced` | `basic`, `advanced`, `fast`, or `ultra-fast`. |
| `format` | `text` | `markdown` | Search output format: `markdown` or `json`. |
| `topic` | `text` | `null` | Search category: `general`, `news`, or `finance`. |
| `time_range` | `text` | `null` | `day`, `week`, `month`, or `year` (or `d`, `w`, `m`, `y`). |
| `start_date` | `text` | `null` | Only results published after this date (`YYYY-MM-DD`). |
| `end_date` | `text` | `null` | Only results published before this date (`YYYY-MM-DD`). |
| `days` | `number` | `null` | Days back to include; `news` topic only. |
| `include_domains` | `string[]` | `null` | Restrict results to these domains. |
| `exclude_domains` | `string[]` | `null` | Exclude these domains. |
| `country` | `text` | `null` | Boost results from this country, such as `united states`. |
| `auto_parameters` | `boolean` | `false` | Let Tavily tune search parameters; explicitly set options take precedence. |
| `chunks_per_source` | `number` | `null` | Content chunks per source, `1` to `3`; `advanced` depth only. |
| `extract_depth` | `text` | `basic` | `basic` or `advanced`. |
| `extract_format` | `text` | `markdown` | `markdown` or `text`. |
| `extract_timeout` | `number` | `null` | Extraction timeout in seconds. |
| `include_images` | `boolean` | `false` | Requests images from Tavily during extraction, but the returned content does not include them. |
| `include_favicon` | `boolean` | `false` | Requests favicons from Tavily during extraction, but the returned content does not include them. |

The filters from `topic` through `chunks_per_source` apply only to `web_search_using_tavily()`, not to context mode.

```yaml
agents:
  newsdesk:
    tools:
      - tavily:
          enable_extract: true
          topic: news
          time_range: week
```

## [`exa`]

`exa` is the research-oriented search toolkit.
It provides `search_exa(query, num_results=5, category=None)`, `get_contents(urls)`, `find_similar(url, num_results=5)`, `exa_answer(query, text=False)`, and, only when `enable_research: true`, the long-running `research(instructions, output_schema=None)`.
Search results include title, author, published date, URL, and page text truncated to `text_length_limit`.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Exa API key; required unless `EXA_API_KEY` is set. |
| `enable_search` | `boolean` | `true` | Enable `search_exa()`. |
| `enable_get_contents` | `boolean` | `true` | Enable `get_contents()`. |
| `enable_find_similar` | `boolean` | `true` | Enable `find_similar()`. |
| `enable_answer` | `boolean` | `true` | Enable `exa_answer()`. |
| `enable_research` | `boolean` | `false` | Enable `research()`. |
| `all` | `boolean` | `false` | Enable all functions. |
| `text` | `boolean` | `true` | Include page text in results. |
| `text_length_limit` | `number` | `1000` | Maximum text length per result. |
| `summary` | `boolean` | `false` | Request result summaries. |
| `num_results` | `number` | `null` | Result count for every call, replacing the per-call `num_results`. |
| `type` | `text` | `null` | Exa search mode, such as `auto`. |
| `category` | `text` | `null` | Content category, such as `news`; replaces the per-call `category`. |
| `include_domains` | `string[]` | `null` | Domain allowlist. |
| `exclude_domains` | `string[]` | `null` | Domain denylist. |
| `start_published_date` | `text` | `null` | Earliest publication date, ISO 8601. |
| `end_published_date` | `text` | `null` | Latest publication date, ISO 8601. |
| `start_crawl_date` | `text` | `null` | Deprecated; Exa ignores it. |
| `end_crawl_date` | `text` | `null` | Deprecated; Exa ignores it. |
| `livecrawl` | `text` | `always` | Has no effect. |
| `model` | `text` | `null` | Model for `exa_answer()` only: `exa` or `exa-pro`. |
| `research_model` | `text` | `exa-research` | Model for `research()` only: `exa-research` or `exa-research-pro`. |
| `timeout` | `number` | `30` | API timeout in seconds. |
| `show_results` | `boolean` | `false` | Log raw parsed results. |

Publication-date filters cannot be combined with the `company` and `people` categories.

```yaml
agents:
  analyst:
    tools:
      - exa:
          enable_research: true
          type: auto
          category: news
          include_domains:
            - matrix.org
            - element.io
```

## [`serpapi`]

`serpapi` provides `search_google(query, num_results=10)` and, when enabled, `search_youtube(query)` through SerpApi.
`search_google()` returns organic results plus recipes, shopping, knowledge graph, and related questions.
`search_youtube()` returns video, movie, and channel results.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | SerpApi key; required unless `SERP_API_KEY` is set. |
| `enable_search_google` | `boolean` | `true` | Enable `search_google()`. |
| `enable_search_youtube` | `boolean` | `false` | Enable `search_youtube()`. |
| `all` | `boolean` | `false` | Enable all functions. |

```yaml
agents:
  researcher:
    tools:
      - serpapi:
          enable_search_youtube: true
```

## [`serper`]

`serper` provides `search_web(query, num_results=None)`, `search_news(query, num_results=None)`, `search_scholar(query, num_results=None)`, and `scrape_webpage(url, markdown=False)` through the Serper API.
All functions return Serper's raw JSON, and `scrape_webpage(markdown=True)` also requests the page as Markdown.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Serper API key; required unless `SERPER_API_KEY` is set. |
| `location` | `text` | `us` | Google country code (`gl`) for all searches. |
| `language` | `text` | `en` | Google language code (`hl`) for all searches. |
| `num_results` | `number` | `10` | Default result count when a call does not pass one. |
| `date_range` | `text` | `null` | Google date filter (`tbs`) for all searches, such as `qdr:w`. |
| `enable_search` | `boolean` | `true` | Enable `search_web()`. |
| `enable_search_news` | `boolean` | `true` | Enable `search_news()`. |
| `enable_search_scholar` | `boolean` | `true` | Enable `search_scholar()`. |
| `enable_scrape_webpage` | `boolean` | `true` | Enable `scrape_webpage()`. |
| `all` | `boolean` | `false` | Enable all functions. |
| `timeout` | `number` | `30` | Request timeout in seconds. |

```yaml
agents:
  analyst:
    tools:
      - serper:
          location: de
          language: de
          enable_scrape_webpage: false
```

## [`searxng`]

`searxng` searches through your own SearXNG instance.
It provides `search_web`, `image_search`, `it_search`, `map_search`, `music_search`, `news_search`, `science_search`, and `video_search`, each taking `(query, max_results=5)`.
The instance must allow JSON output (`json` in SearXNG's `search.formats`).

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `host` | `url` | required | Instance root URL, such as `https://search.example.com`, not a `/search` URL. |
| `engines` | `string[]` | `[]` | Restrict searches to these SearXNG engines. |
| `fixed_max_results` | `number` | `null` | Result count for every call, replacing the per-call `max_results`. |
| `timeout` | `number` | `30` | Request timeout in seconds. |

The tool sends no credentials, so put any authentication or access policy at the network or reverse-proxy layer.

```yaml
agents:
  privacy_research:
    tools:
      - searxng:
          host: https://search.example.com
          engines:
            - duckduckgo
            - wikipedia
```

## [`linkup`]

`linkup` provides `web_search_with_linkup(query, depth=None, output_type=None)`, which returns Linkup's response as a result list or a sourced answer.
The configured `depth` and `output_type` apply when a call does not pass its own.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Linkup API key; required unless `LINKUP_API_KEY` is set. |
| `depth` | `text` | `standard` | `standard` or `deep`. |
| `output_type` | `text` | `searchResults` | `searchResults` or `sourcedAnswer`. |
| `enable_web_search_with_linkup` | `boolean` | `true` | Enable `web_search_with_linkup()`. |
| `all` | `boolean` | `false` | Enable all functions. |

```yaml
agents:
  briefings:
    tools:
      - linkup:
          depth: deep
          output_type: sourcedAnswer
```

## Related Docs

- [Tools Overview](index.md)
- [Research Sources](research-sources.md)
- [Web Scraping & Browser](web-scraping-and-browser.md)
