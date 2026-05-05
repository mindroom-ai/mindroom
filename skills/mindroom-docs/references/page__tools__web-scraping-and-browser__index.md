# Web Scraping & Browser

Use these tools to read pages, extract article text, crawl websites, and drive either a local browser runtime or a hosted browser API.

## What This Page Covers

This page documents the built-in tools in the `web-scraping-and-browser` group.
Use these tools when you need lightweight text extraction, structured scraping APIs, or browser automation against live websites.

## Tools On This Page

- \[`crawl4ai`\] - Local Crawl4AI crawling with readable-text extraction and optional query-aware filtering.
- \[`website`\] - Simple website reader and optional knowledge-base ingester.
- \[`trafilatura`\] - Local page extraction, metadata extraction, HTML-to-text conversion, batch extraction, and focused crawling.
- \[`newspaper`\] - News-article reader backed by the `newspaper4k` dependency.
- \[`jina`\] - Jina Reader URL reading and optional web search with an optional API key.
- \[`firecrawl`\] - Firecrawl API for scrape, crawl, map, and search jobs.
- \[`spider`\] - Spider Cloud API for search, scrape, and crawl.
- \[`scrapegraph`\] - ScrapeGraph AI extraction, markdown conversion, search scraping, and agentic crawling.
- \[`apify`\] - Apify Actor runner that turns configured actors into tool functions.
- \[`brightdata`\] - Bright Data scraping, screenshots, SERP queries, and feed endpoints.
- \[`oxylabs`\] - Oxylabs Google search, Amazon data, and general web scraping.
- \[`agentql`\] - AgentQL browser-assisted scraping with optional custom extraction queries.
- \[`browserbase`\] - Browserbase-hosted browser sessions with remote navigation, screenshots, and page reads.
- \[`browser`\] - MindRoom's local Playwright browser controller.
- \[`web_browser_tools`\] - Host OS browser opener for launching a real browser tab or window.

## Common Setup Notes

`crawl4ai`, `website`, `trafilatura`, `newspaper`, and `web_browser_tools` are the lowest-friction no-config options on this page.
`firecrawl`, `browserbase`, `agentql`, `scrapegraph`, `apify`, `brightdata`, and `oxylabs` are all credentialed tools that normally need stored credentials or SDK environment variables before they are useful.
`spider` also needs credentials in practice even though the current MindRoom metadata marks it as `setup_type: none`, because the installed `spider-client` raises when `SPIDER_API_KEY` is missing.
`jina` is the middle ground here, because the installed `JinaReaderTools` only adds an `Authorization` header when `api_key` is present, so public `read_url()` usage works without a key while authenticated plans can still set one.
`browser` is local Playwright automation, `browserbase` is a hosted browser API that you connect to over CDP, and `web_browser_tools` simply asks the host operating system to open a browser tab or window.
`src/mindroom/api/integrations.py` currently only exposes Spotify OAuth routes on this branch, so none of the tools on this page have a dedicated MindRoom OAuth flow.
Store password fields through the dashboard or credential store instead of inline YAML, and use environment variables such as `FIRECRAWL_API_KEY`, `SPIDER_API_KEY`, `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID`, `AGENTQL_API_KEY`, `SGAI_API_KEY`, `APIFY_API_TOKEN`, `BRIGHT_DATA_API_KEY`, `OXYLABS_USERNAME`, `OXYLABS_PASSWORD`, and `JINA_API_KEY` when you prefer SDK-native auth.
`crawl4ai`, `agentql`, `browserbase`, and `browser` also depend on a working browser runtime, and `web_browser_tools` only makes sense on a host that can open a real desktop browser.
Missing optional dependencies can auto-install at first use unless `MINDROOM_NO_AUTO_INSTALL_TOOLS=1` is set.

## No-Config Scrapers

### \[`crawl4ai`\]

`crawl4ai` is the best local option on this page when you want one tool that can fetch readable page content from one URL or a short URL list.

#### What It Does

`crawl4ai` exposes `crawl(url, search_query=None)`.
It accepts either one URL string or a list of URLs and returns readable extracted content for each one.
When you pass `search_query`, the tool enables BM25-based content filtering to keep the extracted text focused on that query.
When `use_pruning` is enabled without a query, the tool uses Crawl4AI pruning to trim noisy page content.
The current implementation bypasses Crawl4AI cache for fresher reads and truncates the result to `max_length` when needed.
This is a local crawler rather than a hosted API, so it does not need an API key, but it still needs a working browser runtime.

#### Configuration

