"""PubMed tool configuration."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.pubmed import PubmedTools


@register_tool_with_metadata(
    name="pubmed",
    file_access=ToolFileAccess.NONE,
    display_name="PubMed",
    description="Search and retrieve medical and life science literature from PubMed",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="SiPubmed",
    icon_color="text-blue-600",  # Medical blue
    config_fields=[
        ConfigField(
            name="email",
            label="Email",
            type="text",
            required=False,
            default="your_email@example.com",
        ),
        ConfigField(
            name="max_results",
            label="Max Results",
            type="number",
            required=False,
            default=None,
        ),
        ConfigField(
            name="results_expanded",
            label="Results Expanded",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_search_pubmed",
            label="Enable Search Pubmed",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
        ),
    ],
    dependencies=["httpx"],
    docs_url="https://docs.agno.com/tools/toolkits/search/pubmed",
    function_names=("search_pubmed",),
)
def pubmed_tools() -> type[PubmedTools]:
    """Return PubMed tools for medical research and literature search."""
    from agno.tools.pubmed import PubmedTools

    class MindRoomPubmedTools(PubmedTools):
        """PubMed toolkit whose configured result limit is the call default."""

        def search_pubmed(self, query: str, max_results: int | None = None) -> str:
            """Search PubMed, using the configured max_results when the call omits it."""
            if max_results == 0:
                return "[]"
            resolved_max_results = self.max_results if max_results is None else max_results
            if self.results_expanded:
                return super().search_pubmed(query, max_results=resolved_max_results)

            # AGNO_COMPAT: Concise PubMed results lose metadata for short abstracts.
            # Reason: Upstream's conditional expression includes the title/year
            # prefix only when the summary exceeds 200 characters.
            # Upstream issue: Tracking gap; preserve the documented concise fields.
            # Upstream PR: No verified fix identified.
            # Remove when: Agno retains title/year independently of truncation.
            # Coverage: tests/test_pubmed_tools.py; tests/test_tool_config_sync.py.
            try:
                ids = self.fetch_pubmed_ids(query, resolved_max_results or 10, self.email)
                articles = self.parse_details(self.fetch_details(ids))
                results = []
                for article in articles:
                    summary = article.get("Summary", "")
                    if len(summary) > 200:
                        summary = f"{summary[:200]}..."
                    results.append(
                        f"Title: {article.get('Title')}\nPublished: {article.get('Published')}\nSummary: {summary}",
                    )
                return json.dumps(results)
            except Exception as error:
                return f"Could not fetch articles. Error: {error}"

    return MindRoomPubmedTools
