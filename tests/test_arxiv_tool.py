"""ArXiv toolkit behavior against faked arXiv API and PDF responses."""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from reportlab.pdfgen import canvas

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
        elif request.url == "http://export.arxiv.org/pdf/math/0211159v1":
            response.raw = io.BytesIO(pdf)
        else:
            response.status_code = 404
            response.raw = io.BytesIO(b"")
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    return requested


def test_read_arxiv_papers_downloads_and_reads_the_pdf(arxiv_http: list[str], tmp_path: Path) -> None:
    """Papers download from the export host under arxiv 3's file name and return Agno's article JSON."""
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