| Option              | Type      | Required | Default            | Notes                                                                                                                    |
| ------------------- | --------- | -------- | ------------------ | ------------------------------------------------------------------------------------------------------------------------ |
| `max_length`        | `number`  | `no`     | `5000`             | Maximum returned character count after extraction.                                                                       |
| `timeout`           | `number`  | `no`     | `60`               | Crawl timeout in seconds.                                                                                                |
| `use_pruning`       | `boolean` | `no`     | `false`            | Enable pruning-based cleanup when no `search_query` is provided.                                                         |
| `pruning_threshold` | `number`  | `no`     | `0.48`             | Threshold passed to Crawl4AI pruning mode.                                                                               |
| `bm25_threshold`    | `number`  | `no`     | `1.0`              | Threshold passed to BM25 filtering when `search_query` is used.                                                          |
| `headless`          | `boolean` | `no`     | `true`             | Launch Crawl4AI's browser in headless mode.                                                                              |
| `wait_until`        | `text`    | `no`     | `domcontentloaded` | Playwright wait condition before extraction.                                                                             |
| `proxy_config`      | `object`  | `no`     | `null`             | Raw browser proxy config passed into Crawl4AI `BrowserConfig`, while the current MindRoom metadata exposes this as text. |
| `enable_crawl`      | `boolean` | `no`     | `true`             | Enable `crawl()`.                                                                                                        |
| `all`               | `boolean` | `no`     | `false`            | Enable the full upstream toolkit surface.                                                                                |

#### Example

```yaml
agents:
  researcher:
    tools:
      - crawl4ai:
          max_length: 8000
          use_pruning: true
          wait_until: networkidle
```

```python
crawl("https://matrix.org/blog/", search_query="bridges and federation")
```

#### Notes

- Use `crawl4ai` when you want a local scraper instead of a hosted API.
- `proxy_config` maps directly to Crawl4AI browser settings, so treat it as an advanced raw config object.
- For heavily protected or browser-hostile sites, `browserbase`, `brightdata`, or `browser` can be a better fit.

### \[`website`\]

`website` is the lightest built-in page reader on this page.

#### What It Does

With normal MindRoom YAML configuration, `website` exposes `read_url(url)` and returns JSON-serialized `Document` objects from Agno's `WebsiteReader`.
If a `Knowledge` object is injected programmatically through the `knowledge` constructor argument, the tool exposes `add_website_to_knowledge(url)` instead of `read_url()`.
That means the same registry entry can act either as a simple page reader or as a knowledge-base ingestion hook depending on how it is constructed.
In normal hand-authored `config.yaml`, you should treat this as a quick page-reading tool.

#### Configuration

| Option      | Type     | Required | Default | Notes                                                                                                                               |
| ----------- | -------- | -------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `knowledge` | `object` | `no`     | `null`  | Advanced programmatic `Knowledge` object injection that changes the tool surface from `read_url()` to `add_website_to_knowledge()`. |

#### Example

```yaml
agents:
  assistant:
    tools:
      - website
```

```python
read_url("https://docs.mindroom.chat")
```

#### Notes

- `website` is the simplest default when you just need to read one page.
- The `knowledge` field is not typical hand-written YAML and is mainly useful in programmatic setups.
- If you need metadata-only extraction, batch extraction, or crawling, `trafilatura` is usually a better fit.

### \[`trafilatura`\]

`trafilatura` is the most capable local extractor on this page when you want text extraction, metadata, HTML conversion, and lightweight crawling from one toolkit.

#### What It Does

`trafilatura` exposes `extract_text()`, `extract_metadata_only()`, `crawl_website()`, `html_to_text()`, and `extract_batch()`.
It fetches pages locally through Trafilatura and can return plain text, Markdown, JSON, XML, CSV, or HTML output depending on `output_format`.
`extract_metadata_only()` returns metadata without full article text.
`extract_batch()` loops over multiple URLs and returns one JSON payload with successes and failures.
`crawl_website()` uses Trafilatura's focused spider support when that module is importable in the runtime.
If the spider module is missing, the tool skips crawler registration instead of exposing a broken crawl function.

#### Configuration

