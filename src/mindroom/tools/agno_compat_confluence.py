"""Keep Agno's Confluence toolkit on the v1 REST client and page lookups it was written for."""

from __future__ import annotations

from typing import Any

import requests
from agno.tools.confluence import ConfluenceTools
from atlassian.confluence.server import Server

_PAGE_BATCH = 50


class _V1Confluence(Server):
    """The v1 REST client of atlassian-python-api 5, answering the lookups Agno calls in the shape 4.x returned."""

    def get_page_by_title(  # ty: ignore[invalid-method-override] - 4.x signature
        self,
        space_key: str,
        title: str,
        expand: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the first page with this title in the space, or None."""
        params: dict[str, Any] = {"start": 0, "limit": 1}
        if expand is not None:
            params["expand"] = expand
        results = super().get_page_by_title(space_key, title, **params).get("results")
        return results[0] if results else None

    def get_all_pages_from_space(  # ty: ignore[invalid-method-override] - 4.x signature
        self,
        space_key: str,
        status: str | None = None,
        expand: str | None = None,
        content_type: str = "page",
    ) -> list[dict[str, Any]]:
        """Return every page of the space as a list, reading it in batches by offset."""
        pages: list[dict[str, Any]] = []
        while True:
            batch = self.get_all_pages_from_space_raw(
                space=space_key,
                start=len(pages),
                limit=_PAGE_BATCH,
                status=status,
                expand=expand,
                content_type=content_type,
            ).get("results", [])
            pages.extend(batch)
            if len(batch) < _PAGE_BATCH:
                return pages


# AGNO_COMPAT: ConfluenceTools calls atlassian-python-api 4's Confluence client, which version 5 split apart.
# Reason: Agno 3.2.1 passes a space key to create_page, calls update_page, reads get_all_spaces as a v1 page with
# "results", and expects get_page_by_title to return one page or None and get_all_pages_from_space a list. In
# atlassian-python-api 5, Confluence(url=...) picks a Cloud client for *.atlassian.net URLs that has no create_page
# or update_page and lists spaces as a v2 generator, and both its Cloud and Server get_page_by_title return the
# whole search response and get_all_pages_from_space a generator, so most of the toolkit fails or misreports.
# The toolkit therefore talks to the v1 REST API as 4.0.7 did, for Cloud and Server URLs alike.
# Upstream issue: Tracking gap; a search of agno-agi/agno issues on November 20, 2026 found none. Agno pinned
# atlassian-python-api<5 in https://github.com/agno-agi/agno/pull/9601 and called porting the toolkit a follow-up.
# Upstream PR: None identified.
# Remove when: ConfluenceTools works with atlassian-python-api 5 on both Atlassian Cloud and Server URLs.
# Coverage: tests/test_confluence_tool.py::test_get_page_content_returns_the_page_or_a_not_found_error,
# tests/test_confluence_tool.py::test_create_page_posts_the_v1_content_body,
# tests/test_confluence_tool.py::test_update_page_puts_the_next_version, and
# tests/test_confluence_tool.py::test_space_and_page_listings.
class V1ConfluenceTools(ConfluenceTools):
    """ConfluenceTools on the v1 REST API, reached the way atlassian-python-api 4.0.7 reached it."""

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        url: str | None = None,
        api_key: str | None = None,
        verify_ssl: bool = True,
        **kwargs: Any,  # noqa: ANN401 - Agno toolkit options
    ) -> None:
        super().__init__(
            username=username,
            password=password,
            url=url,
            api_key=api_key,
            verify_ssl=verify_ssl,
            **kwargs,
        )
        site_url = str(self.url)
        # atlassian-python-api 4.0.7 served Atlassian Cloud sites from their /wiki context.
        cloud = ("atlassian.net" in site_url or "jira.com" in site_url) and "/wiki" not in site_url
        session = requests.Session()
        session.verify = verify_ssl
        self.confluence = _V1Confluence(
            url=Server.url_joiner(site_url, "/wiki") if cloud else site_url,
            username=self.username,
            password=self.password,
            verify_ssl=verify_ssl,
            session=session,
            cloud=cloud,
        )
