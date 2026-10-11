# Research Sources

Use these tools to query a specific source, such as ArXiv papers, Google Scholar citations, Wikipedia summaries, PubMed literature, or Hacker News, instead of general web search.

## Tools On This Page

| Tool | Best for |
| --- | --- |
| [`arxiv`] | Searching ArXiv and reading the text of selected papers. |
| [`google_scholar`] | Cross-publisher publication search with citation counts and PDF links. |
| [`wikipedia`] | One encyclopedia summary per query. |
| [`pubmed`] | Medical and life-science literature. |
| [`hackernews`] | Top Hacker News stories and user profiles. |

## Setup

None of these tools needs an API key or OAuth.
Add the tool to an agent's `tools` list; see [Per-Agent Tool Configuration](https://docs.mindroom.chat/tools/#per-agent-tool-configuration) for inline options and `include_tools`/`exclude_tools`.
Missing Python dependencies install on first use; see [Automatic Dependency Installation](https://docs.mindroom.chat/tools/#automatic-dependency-installation).
For toolkits with an `all` option, `all: true` enables every function regardless of the individual `enable_*` flags.
Use [Web Search](https://docs.mindroom.chat/tools/web-search/) for broader web discovery or news search.

## [`arxiv`]

`arxiv` searches ArXiv and can download papers to extract their page text.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `enable_search_arxiv` | `boolean` | `true` | Enable `search_arxiv_and_return_articles(query, num_articles=10)`. |
| `enable_read_arxiv_papers` | `boolean` | `true` | Enable `read_arxiv_papers(id_list, pages_to_read=None)`. |
| `all` | `boolean` | `false` | Enable both functions. |
| `download_dir` | `text` | unset | Directory where downloaded PDFs are stored; when unset, PDFs go to an `arxiv_pdfs` directory inside the installed Agno package. |

Search returns JSON with each paper's title, ID, authors, categories, publish date, PDF URL, summary, and comment.
`read_arxiv_papers()` takes ArXiv IDs such as `2103.03404v1`, not a search query, and returns the same metadata plus the text of each page; `pages_to_read=None` reads every page.

```yaml
agents:
  researcher:
    tools:
      - arxiv:
          download_dir: /srv/mindroom/arxiv
```

## [`google_scholar`]

`google_scholar` provides `search_google_scholar(query, max_results=None)`, which returns a JSON list with title, authors, year, venue, abstract, citation count, publication URL, and PDF URL when available.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `max_results` | `number` | `5` | Result cap when the call does not pass `max_results`. |

Google Scholar has no official API, so results are scraped and citation counts, venues, and abstracts are best-effort.
Google Scholar rate-limits scrapers, and when it blocks requests the tool returns `Google Scholar is currently blocking automated requests. Try again later.`
Prefer `arxiv` or `pubmed` when they cover the topic, and use Google Scholar for cross-publisher coverage or citation counts.

## [`wikipedia`]

`wikipedia` provides `search_wikipedia(query)`, which returns the Wikipedia summary for the query as JSON.
An ambiguous query returns a list of candidate page titles instead of a summary, so retry with one of them or a more specific query.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `auto_suggest` | `boolean` | `true` | Let Wikipedia suggest or correct the title before lookup; set `false` for exact-title lookup. |
| `knowledge` | `text` | unset | Has no effect in YAML configuration; leave unset. |
| `all` | `boolean` | `false` | Has no effect for this toolkit. |

```yaml
agents:
  researcher:
    tools:
      - wikipedia
```

## [`pubmed`]

`pubmed` provides `search_pubmed(query, max_results=None)` through NCBI E-utilities and returns a JSON list of formatted text results.
By default each result has the title, publication year, and a summary truncated to about 200 characters.
With `results_expanded: true`, each result has the full abstract plus the first author, journal, publication type, DOI, PubMed URL, full-text URL when available, keywords, and MeSH terms.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `email` | `text` | `your_email@example.com` | Contact email sent to NCBI with each request; set a real address. |
| `max_results` | `number` | unset | Result cap when the call does not pass `max_results`; 10 results when neither sets it. |
| `results_expanded` | `boolean` | `false` | Return the full abstract and the expanded metadata described above. |
| `enable_search_pubmed` | `boolean` | `true` | Enable `search_pubmed()`. |
| `all` | `boolean` | `false` | Enable all functions. |
| `timeout` | `number` | `30` | Per-request HTTP timeout in seconds. |

```yaml
agents:
  clinician:
    tools:
      - pubmed:
          email: research@example.com
          max_results: 5
          results_expanded: true
```

## [`hackernews`]

`hackernews` reads the public Hacker News API.
`get_top_hackernews_stories(num_stories=10)` returns the current top story items, including title, URL, score, and author.
`get_user_details(username)` returns the user's karma, about text, and number of submitted items.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `enable_get_top_stories` | `boolean` | `true` | Enable `get_top_hackernews_stories()`. |
| `enable_get_user_details` | `boolean` | `true` | Enable `get_user_details()`. |
| `all` | `boolean` | `false` | Enable both functions. |
| `timeout` | `number` | `30` | Per-request HTTP timeout in seconds. |

The tool returns story metadata only; pair it with [Web Scraping & Browser](https://docs.mindroom.chat/tools/web-scraping-and-browser/) to read the linked pages.

## Related Docs

- [Tools Overview](https://docs.mindroom.chat/tools/)
- [Web Search](https://docs.mindroom.chat/tools/web-search/)
- [Web Scraping & Browser](https://docs.mindroom.chat/tools/web-scraping-and-browser/)