| Option                         | Type      | Required | Default  | Notes                                                                                 |
| ------------------------------ | --------- | -------- | -------- | ------------------------------------------------------------------------------------- |
| `output_format`                | `text`    | `no`     | `txt`    | Default extraction format such as `txt`, `json`, `markdown`, `xml`, `csv`, or `html`. |
| `include_comments`             | `boolean` | `no`     | `true`   | Include comment content in extracted output.                                          |
| `include_tables`               | `boolean` | `no`     | `true`   | Keep table content in extracted output.                                               |
| `include_images`               | `boolean` | `no`     | `false`  | Include image information where Trafilatura supports it.                              |
| `include_formatting`           | `boolean` | `no`     | `false`  | Preserve formatting markers in extracted output.                                      |
| `include_links`                | `boolean` | `no`     | `false`  | Preserve links in extracted output.                                                   |
| `with_metadata`                | `boolean` | `no`     | `false`  | Include metadata in extraction output.                                                |
| `favor_precision`              | `boolean` | `no`     | `false`  | Bias extraction toward precision.                                                     |
| `favor_recall`                 | `boolean` | `no`     | `false`  | Bias extraction toward recall.                                                        |
| `target_language`              | `text`    | `no`     | `null`   | Optional ISO 639-1 language filter such as `en` or `de`.                              |
| `deduplicate`                  | `boolean` | `no`     | `false`  | Deduplicate repeated content segments.                                                |
| `max_tree_size`                | `number`  | `no`     | `null`   | Optional parser tree-size limit.                                                      |
| `max_crawl_urls`               | `number`  | `no`     | `10`     | Maximum URLs to visit when crawling.                                                  |
| `max_known_urls`               | `number`  | `no`     | `100000` | Maximum discovered URLs to track while crawling.                                      |
| `enable_extract_text`          | `boolean` | `no`     | `true`   | Enable `extract_text()`.                                                              |
| `enable_extract_metadata_only` | `boolean` | `no`     | `true`   | Enable `extract_metadata_only()`.                                                     |
| `enable_html_to_text`          | `boolean` | `no`     | `true`   | Enable `html_to_text()`.                                                              |
| `enable_extract_batch`         | `boolean` | `no`     | `true`   | Enable `extract_batch()`.                                                             |
| `enable_crawl_website`         | `boolean` | `no`     | `true`   | Enable `crawl_website()` when Trafilatura spider support is available.                |
| `all`                          | `boolean` | `no`     | `false`  | Enable the full upstream toolkit surface.                                             |

#### Example

```yaml
agents:
  analyst:
    tools:
      - trafilatura:
          output_format: markdown
          with_metadata: true
          include_links: true
```

```python
extract_text("https://matrix.org/blog/", output_format="markdown")
extract_metadata_only("https://matrix.org/blog/")
```

#### Notes

- `trafilatura` is the strongest no-key option when you want more than a plain page read.
- `crawl_website()` depends on Trafilatura spider support in the runtime, so verify the crawler function exists if crawling matters to your workflow.
- For news-article specific extraction with titles, authors, and summaries, `newspaper` can be a better fit.

### \[`newspaper`\]

`newspaper` is the article-focused extractor for news pages and blog posts.

#### What It Does

`newspaper` exposes `read_article(url)`.
It returns JSON with whichever article fields were extracted successfully, including title, authors, text, publish date, and optional summary.
`article_length` truncates article text after extraction.
The registry name is `newspaper`, but the underlying module and dependency still come from `newspaper4k`.
That means old references to `newspaper4k` are stale for current MindRoom config.

#### Configuration

| Option                | Type      | Required | Default | Notes                                          |
| --------------------- | --------- | -------- | ------- | ---------------------------------------------- |
| `include_summary`     | `boolean` | `no`     | `false` | Include article summary when available.        |
| `article_length`      | `number`  | `no`     | `null`  | Truncate article text to this many characters. |
| `enable_read_article` | `boolean` | `no`     | `true`  | Enable `read_article()`.                       |
| `all`                 | `boolean` | `no`     | `false` | Enable the full upstream toolkit surface.      |

#### Example

```yaml
agents:
  newsdesk:
    tools:
      - newspaper:
          include_summary: true
          article_length: 6000
```

```python
read_article("https://matrix.org/blog/")
```

#### Notes

- Use `newspaper` in `tools:`, not `newspaper4k`.
- This tool is tuned for article-style pages rather than arbitrary websites.
- For generic site crawling or metadata extraction across many URLs, use `trafilatura` or `crawl4ai`.

### \[`jina`\]

`jina` wraps Jina Reader's read and search endpoints and is the easiest hosted option on this page when you want an optional-key reader rather than a strict credential gate.

#### What It Does

`jina` exposes `read_url(url)` and, when enabled, `search_query(query)`.
`read_url()` prepends the target URL to `base_url`, which defaults to `https://r.jina.ai/`.
`search_query()` posts the query to `search_url`, which defaults to `https://s.jina.ai/`.
When `search_query_content` is false, the tool adds `X-Respond-With: no-content` to avoid returning full page text in search results.
Returned content is truncated to `max_content_length`.
The installed implementation only adds the `Authorization` header when an API key is present, so unauthenticated public-reader usage still works.

