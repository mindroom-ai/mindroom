"""Private Agno WebsiteReader crawl-state bindings with owner callbacks."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.knowledge.reader.website_reader import WebsiteReader

# Reason: WebsiteReader inlines fetching, host admission and result handling in
# its crawl loop; its private queue/visited state has no public policy hooks.
# Upstream issue: No matching injectable crawl-policy/transport issue identified.
# Upstream PR: None identified for this extension point.
# Remove when: Public crawl hooks allow owner-controlled fetch/redirect validation,
# host admission, extraction, budgets, errors and logging without copied state access.
# Coverage: tests/test_website_tool.py.


def queue_crawl_url(reader: WebsiteReader, url: str, depth: int) -> None:
    """Queue one owner-approved URL against Agno's visited and pending state."""
    if url not in reader._visited and (url, depth) not in reader._urls_to_crawl:
        reader._urls_to_crawl.append((url, depth))


def crawl_with_callbacks(
    reader: WebsiteReader,
    starting_url: str,
    starting_depth: int,
    *,
    should_skip: Callable[[str, int, int], bool],
    record: Callable[[str, int, dict[str, str]], int],
) -> dict[str, str]:
    """Run Agno's synchronous queue lifecycle using the owner's page policies."""
    num_links = 0
    crawler_result: dict[str, str] = {}
    reader._visited = set()
    reader._urls_to_crawl = [(starting_url, starting_depth)]
    while reader._urls_to_crawl:
        current_url, current_depth = reader._urls_to_crawl.pop(0)
        if current_url in reader._visited or should_skip(current_url, current_depth, num_links):
            continue
        reader._visited.add(current_url)
        reader.delay()
        num_links += record(current_url, current_depth, crawler_result)
    return crawler_result
