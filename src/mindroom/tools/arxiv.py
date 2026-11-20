"""ArXiv tool configuration."""

from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agno.tools.arxiv import ArxivTools

_PDF_DOWNLOAD_TIMEOUT_SECONDS = 60


@register_tool_with_metadata(
    name="arxiv",
    file_access=ToolFileAccess.NONE,
    display_name="ArXiv",
    description="Search and read academic papers from ArXiv",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="SiArxiv",
    icon_color="text-red-600",  # ArXiv red
    config_fields=[
        ConfigField(
            name="enable_search_arxiv",
            label="Enable Search Arxiv",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_read_arxiv_papers",
            label="Enable Read Arxiv Papers",
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
            name="download_dir",
            label="Download Dir",
            type="text",
            required=False,
            default=None,
        ),
    ],
    dependencies=["arxiv", "pypdf"],
    docs_url="https://docs.mindroom.chat/tools/research-sources/#arxiv",
    function_names=("read_arxiv_papers", "search_arxiv_and_return_articles"),
)
def arxiv_tools() -> type[ArxivTools]:
    """Return ArXiv tools for academic paper research."""
    import arxiv
    import requests
    from agno.tools.arxiv import ArxivTools

    def download_pdf(result: arxiv.Result, dirpath: str) -> str:
        """Save a result's PDF into ``dirpath`` under the name and from the host arxiv 3's ``download_pdf`` used."""
        if result.pdf_url is None:
            msg = "No PDF URL available for this result"
            raise ValueError(msg)
        filename = ".".join(
            [result.get_short_id().replace("/", "_"), re.sub(r"[^\w]", "_", result.title or "UNTITLED"), "pdf"],
        )
        path = Path(dirpath) / filename
        pdf_url = urlparse(result.pdf_url)._replace(netloc="export.arxiv.org").geturl()
        with requests.get(pdf_url, stream=True, timeout=_PDF_DOWNLOAD_TIMEOUT_SECONDS) as response:
            response.raise_for_status()
            with path.open("wb") as file:
                for chunk in response.iter_content(chunk_size=65536):
                    file.write(chunk)
        return str(path)

    # AGNO_COMPAT: ArxivTools reads papers through Result.download_pdf, which arxiv 4 removed.
    # Reason: Agno 3.2.1 read_arxiv_papers calls result.download_pdf(dirpath=...), so with arxiv 4 every paper
    # fails with AttributeError and the tool returns an empty list.
    # Upstream issue: https://github.com/agno-agi/agno/issues/10771
    # Upstream PR: https://github.com/agno-agi/agno/pull/10769, open on November 20, 2026; it saves the PDF as
    # "<id>.pdf" instead of arxiv 3's "<id>.<title>.pdf".
    # Remove when: read_arxiv_papers downloads Result.pdf_url itself.
    # Coverage: tests/test_arxiv_tool.py::test_read_arxiv_papers_downloads_and_reads_the_pdf.
    class _PdfDownloadingClient(arxiv.Client):
        """Give each result back the ``download_pdf`` method that Agno calls."""

        def results(self, search: arxiv.Search, offset: int = 0) -> Iterator[arxiv.Result]:
            for result in super().results(search, offset):
                result.download_pdf = functools.partial(download_pdf, result)  # ty: ignore[missing-attribute]
                yield result

    class MindRoomArxivTools(ArxivTools):
        """ArXiv toolkit accepting serialized authored download paths and downloading PDFs with arxiv 4."""

        def __init__(
            self,
            enable_search_arxiv: bool = True,
            enable_read_arxiv_papers: bool = True,
            all: bool = False,  # noqa: A002
            download_dir: Path | str | None = None,
            **kwargs: object,
        ) -> None:
            if isinstance(download_dir, str):
                resolved_download_dir = Path(download_dir) if download_dir else None
            else:
                resolved_download_dir = download_dir
            super().__init__(
                enable_search_arxiv=enable_search_arxiv,
                enable_read_arxiv_papers=enable_read_arxiv_papers,
                all=all,
                download_dir=resolved_download_dir,
                **kwargs,
            )
            self.client = _PdfDownloadingClient()

    return MindRoomArxivTools