#### Configuration

| Option                 | Type       | Required | Default              | Notes                                                                            |
| ---------------------- | ---------- | -------- | -------------------- | -------------------------------------------------------------------------------- |
| `api_key`              | `password` | `no`     | `null`               | Optional Jina API key, with `JINA_API_KEY` as the SDK fallback.                  |
| `base_url`             | `url`      | `no`     | `https://r.jina.ai/` | Base URL for `read_url()`.                                                       |
| `search_url`           | `url`      | `no`     | `https://s.jina.ai/` | Base URL for `search_query()`.                                                   |
| `max_content_length`   | `number`   | `no`     | `10000`              | Maximum returned character count.                                                |
| `timeout`              | `number`   | `no`     | `null`               | Optional Jina timeout header in seconds.                                         |
| `search_query_content` | `boolean`  | `no`     | `true`               | Return full content in search results instead of metadata-only search summaries. |
| `enable_read_url`      | `boolean`  | `no`     | `true`               | Enable `read_url()`.                                                             |
| `enable_search_query`  | `boolean`  | `no`     | `false`              | Enable `search_query()`.                                                         |
| `all`                  | `boolean`  | `no`     | `false`              | Enable the full upstream toolkit surface.                                        |

#### Example

```yaml
agents:
  researcher:
    tools:
      - jina:
          enable_search_query: true
          search_query_content: false
```

```python
read_url("https://matrix.org/blog/")
search_query("latest Matrix bridge updates")
```

#### Notes

- `jina` works without a key for public reader endpoints, but a key is still useful for authenticated plans or rate limits.
- The current MindRoom metadata marks this tool as `requires_config`, but the installed code only treats auth as optional.
- Pick `jina` when you specifically want Jina Reader semantics instead of local extraction libraries.

## API-Based Scrapers

### \[`firecrawl`\]

`firecrawl` is the hosted scraper on this page that covers scrape, crawl, map, and search from one API.

#### What It Does

`firecrawl` exposes `scrape_website()`, `crawl_website()`, `map_website()`, and `search_web()`.
`formats` is applied to scrape, crawl, and search requests.
`limit` acts as the default result cap for crawl and search operations.
`poll_interval` controls how often crawl jobs are polled.
`search_params` is passed through to Firecrawl search calls as raw provider-specific options.
The upstream tool falls back to `FIRECRAWL_API_KEY` when `api_key` is not provided directly.

#### Configuration

| Option           | Type       | Required | Default                     | Notes                                                                                                                     |
| ---------------- | ---------- | -------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `api_key`        | `password` | `yes`    | `null`                      | Firecrawl API key, with `FIRECRAWL_API_KEY` as the SDK fallback.                                                          |
| `enable_scrape`  | `boolean`  | `no`     | `true`                      | Enable `scrape_website()`.                                                                                                |
| `enable_crawl`   | `boolean`  | `no`     | `false`                     | Enable `crawl_website()`.                                                                                                 |
| `enable_mapping` | `boolean`  | `no`     | `false`                     | Enable `map_website()`.                                                                                                   |
| `enable_search`  | `boolean`  | `no`     | `false`                     | Enable `search_web()`.                                                                                                    |
| `all`            | `boolean`  | `no`     | `false`                     | Enable the full upstream toolkit surface.                                                                                 |
| `formats`        | `string[]` | `no`     | `null`                      | Requested Firecrawl formats such as `markdown` or `html`, while the current MindRoom metadata exposes this field as text. |
| `limit`          | `number`   | `no`     | `10`                        | Default page or result limit for crawl and search.                                                                        |
| `poll_interval`  | `number`   | `no`     | `30`                        | Crawl polling interval in seconds.                                                                                        |
| `search_params`  | `object`   | `no`     | `null`                      | Raw Firecrawl search parameters object, while the current MindRoom metadata exposes this field as text.                   |
| `api_url`        | `url`      | `no`     | `https://api.firecrawl.dev` | Firecrawl API base URL.                                                                                                   |

#### Example

```yaml
agents:
  research:
    tools:
      - firecrawl:
          enable_crawl: true
          enable_search: true
          limit: 5
```

```python
scrape_website("https://matrix.org/blog/")
search_web("latest Matrix bridges")
```

#### Notes

- Use `firecrawl` when you want scrape, crawl, map, and search in one hosted API.
- `formats` and `search_params` are raw upstream arguments, so verify them against your Firecrawl plan and endpoint version.
- This is usually a better fit than `crawl4ai` when you want provider-hosted crawling instead of local browser work.

### \[`spider`\]

`spider` is Spider Cloud's search, scrape, and crawl toolkit for LLM-ready output.

