# Research Sources

Use these tools to query source-specific knowledge bases such as ArXiv, Wikipedia, PubMed, and Hacker News instead of doing general web search.

## What This Page Covers

This page documents the built-in tools in the `research-sources` group.
Use these tools when you want paper-only search, encyclopedia summaries, biomedical literature lookup, or Hacker News story and user data.

## Tools On This Page

- \[`arxiv`\] - Search ArXiv and optionally download papers to extract page text.
- \[`wikipedia`\] - Fetch Wikipedia summaries, or update an injected knowledge base from Wikipedia.
- \[`pubmed`\] - Search PubMed for medical and life-science literature with concise or expanded result formatting.
- \[`hackernews`\] - Fetch top Hacker News stories and basic user details from the public API.

## Common Setup Notes

All four tools are `setup_type: none`, so they work out of the box and do not require API keys or OAuth.
`src/mindroom/api/integrations.py` currently only exposes Spotify OAuth routes on this branch, so these tools have no dedicated dashboard auth flow.
Missing optional Python dependencies can auto-install at first use unless `MINDROOM_NO_AUTO_INSTALL_TOOLS=1` is set.
MindRoom does not add Matrix runtime-context behavior or worker-routing overrides for these tools. Use [Web Search](https://docs.mindroom.chat/tools/web-search/index.md) instead when you need broader web discovery, news search, or provider-backed search APIs.

## \[`arxiv`\]

`arxiv` searches ArXiv by query and can download selected PDFs to extract text from their pages.

### What It Does

By default `arxiv` exposes `search_arxiv_and_return_articles(query, num_articles=10)` and `read_arxiv_papers(id_list, pages_to_read=None)`.
Search results are returned as JSON with title, short ID, entry URL, authors, categories, publish timestamp, PDF URL, links, summary, and comment.
Reading papers downloads each PDF locally, parses it with `pypdf`, and returns the same metadata plus per-page extracted text.

### Configuration

| Option                     | Type      | Required | Default | Notes                                                                    |
| -------------------------- | --------- | -------- | ------- | ------------------------------------------------------------------------ |
| `enable_search_arxiv`      | `boolean` | `no`     | `true`  | Enable `search_arxiv_and_return_articles()`.                             |
| `enable_read_arxiv_papers` | `boolean` | `no`     | `true`  | Enable `read_arxiv_papers()`.                                            |
| `all`                      | `boolean` | `no`     | `false` | Enable the full upstream toolkit surface.                                |
| `download_dir`             | `text`    | `no`     | `null`  | Local directory where downloaded PDFs are stored before text extraction. |

### Example

```yaml
agents:
  researcher:
    tools:
      - arxiv:
          download_dir: mindroom_data/arxiv
```

```python
search_arxiv_and_return_articles("matrix protocol", num_articles=5)
read_arxiv_papers(["2103.03404v1"], pages_to_read=3)
```

### Notes

- `read_arxiv_papers()` expects ArXiv IDs such as `2103.03404v1`, not a free-text search query.
- If `download_dir` is not set, the upstream toolkit writes PDFs to its default local `arxiv_pdfs` directory before parsing them.
- Use `duckduckgo`, `googlesearch`, or `exa` from [Web Search](https://docs.mindroom.chat/tools/web-search/index.md) when you need broader search beyond ArXiv papers.

## \[`wikipedia`\]

`wikipedia` is the lightweight encyclopedia lookup tool for summary-style retrieval from Wikipedia.

### What It Does

In normal MindRoom usage `wikipedia` exposes `search_wikipedia(query)`, which returns one JSON document containing the queried title and `wikipedia.summary(query)` content.
If an upstream `Knowledge` object is injected, the toolkit instead exposes `search_wikipedia_and_update_knowledge_base(topic)`, which inserts the topic into that knowledge base and returns relevant documents from it.
This makes `wikipedia` a simple direct lookup tool by default, with an advanced knowledge-base update mode for custom integrations.

### Configuration

| Option      | Type      | Required | Default | Notes                                                                                                                                         |
| ----------- | --------- | -------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `knowledge` | `text`    | `no`     | `null`  | Advanced upstream hook for injecting a `Knowledge` object. In typical MindRoom YAML usage you leave this unset and use direct summary search. |
| `all`       | `boolean` | `no`     | `false` | Exposed in metadata, but the current upstream implementation does not change behavior for this toolkit.                                       |

### Example

```yaml
agents:
  researcher:
    tools:
      - wikipedia
```

```python
search_wikipedia("Matrix protocol")
```

### Notes

- `knowledge` is not a normal string option at runtime, so the usual MindRoom configuration is just `- wikipedia`.
- Search uses the upstream `wikipedia.summary()` call, so ambiguous topics work best with a specific query.
- Use [Web Search](https://docs.mindroom.chat/tools/web-search/index.md) when you need multiple result links or broader web coverage instead of one encyclopedia summary.

## \[`pubmed`\]

`pubmed` searches PubMed through NCBI E-utilities and formats article metadata for medical and life-science research.

### What It Does

`pubmed` exposes `search_pubmed(query, max_results=10)`.
It first looks up PubMed IDs through `esearch`, then fetches article XML through `efetch`, and finally returns a JSON list of formatted result strings.
Default output includes title, publication year, and summary text.
When `results_expanded` is enabled, each result also includes first author, journal, publication type, DOI, PubMed URL, full-text URL when available, keywords, and MeSH terms.

### Configuration

| Option                 | Type      | Required | Default                  | Notes                                                                                                   |
| ---------------------- | --------- | -------- | ------------------------ | ------------------------------------------------------------------------------------------------------- |
| `email`                | `text`    | `no`     | `your_email@example.com` | Contact email sent to NCBI E-utilities. A real email is recommended even though no API key is required. |
| `max_results`          | `number`  | `no`     | `null`                   | Default result cap used when the call does not pass `max_results`.                                      |
| `results_expanded`     | `boolean` | `no`     | `false`                  | Return richer metadata instead of the concise title and summary format.                                 |
| `enable_search_pubmed` | `boolean` | `no`     | `true`                   | Enable `search_pubmed()`.                                                                               |
| `all`                  | `boolean` | `no`     | `false`                  | Enable the full upstream toolkit surface.                                                               |

### Example

```yaml
agents:
  clinician:
    tools:
      - pubmed:
          email: research@example.com
          max_results: 5
          results_expanded: true
```

```python
search_pubmed("CRISPR therapy", max_results=5)
```

### Notes

- `pubmed` does not need an API key, but the upstream client sends the configured `email` with requests to NCBI.
- Concise mode truncates long abstracts to about 200 characters, so use `results_expanded: true` when you need more context in each result.
- The tool returns a JSON list of formatted text blocks rather than a deeply nested article schema.

## \[`hackernews`\]

`hackernews` reads the public Hacker News Firebase API for top-story and user-profile data.

### What It Does

By default `hackernews` exposes `get_top_hackernews_stories(num_stories=10)` and `get_user_details(username)`.
Top-story lookups return the raw story objects from the Hacker News item endpoint, with an extra `username` field copied from `by`.
User lookups return a smaller JSON object with karma, about text, and total submitted item count.

### Configuration

| Option                    | Type      | Required | Default | Notes                                     |
| ------------------------- | --------- | -------- | ------- | ----------------------------------------- |
| `enable_get_top_stories`  | `boolean` | `no`     | `true`  | Enable `get_top_hackernews_stories()`.    |
| `enable_get_user_details` | `boolean` | `no`     | `true`  | Enable `get_user_details()`.              |
| `all`                     | `boolean` | `no`     | `false` | Enable the full upstream toolkit surface. |

### Example

```yaml
agents:
  tech_watch:
    tools:
      - hackernews
```

```python
get_top_hackernews_stories(num_stories=5)
get_user_details("pg")
```

### Notes

- This tool uses public Hacker News endpoints and does not need credentials.
- `get_top_hackernews_stories()` is best for front-page monitoring and lightweight discussion sourcing, not full web search.
- Pair it with [Web Search](https://docs.mindroom.chat/tools/web-search/index.md) or [Web Scraping & Browser](https://docs.mindroom.chat/tools/web-scraping-and-browser/index.md) when you want to follow story links and inspect the linked pages themselves.

## Related Docs

- [Tools Overview](https://docs.mindroom.chat/tools/index.md)
- [Web Search](https://docs.mindroom.chat/tools/web-search/index.md)
- [Web Scraping & Browser](https://docs.mindroom.chat/tools/web-scraping-and-browser/index.md)
