"""ArXiv toolkit behavior against faked arXiv API and PDF responses."""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import arxiv
import pytest
import requests
from reportlab.pdfgen import canvas

from mindroom.custom_tools.arxiv import _download_pdf
from mindroom.tools.arxiv import arxiv_tools

if TYPE_CHECKING:
    from pathlib import Path

_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <opensearch:totalResults>1</opensearch:totalResults>
  <opensearch:startIndex>0</opensearch:startIndex>
  <opensearch:itemsPerPage>100</opensearch:itemsPerPage>
  <entry>
    <id>http://arxiv.org/abs/math/0211159v1</id>
    <updated>2002-11-11T16:11:49Z</updated>
    <published>2002-11-11T16:11:49Z</published>
    <title>The entropy formula: for the Ricci
      flow</title>
    <summary>We present a monotonic expression.</summary>
    <author><name>Grisha Perelman</name></author>
    <arxiv:comment>7 pages</arxiv:comment>
    <link href="http://arxiv.org/abs/math/0211159v1" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/math/0211159v1" rel="related" type="application/pdf"/>
    <arxiv:primary_category term="math.DG"/>
    <category term="math.DG"/>
    <category term="math.GT"/>
  </entry>
</feed>
"""


def _pdf(*page_texts: str) -> bytes:
    written = io.BytesIO()
    document = canvas.Canvas(written)
    for text in page_texts:
        document.drawString(72, 720, text)
        document.showPage()
    document.save()
    return written.getvalue()


@pytest.fixture
def arxiv_http(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Answer the arXiv API with one entry and its PDF URL with a two-page PDF, recording each requested URL."""
    requested: list[str] = []
    pdf = _pdf("First page text", "Second page text")

    def send(_session: requests.Session, request: requests.PreparedRequest, **_kwargs: object) -> requests.Response:
        assert request.url is not None
        requested.append(request.url)
        response = requests.Response()
        response.status_code = 200
        response.request = request
        response.url = request.url
        if request.url.startswith("https://export.arxiv.org/api/query?"):
            response.raw = io.BytesIO(_FEED)
        elif request.url in {
            "http://export.arxiv.org/pdf/math/0211159v1",
            "https://export.arxiv.org/pdf/2103.03404v1",
        }:
            response.raw = io.BytesIO(pdf)
        else:
            response.status_code = 404
            response.raw = io.BytesIO(b"")
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    return requested


def test_read_arxiv_papers_downloads_and_reads_the_pdf(arxiv_http: list[str], tmp_path: Path) -> None:
    """Papers download from the export host under the sanitized filename and return the expected article JSON."""
    toolkit = arxiv_tools()(download_dir=str(tmp_path))

    articles = json.loads(toolkit.read_arxiv_papers(["math/0211159v1"], pages_to_read=1))

    assert articles == [
        {
            "title": "The entropy formula: for the Ricci flow",
            "id": "math/0211159v1",
            "entry_id": "http://arxiv.org/abs/math/0211159v1",
            "authors": ["Grisha Perelman"],
            "primary_category": "math.DG",
            "categories": ["math.DG", "math.GT"],
            "published": "2002-11-11T16:11:49+00:00",
            "pdf_url": "http://arxiv.org/pdf/math/0211159v1",
            "links": ["http://arxiv.org/abs/math/0211159v1", "http://arxiv.org/pdf/math/0211159v1"],
            "summary": "We present a monotonic expression.",
            "comment": "7 pages",
            "content": [{"page": 1, "text": "First page text\n"}],
        },
    ]
    api_url, pdf_url = arxiv_http
    assert parse_qs(urlparse(api_url).query)["id_list"] == ["math/0211159v1"]
    assert pdf_url == "http://export.arxiv.org/pdf/math/0211159v1"
    assert [path.name for path in tmp_path.iterdir()] == ["math_0211159v1.The_entropy_formula__for_the_Ricci_flow.pdf"]


def test_search_and_page_bounds(arxiv_http: list[str], tmp_path: Path) -> None:
    """Search serializes article metadata and read_arxiv_papers honors None and 0 page limits."""
    toolkit = arxiv_tools()(download_dir=tmp_path)

    search_articles = json.loads(toolkit.search_arxiv_and_return_articles("ricci flow", num_articles=3))
    assert len(search_articles) == 1
    assert search_articles[0]["id"] == "math/0211159v1"
    assert "content" not in search_articles[0]
    assert parse_qs(urlparse(arxiv_http[0]).query)["max_results"] == ["3"]

    all_pages = json.loads(toolkit.read_arxiv_papers(["math/0211159v1"]))
    assert [page["page"] for page in all_pages[0]["content"]] == [1, 2]

    zero_pages = json.loads(toolkit.read_arxiv_papers(["math/0211159v1"], pages_to_read=0))
    assert zero_pages[0]["content"] == []


def test_download_pdf_guards_missing_url_scheme_host_and_symlinks(
    arxiv_http: list[str],
    tmp_path: Path,
) -> None:
    """PDF retrieval enforces export.arxiv.org host rewriting and rejects missing URLs, bad schemes, and symlinks."""
    no_pdf = arxiv.Result(entry_id="http://arxiv.org/abs/2103.03404v1", title="No PDF")
    with pytest.raises(ValueError, match="No PDF URL"):
        _download_pdf(no_pdf, tmp_path)

    bad_scheme = arxiv.Result(
        entry_id="http://arxiv.org/abs/2103.03404v1",
        title="Bad Scheme",
        links=[arxiv.Result.Link("file:///etc/passwd", title="pdf")],
    )
    with pytest.raises(ValueError, match="Unsupported PDF URL scheme"):
        _download_pdf(bad_scheme, tmp_path)

    target = tmp_path / "outside.txt"
    target.write_text("keep", encoding="utf-8")
    symlink_path = tmp_path / "2103.03404v1.Symlink_Target.pdf"
    symlink_path.symlink_to(target)
    symlink_result = arxiv.Result(
        entry_id="http://arxiv.org/abs/2103.03404v1",
        title="Symlink Target",
        links=[arxiv.Result.Link("https://evil.example/pdf/2103.03404v1", title="pdf")],
    )
    with pytest.raises(ValueError, match="symlinked PDF path"):
        _download_pdf(symlink_result, tmp_path)
    assert target.read_text(encoding="utf-8") == "keep"

    rewritten_host = arxiv.Result(
        entry_id="http://arxiv.org/abs/2103.03404v1",
        title="Host Rewrite",
        links=[arxiv.Result.Link("https://evil.example:8443/pdf/2103.03404v1", title="pdf")],
    )
    downloaded = _download_pdf(rewritten_host, tmp_path)
    assert downloaded == tmp_path / "2103.03404v1.Host_Rewrite.pdf"
    assert arxiv_http == ["https://export.arxiv.org/pdf/2103.03404v1"]


def test_arxiv_toolkit_flags_and_name() -> None:
    """ArxivTools preserves its toolkit name and respects enable_* and all flags."""
    tool_class = arxiv_tools()

    default_tool = tool_class()
    assert default_tool.name == "arxiv_tools"
    assert list(default_tool.functions) == ["search_arxiv_and_return_articles", "read_arxiv_papers"]

    read_only = tool_class(enable_search_arxiv=False, enable_read_arxiv_papers=True)
    assert list(read_only.functions) == ["read_arxiv_papers"]

    all_override = tool_class(enable_search_arxiv=False, enable_read_arxiv_papers=False, all=True)
    assert list(all_override.functions) == ["search_arxiv_and_return_articles", "read_arxiv_papers"]