#### What It Does

`spider` exposes `search_web(query, max_results=5)`, `scrape(url)`, and `crawl(url, limit=None)`.
The current wrapper calls Spider search with `fetch_page_content: false`, so search is primarily discovery rather than full-content extraction.
`scrape()` and `crawl()` request Markdown-style output from Spider.
`optional_params` is merged into Spider API requests as a raw provider options object.
The installed `spider-client` constructor raises when no API key is available, even though the current MindRoom metadata says this tool is available without setup.

#### Configuration

| Option            | Type      | Required | Default | Notes                                                                                                                                     |
| ----------------- | --------- | -------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `max_results`     | `number`  | `no`     | `null`  | Default result count override for `search_web()`.                                                                                         |
| `url`             | `url`     | `no`     | `null`  | Optional default URL constructor argument from the upstream toolkit.                                                                      |
| `optional_params` | `object`  | `no`     | `null`  | Raw Spider API parameters merged into search, scrape, and crawl requests, while the current MindRoom metadata exposes this field as text. |
| `enable_search`   | `boolean` | `no`     | `true`  | Enable `search_web()`.                                                                                                                    |
| `enable_scrape`   | `boolean` | `no`     | `true`  | Enable `scrape()`.                                                                                                                        |
| `enable_crawl`    | `boolean` | `no`     | `true`  | Enable `crawl()`.                                                                                                                         |
| `all`             | `boolean` | `no`     | `false` | Enable the full upstream toolkit surface.                                                                                                 |

#### Example

```yaml
agents:
  crawler:
    tools:
      - spider:
          max_results: 8
          enable_crawl: true
```

```python
search_web("MindRoom Matrix setup", max_results=5)
scrape("https://matrix.org/blog/")
```

#### Notes

- Treat `spider` as a credentialed tool and set `SPIDER_API_KEY`, even though the current MindRoom metadata still says `setup_type: none`.
- `optional_params` is a raw provider object and is best used only when you already know the Spider API field names you want.
- If you want a cleaner, explicitly credentialed hosted scraper with clearer metadata, `firecrawl` is usually simpler.

### \[`scrapegraph`\]

`scrapegraph` is the prompt-driven extractor on this page for turning web pages into structured answers.

#### What It Does

`scrapegraph` exposes `smartscraper()`, `markdownify()`, `crawl()`, `agentic_crawler()`, `searchscraper()`, and `scrape()`.
`smartscraper()` extracts structured data from one page based on a natural-language prompt.
`markdownify()` returns a Markdown version of a page.
`crawl()` applies a prompt plus JSON schema across a crawl.
`agentic_crawler()` performs automated steps in the browser and can optionally run AI extraction over the resulting content.
`searchscraper()` searches the web before extracting information.
`render_heavy_js` only affects the low-level `scrape()` path.

#### Configuration

| Option                   | Type       | Required | Default | Notes                                                         |
| ------------------------ | ---------- | -------- | ------- | ------------------------------------------------------------- |
| `api_key`                | `password` | `yes`    | `null`  | ScrapeGraph API key, with `SGAI_API_KEY` as the SDK fallback. |
| `enable_smartscraper`    | `boolean`  | `no`     | `true`  | Enable `smartscraper()`.                                      |
| `enable_markdownify`     | `boolean`  | `no`     | `false` | Enable `markdownify()`.                                       |
| `enable_crawl`           | `boolean`  | `no`     | `false` | Enable `crawl()`.                                             |
| `enable_searchscraper`   | `boolean`  | `no`     | `false` | Enable `searchscraper()`.                                     |
| `enable_agentic_crawler` | `boolean`  | `no`     | `false` | Enable `agentic_crawler()`.                                   |
| `enable_scrape`          | `boolean`  | `no`     | `false` | Enable raw `scrape()`.                                        |
| `render_heavy_js`        | `boolean`  | `no`     | `false` | Ask ScrapeGraph to render heavy JavaScript for `scrape()`.    |
| `all`                    | `boolean`  | `no`     | `false` | Enable the full upstream toolkit surface.                     |

#### Example

```yaml
agents:
  extractor:
    tools:
      - scrapegraph:
          enable_searchscraper: true
          enable_agentic_crawler: true
```

```python
smartscraper("https://matrix.org/blog/", "Extract the title, date, and three main points.")
markdownify("https://matrix.org/blog/")
```

#### Notes

- If you disable `enable_smartscraper` without enabling `all`, the installed upstream toolkit auto-enables `markdownify()` so the tool still has a useful default surface.
- Use `scrapegraph` when you want prompt-shaped extraction rather than generic page text.
- For purely local extraction with no hosted API dependency, use `crawl4ai` or `trafilatura`.

