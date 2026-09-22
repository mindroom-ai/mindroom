"""Structured search settings must survive authored configuration and tool construction."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import httpx
import pytest
import yaml

from mindroom.config.main import load_config
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import CredentialsManager
from mindroom.tool_system.metadata import get_tool_by_name

if TYPE_CHECKING:
    from pathlib import Path

    from agno.tools import Toolkit
    from agno.tools.exa import ExaTools
    from agno.tools.firecrawl import FirecrawlTools
    from agno.tools.searxng import Searxng


def _configured_tool(tmp_path: Path, tool_name: str, overrides: dict[str, object]) -> Toolkit:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
                "agents": {"research": {"display_name": "Research", "tools": [{tool_name: overrides}]}},
            },
        ),
    )
    paths = resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "state", process_env={})
    config = load_config(paths)
    entry = next(entry for entry in config.resolve_entity("research").tool_configs if entry.name == tool_name)
    return get_tool_by_name(
        tool_name,
        paths,
        worker_target=None,
        disable_sandbox_proxy=True,
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        credential_overrides={"api_key": "synthetic-search-key"} if tool_name != "searxng" else None,
        tool_config_overrides=entry.tool_config_overrides,
    )


@pytest.mark.parametrize("domains", [["matrix.org", "element.io"], []])
def test_exa_authored_domain_lists_reach_tool(tmp_path: Path, domains: list[str]) -> None:
    """Both domain lists retain their collection values through config and factory."""
    tool = cast("ExaTools", _configured_tool(tmp_path, "exa", {"include_domains": domains, "exclude_domains": domains}))

    assert tool.include_domains == domains
    assert tool.exclude_domains == domains


@pytest.mark.parametrize("formats", [["markdown", "html"], []])
def test_firecrawl_authored_formats_reach_tool(tmp_path: Path, formats: list[str]) -> None:
    """The toolkit receives a format list, including an explicitly empty list."""
    tool = cast("FirecrawlTools", _configured_tool(tmp_path, "firecrawl", {"formats": formats}))

    assert tool.formats == formats


@pytest.mark.parametrize("engines", [["duckduckgo", "wikipedia"], []])
def test_searxng_authored_engines_reach_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engines: list[str],
) -> None:
    """The outgoing query names engines rather than joining characters from text."""
    requests: list[httpx.URL] = []

    def get(url: str, *, timeout: int) -> httpx.Response:
        assert timeout == 30
        request = httpx.Request("GET", url)
        requests.append(request.url)
        return httpx.Response(200, json={"results": []}, request=request)

    monkeypatch.setattr(httpx, "get", get)
    tool = cast(
        "Searxng",
        _configured_tool(tmp_path, "searxng", {"host": "https://search.example.com", "engines": engines}),
    )

    assert json.loads(tool.search_web("Matrix federation")) == {"results": []}
    assert len(requests) == 1
    assert requests[0].params.get("engines") == (",".join(engines) if engines else None)