### \[`apify`\]

`apify` is the dynamic tool on this page, because its callable surface depends on which Actors you register.

#### What It Does

`apify` does not expose one fixed method like the other tools on this page.
Instead, it reads the configured Actor IDs and registers one tool function per Actor at startup.
Each generated tool uses the Actor's input schema to build parameters and returns that Actor's dataset items as JSON.
Without configured `actors`, there is no practical tool surface.
This is best thought of as a hosted Actor adapter rather than a single scraper API.

#### Configuration

| Option            | Type       | Required | Default | Notes                                                                                                                                                                                                   |
| ----------------- | ---------- | -------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `apify_api_token` | `password` | `yes`    | `null`  | Apify API token, with `APIFY_API_TOKEN` as the SDK fallback.                                                                                                                                            |
| `actors`          | `text`     | `yes`    | `null`  | Actor ID string such as `apify/rag-web-browser`, with the current MindRoom metadata also claiming comma-separated lists even though the installed upstream class treats a plain string as one actor ID. |

#### Example

```yaml
agents:
  extractor:
    tools:
      - apify:
          actors: apify/rag-web-browser
```

#### Notes

- `actors` is the important field here, because it determines which functions actually exist at runtime.
- The current metadata advertises comma-separated Actor IDs, but the installed upstream constructor does not split plain strings, so the safest documented path on this branch is a single Actor ID.
- Generated tool names are derived from the Actor ID, so check the runtime tool list if you need the exact callable name.

### \[`brightdata`\]

`brightdata` is the hosted toolkit for markdown scraping, screenshots, SERP queries, and provider-specific web data feeds.

#### What It Does

`brightdata` exposes `scrape_as_markdown()`, `get_screenshot()`, `search_engine()`, and `web_data_feed()`.
`scrape_as_markdown()` uses the configured web-unlocker zone and returns Markdown output.
`get_screenshot()` returns a `ToolResult` with an image artifact instead of just raw text.
`search_engine()` supports Google, Bing, and Yandex search through Bright Data's SERP infrastructure.
`web_data_feed()` accesses Bright Data feed endpoints for supported source types.
Zone selection is controlled by `serp_zone` and `web_unlocker_zone`, which can also be overridden by environment variables.

#### Configuration

| Option                   | Type       | Required | Default         | Notes                                                                        |
| ------------------------ | ---------- | -------- | --------------- | ---------------------------------------------------------------------------- |
| `api_key`                | `password` | `yes`    | `null`          | Bright Data API key, with `BRIGHT_DATA_API_KEY` as the SDK fallback.         |
| `enable_scrape_markdown` | `boolean`  | `no`     | `true`          | Enable `scrape_as_markdown()`.                                               |
| `enable_screenshot`      | `boolean`  | `no`     | `true`          | Enable `get_screenshot()`.                                                   |
| `enable_search_engine`   | `boolean`  | `no`     | `true`          | Enable `search_engine()`.                                                    |
| `enable_web_data_feed`   | `boolean`  | `no`     | `true`          | Enable `web_data_feed()`.                                                    |
| `all`                    | `boolean`  | `no`     | `false`         | Enable the full upstream toolkit surface.                                    |
| `serp_zone`              | `text`     | `no`     | `serp_api`      | SERP zone, with `BRIGHT_DATA_SERP_ZONE` able to override it.                 |
| `web_unlocker_zone`      | `text`     | `no`     | `web_unlocker1` | Web unlocker zone, with `BRIGHT_DATA_WEB_UNLOCKER_ZONE` able to override it. |
| `verbose`                | `boolean`  | `no`     | `false`         | Emit extra Bright Data request logging.                                      |
| `timeout`                | `number`   | `no`     | `600`           | Timeout in seconds.                                                          |

#### Example

```yaml
agents:
  research:
    tools:
      - brightdata:
          enable_web_data_feed: false
          timeout: 300
```

```python
scrape_as_markdown("https://matrix.org/blog/")
search_engine("Matrix hosting", engine="google", num_results=5)
```

#### Notes

- `brightdata` is the better fit than `firecrawl` when screenshots and feed endpoints matter.
- Zone environment variables can override the inline config values, so document your deployment defaults if multiple zones exist.
- `get_screenshot()` returns an image artifact rather than a file path string, which is useful for agents that need to hand the screenshot to a model immediately.

### \[`oxylabs`\]

`oxylabs` is the e-commerce and SERP-oriented scraper on this page.

#### What It Does

`oxylabs` exposes `search_google()`, `get_amazon_product()`, `search_amazon_products()`, and `scrape_website()`.
It uses the Oxylabs realtime client for Google and Amazon scraping rather than a generic HTML fetch path.
`search_google()` returns parsed organic results with title, URL, description, and position.
The Amazon functions expose both product-detail and product-search workflows.
`scrape_website()` is the generic fallback when you just want one URL scraped.
This tool is credentialed with a username and password pair rather than one API key.

#### Configuration

| Option     | Type       | Required | Default | Notes                                                          |
| ---------- | ---------- | -------- | ------- | -------------------------------------------------------------- |
| `username` | `text`     | `yes`    | `null`  | Oxylabs username, with `OXYLABS_USERNAME` as the SDK fallback. |
| `password` | `password` | `yes`    | `null`  | Oxylabs password, with `OXYLABS_PASSWORD` as the SDK fallback. |

#### Example

```yaml
agents:
  commerce:
    tools:
      - oxylabs
```

```python
search_google("Matrix hosting", domain_code="com")
search_amazon_products("ergonomic keyboard", domain_code="com")
```

#### Notes

- `oxylabs` needs both `username` and `password`, so it is not a single-key setup like `firecrawl` or `brightdata`.
- Use `domain_code` to switch between regional Google and Amazon domains.
- Pick `oxylabs` when Google SERP plus Amazon data matters more than generic website crawling.

## Browser Tools

### \[`agentql`\]

`agentql` is the browser-assisted extractor for sites where you want AgentQL queries rather than plain text scraping.

#### What It Does

`agentql` exposes `scrape_website(url)` and, when enabled, `custom_scrape_website(url)`.
`scrape_website()` uses a built-in query that extracts generic page text.
`custom_scrape_website()` only becomes useful when `agentql_query` is non-empty.
The installed upstream toolkit registers the custom scrape function automatically when `agentql_query` is set, even if `enable_custom_scrape_website` is false.
The current upstream implementation launches Playwright with `headless=False`, which matters on headless-only runtimes.

#### Configuration

| Option                         | Type       | Required | Default | Notes                                                                 |
| ------------------------------ | ---------- | -------- | ------- | --------------------------------------------------------------------- |
| `api_key`                      | `password` | `yes`    | `null`  | AgentQL API key, with `AGENTQL_API_KEY` as the SDK fallback.          |
| `enable_scrape_website`        | `boolean`  | `no`     | `true`  | Enable `scrape_website()`.                                            |
| `enable_custom_scrape_website` | `boolean`  | `no`     | `false` | Enable `custom_scrape_website()` when `agentql_query` is also useful. |
| `all`                          | `boolean`  | `no`     | `false` | Enable the full upstream toolkit surface.                             |
| `agentql_query`                | `text`     | `no`     | `""`    | Custom AgentQL query used by `custom_scrape_website()`.               |

#### Example

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

```python
scrape_website("https://matrix.org/blog/")
custom_scrape_website("https://matrix.org/blog/")
```

#### Notes

- The installed upstream code launches Playwright with `headless=False`, so this tool may need a GUI-capable runtime or virtual display.
- Setting `agentql_query` is enough to register the custom scrape function on this branch.
- Use `agentql` when you want AgentQL query semantics rather than a generic readable-text scraper.

### \[`browserbase`\]

`browserbase` is the hosted browser session tool for navigation, screenshots, and page-content reads over a remote browser.

#### What It Does

`browserbase` exposes `navigate_to()`, `screenshot()`, `get_page_content()`, and `close_session()`, plus async variants for async agent execution.
The tool auto-creates a Browserbase session, stores its `connect_url`, and connects to it over Playwright CDP.
`get_page_content()` returns visible cleaned text when `parse_html` is true and raw HTML when `parse_html` is false.
Long page content is truncated to `max_content_length`.
`base_url` configures the Browserbase API endpoint, not the website you want to visit.
This is simpler than `browser` when you only need remote navigation, screenshots, and page reads.

#### Configuration

| Option                    | Type       | Required | Default  | Notes                                                                                        |
| ------------------------- | ---------- | -------- | -------- | -------------------------------------------------------------------------------------------- |
| `api_key`                 | `password` | `yes`    | `null`   | Browserbase API key, with `BROWSERBASE_API_KEY` as the SDK fallback.                         |
| `project_id`              | `text`     | `yes`    | `null`   | Browserbase project ID, with `BROWSERBASE_PROJECT_ID` as the SDK fallback.                   |
| `base_url`                | `url`      | `no`     | `null`   | Optional Browserbase API endpoint override, with `BROWSERBASE_BASE_URL` as the SDK fallback. |
| `enable_navigate_to`      | `boolean`  | `no`     | `true`   | Enable `navigate_to()`.                                                                      |
| `enable_screenshot`       | `boolean`  | `no`     | `true`   | Enable `screenshot()`.                                                                       |
| `enable_get_page_content` | `boolean`  | `no`     | `true`   | Enable `get_page_content()`.                                                                 |
| `enable_close_session`    | `boolean`  | `no`     | `true`   | Enable `close_session()`.                                                                    |
| `all`                     | `boolean`  | `no`     | `false`  | Enable the full upstream toolkit surface.                                                    |
| `parse_html`              | `boolean`  | `no`     | `true`   | Return cleaned visible text instead of raw HTML.                                             |
| `max_content_length`      | `number`   | `no`     | `100000` | Maximum returned character count for page content.                                           |

#### Example

```yaml
agents:
  browser_worker:
    tools:
      - browserbase:
          parse_html: true
          max_content_length: 20000
```

```python
navigate_to("https://matrix.org/blog/")
get_page_content()
```

#### Notes

- `browserbase` needs both `api_key` and `project_id`.
- It still depends on local Playwright support because the client connects to the remote browser over CDP.
- Use `browserbase` when you want a hosted browser session but do not need the broader local action surface of `browser`.

### \[`browser`\]

`browser` is MindRoom's local browser controller for multi-step browser sessions, snapshots, screenshots, PDFs, uploads, dialogs, and low-level actions.

#### What It Does

`browser` exposes one callable, `browser(action=...)`, with actions such as `status`, `start`, `stop`, `profiles`, `tabs`, `open`, `focus`, `close`, `snapshot`, `screenshot`, `navigate`, `console`, `pdf`, `upload`, `dialog`, and `act`.
It manages named browser profiles, with `mindroom` as the default profile name.
It creates tabs, tracks the active tab, records console entries, and resolves temporary element refs from `snapshot()` into later `act()` and `screenshot()` calls.
`snapshot()` can return either `ai` or `aria` format.
`act()` currently supports `click`, `type`, `press`, `hover`, `drag`, `select`, `fill`, `resize`, `wait`, `evaluate`, and `close`.
Only `target="host"` is supported on this branch, so sandbox or node targeting fields currently return an error.
If `output_dir` is unset, screenshots and PDFs are written under `<storage>/browser`.
The runtime picks Chromium from `BROWSER_EXECUTABLE_PATH`, `chromium`, or `google-chrome-stable` when available.

#### Configuration

| Option       | Type   | Required | Default | Notes                                                                                                                                |
| ------------ | ------ | -------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `output_dir` | `text` | `no`     | `null`  | Optional directory for screenshots, PDFs, and other browser artifacts, with `<storage>/browser` as the runtime default when omitted. |

#### Example

```yaml
agents:
  browser_worker:
    tools:
      - browser:
          output_dir: browser-artifacts
```

```python
browser(action="open", targetUrl="https://matrix.org/blog/")
browser(action="snapshot", snapshotFormat="ai")
browser(action="act", request={"kind": "click", "ref": "e1"})
browser(action="screenshot", fullPage=True)
```

#### Notes

- This tool is local Playwright automation rather than a hosted browser API.

### \[`web_browser_tools`\]

`web_browser_tools` is the simplest browser-related tool here, because it just opens a URL in the host's real browser.

#### What It Does

`web_browser_tools` exposes `open_page(url, new_window=False)`.
It uses Python's standard-library `webbrowser` module to open a tab or window on the host operating system.
It does not return page content, DOM state, screenshots, or automation handles.
This makes it useful for human handoff or local desktop workflows, but not for scraping.

#### Configuration

| Option             | Type      | Required | Default | Notes                                     |
| ------------------ | --------- | -------- | ------- | ----------------------------------------- |
| `enable_open_page` | `boolean` | `no`     | `true`  | Enable `open_page()`.                     |
| `all`              | `boolean` | `no`     | `false` | Enable the full upstream toolkit surface. |

#### Example

```yaml
agents:
  assistant:
    tools:
      - web_browser_tools
```

```python
open_page("https://docs.mindroom.chat")
open_page("https://matrix.org/blog/", new_window=True)
```

#### Notes

- `web_browser_tools` only makes sense on a host that can launch a real browser window or tab.
- This tool is not a scraper and does not feed page content back to the model.
- Use `browser` or `browserbase` when you need browser automation or content returned to the agent.

## Related Docs

- [Tools Overview](https://docs.mindroom.chat/tools/index.md)
- [Per-Agent Tool Configuration](https://docs.mindroom.chat/configuration/agents/#per-agent-tool-configuration)
