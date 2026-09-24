"""Separately connected Atlassian sites registered through an ordinary plugin."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import ValidationError

from mindroom.config.main import Config
from mindroom.custom_tools.atlassian import AtlassianToolkit
from mindroom.oauth.atlassian import atlassian_function_names, atlassian_oauth_provider
from mindroom.oauth.registry import load_oauth_providers
from mindroom.tool_system.atlassian_connections import (
    AtlassianConnectionConfig,
    atlassian_connection_oauth_provider,
)
from mindroom.tool_system.catalog import TOOL_METADATA
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.plugins import isolated_plugin_runtime
from mindroom.tool_system.registry_state import BUILTIN_TOOL_METADATA
from tests.atlassian_test_support import (
    CLOUD_ID,
    OTHER_CLOUD_ID,
    OTHER_SITE_URL,
    SITE_URL,
    FakeGateway,
    bearer,
    gateway_url,
    publish_grant,
    runtime_paths,
    save_client_config,
    site,
    worker_target,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

DEFAULT_TOKEN = "default-connection-token"  # noqa: S105
PARTNER_TOKEN = "partner-connection-token"  # noqa: S105
CONNECTIONS = """
from mindroom.tool_system.atlassian_connections import (
    AtlassianConnectionConfig,
    atlassian_connection_oauth_provider,
    register_atlassian_connection_tools,
)

CONNECTIONS = (
    AtlassianConnectionConfig(
        name="partner",
        display_name="Partner Confluence",
        site_url="https://acme.atlassian.net/wiki",
        cloud_id="00000000-0000-4000-8000-000000000002",
        products=("confluence",),
    ),
    AtlassianConnectionConfig(
        name="archive",
        display_name="Archive",
        site_url="https://archive.atlassian.net",
        write=False,
        client_config_service="archive_atlassian_oauth_client",
    ),
)

for connection in CONNECTIONS:
    register_atlassian_connection_tools(connection)


def register_oauth_providers(settings, runtime_paths):
    return [atlassian_connection_oauth_provider(connection) for connection in CONNECTIONS]
"""


def _plugin(tmp_path: Path, source: str = CONNECTIONS) -> tuple[Config, RuntimePaths]:
    plugin = tmp_path / "atlassian_sites"
    plugin.mkdir()
    (plugin / "mindroom.plugin.json").write_text(
        json.dumps({"name": "atlassian-sites", "tools_module": "sites.py", "oauth_module": "sites.py"}),
    )
    (plugin / "sites.py").write_text(source)
    (tmp_path / "config.yaml").write_text("agents: {}\n")
    return Config(plugins=[str(plugin)]), runtime_paths(tmp_path)


def _connection(**overrides: Any) -> AtlassianConnectionConfig:  # noqa: ANN401
    fields: dict[str, Any] = {"name": "partner", "display_name": "Partner", "site_url": OTHER_SITE_URL, **overrides}
    return AtlassianConnectionConfig(**fields)


def test_connection_config_normalizes_the_site_pin() -> None:
    """Site URLs reduce to their origin and cloud IDs to canonical lowercase UUIDs."""
    connection = _connection(site_url=" https://ACME.atlassian.net/wiki/ ", cloud_id=OTHER_CLOUD_ID.upper())

    assert connection.site_url == OTHER_SITE_URL
    assert connection.cloud_id == OTHER_CLOUD_ID
    assert connection.provider_id == "partner_atlassian"
    assert connection.products == ("jira", "confluence")
    assert connection.write is True
    assert connection.client_config_service == "atlassian_oauth_client"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"name": "Partner"}, "name"),
        ({"name": "partner-site"}, "name"),
        ({"name": "a_name_that_is_far_too_long"}, "name"),
        ({"display_name": " "}, "display_name"),
        ({"site_url": None}, "site_url or cloud_id"),
        ({"site_url": "http://acme.atlassian.net"}, "https"),
        ({"site_url": "https://user:secret@acme.atlassian.net"}, "credentials"),
        ({"site_url": "https://acme.atlassian.net/?next=https://example.com"}, "query"),
        ({"cloud_id": "../../oauth/token"}, "UUID"),
        ({"products": ()}, "products"),
        ({"products": ("jira", "jira")}, "products"),
        ({"products": ("bitbucket",)}, "products"),
        ({"client_config_service": "partner_client"}, "client_config_service"),
        ({"scopes": ["read:jira-work"]}, "scopes"),
    ],
)
def test_connection_config_rejects_unsafe_or_ambiguous_values(overrides: dict[str, Any], message: str) -> None:
    """Invalid connections fail before any provider or tool is registered."""
    with pytest.raises(ValidationError, match=message):
        _connection(**overrides)


def test_connection_provider_is_independent_of_the_default_connection() -> None:
    """Each connection has its own provider ID, token store, callback, and scopes, sharing only the app."""
    default = atlassian_oauth_provider()
    partner = atlassian_connection_oauth_provider(_connection(products=("confluence",), write=False))

    assert partner.id == "partner_atlassian"
    assert partner.display_name == "Partner"
    assert partner.credential_service == "partner_atlassian_oauth"
    assert partner.tool_config_service == "partner_atlassian"
    assert partner.redirect_path == "/api/oauth/partner_atlassian/callback"
    assert partner.shared_client_config_services == default.shared_client_config_services
    assert partner.requester_scoped_credentials is True
    assert partner.scopes == (
        "offline_access",
        "search:confluence",
        "read:confluence-content.all",
        "readonly:content.attachment:confluence",
    )
    assert {default.credential_service, default.redirect_path}.isdisjoint(
        {partner.credential_service, partner.redirect_path},
    )


def test_plugin_registers_prefixed_tools_and_providers_beside_the_builtin(tmp_path: Path) -> None:
    """Connections register plugin-owned tools and providers, leaving the built-in tool unchanged."""
    config, paths = _plugin(tmp_path)
    builtin_functions = TOOL_METADATA["atlassian"].function_names

    with isolated_plugin_runtime(config, paths):
        partner = TOOL_METADATA["partner_atlassian"]
        archive = TOOL_METADATA["archive_atlassian"]
        providers = load_oauth_providers(config, paths, skip_broken_plugins=False)

        assert "partner_atlassian" not in BUILTIN_TOOL_METADATA
        assert partner.auth_provider == "partner_atlassian"
        assert partner.requires_primary_runtime is True
        assert partner.function_names == atlassian_function_names(("confluence",), prefix="partner_")
        assert archive.function_names == atlassian_function_names(write=False, prefix="archive_")
        assert not any("create" in name or "update" in name for name in archive.function_names)
        assert TOOL_METADATA["atlassian"].function_names == builtin_functions
        assert {"atlassian", "partner_atlassian", "archive_atlassian"} <= set(providers)
        assert providers["archive_atlassian"].shared_client_config_services == ("archive_atlassian_oauth_client",)
        assert "write:jira-work" not in providers["archive_atlassian"].scopes

    assert "partner_atlassian" not in TOOL_METADATA


def test_connection_toolkit_exposes_only_its_prefixed_functions(tmp_path: Path) -> None:
    """The built toolkit matches its catalog entry, and function filters use the visible names."""
    config, paths = _plugin(tmp_path)
    save_client_config(paths)

    with isolated_plugin_runtime(config, paths):
        tool = _build("partner_atlassian", paths)
        filtered = get_tool_by_name(
            "partner_atlassian",
            paths,
            worker_target=worker_target(),
            disable_sandbox_proxy=True,
            tool_config_overrides={"include_tools": ["partner_confluence_search"]},
        )

        assert tuple(tool.async_functions) == TOOL_METADATA["partner_atlassian"].function_names
        assert tool.name == "partner_atlassian"
        assert set(filtered.async_functions) == {"partner_confluence_search"}


@pytest.mark.asyncio
async def test_connections_share_one_app_but_use_their_own_callbacks(tmp_path: Path) -> None:
    """The shared app client authorizes each connection with its own callback and requested scopes."""
    config, paths = _plugin(tmp_path)
    save_client_config(paths)

    with isolated_plugin_runtime(config, paths):
        providers = load_oauth_providers(config, paths)
        default_url = await providers["atlassian"].authorization_uri_async(paths, state="a")
        partner_url = await providers["partner_atlassian"].authorization_uri_async(paths, state="b")

    default_query = parse_qs(urlparse(default_url).query)
    partner_query = parse_qs(urlparse(partner_url).query)
    assert default_query["client_id"] == partner_query["client_id"] == ["atlassian-client"]
    assert default_query["redirect_uri"] == ["https://chat.example.com/api/oauth/atlassian/callback"]
    assert partner_query["redirect_uri"] == ["https://chat.example.com/api/oauth/partner_atlassian/callback"]
    assert "read:jira-work" in default_query["scope"][0]
    assert "read:jira-work" not in partner_query["scope"][0]


def test_duplicate_connection_names_are_rejected(tmp_path: Path) -> None:
    """Two connections with one name would share credentials, so registration fails."""
    source = CONNECTIONS.replace('name="archive"', 'name="partner"')
    config, paths = _plugin(tmp_path, source)

    with (
        pytest.raises(Exception, match="'partner_atlassian' is registered multiple times"),
        isolated_plugin_runtime(config, paths),
    ):
        load_oauth_providers(config, paths, skip_broken_plugins=False)


def _build(name: str, paths: RuntimePaths, requester_id: str | None = None) -> AtlassianToolkit:
    tool = get_tool_by_name(
        name,
        paths,
        worker_target=worker_target(requester_id) if requester_id else worker_target(),
        disable_sandbox_proxy=True,
    )
    assert isinstance(tool, AtlassianToolkit)
    return tool


async def _call(tool: AtlassianToolkit, function_name: str, **kwargs: object) -> dict[str, Any]:
    entrypoint = tool.async_functions[function_name].entrypoint
    assert entrypoint is not None
    return json.loads(await entrypoint(**kwargs))


@pytest.mark.asyncio
async def test_default_grant_never_authorizes_another_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection without its own grant asks for its own login, even when the default one is connected."""
    config, paths = _plugin(tmp_path)
    manager = save_client_config(paths)
    publish_grant(atlassian_oauth_provider(), manager, DEFAULT_TOKEN)
    gateway = FakeGateway(sites_by_token={DEFAULT_TOKEN: [site(), site(OTHER_CLOUD_ID, OTHER_SITE_URL)]}).install(
        monkeypatch,
    )

    with isolated_plugin_runtime(config, paths):
        result = await _call(_build("partner_atlassian", paths), "partner_confluence_search", cql="type = page")

    assert result["oauth_connection_required"] is True
    assert result["provider"] == "partner_atlassian"
    assert "/api/oauth/partner_atlassian/authorize?connect_token=" in result["connect_url"]
    assert gateway.requests == []


@pytest.mark.asyncio
async def test_each_connection_uses_only_its_own_token_and_pinned_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tokens and sites never cross connections, even when one account can reach both sites."""
    config, paths = _plugin(tmp_path)
    manager = save_client_config(paths)
    both_sites = [site(), site(OTHER_CLOUD_ID, OTHER_SITE_URL, name="acme")]
    gateway = FakeGateway(sites_by_token={DEFAULT_TOKEN: both_sites, PARTNER_TOKEN: both_sites}).install(monkeypatch)
    for cloud_id in (CLOUD_ID, OTHER_CLOUD_ID):
        gateway.route("GET", gateway_url("confluence", "/wiki/rest/api/search", cloud_id), {"results": []})

    with isolated_plugin_runtime(config, paths):
        providers = load_oauth_providers(config, paths)
        publish_grant(providers["atlassian"], manager, DEFAULT_TOKEN)
        publish_grant(providers["partner_atlassian"], manager, PARTNER_TOKEN)
        default = AtlassianToolkit(
            provider=providers["atlassian"],
            function_prefix="",
            products=("confluence",),
            write=True,
            site_url=SITE_URL,
            cloud_id=None,
            runtime_paths=paths,
            credentials_manager=manager,
            worker_target=worker_target(),
            runtime_config=None,
        )
        partner_result = await _call(_build("partner_atlassian", paths), "partner_confluence_search", cql="type = page")
        default_result = await _call(default, "confluence_search", cql="type = page")

    assert partner_result["site"]["cloud_id"] == OTHER_CLOUD_ID
    assert default_result["site"]["cloud_id"] == CLOUD_ID
    calls = [(request.url.path.split("/")[3], bearer(request)) for request in gateway.product_requests()]
    assert calls == [(OTHER_CLOUD_ID, PARTNER_TOKEN), (CLOUD_ID, DEFAULT_TOKEN)]


@pytest.mark.asyncio
async def test_connection_never_falls_back_to_another_reachable_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant that cannot reach the pinned site gets site_not_found, not the other site's data."""
    config, paths = _plugin(tmp_path)
    manager = save_client_config(paths)
    gateway = FakeGateway(sites_by_token={PARTNER_TOKEN: [site()]}).install(monkeypatch)
    gateway.route("GET", gateway_url("confluence", "/wiki/rest/api/search"), {"results": []})

    with isolated_plugin_runtime(config, paths):
        publish_grant(load_oauth_providers(config, paths)["partner_atlassian"], manager, PARTNER_TOKEN)
        result = await _call(_build("partner_atlassian", paths), "partner_confluence_search", cql="type = page")

    assert result["code"] == "site_not_found"
    assert result["configured_cloud_id"] == OTHER_CLOUD_ID
    assert gateway.product_requests() == []


@pytest.mark.asyncio
async def test_requesters_never_share_a_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One requester's grant for a connection never authorizes another requester's calls."""
    config, paths = _plugin(tmp_path)
    manager = save_client_config(paths)
    gateway = FakeGateway(sites_by_token={PARTNER_TOKEN: [site(OTHER_CLOUD_ID, OTHER_SITE_URL)]}).install(monkeypatch)

    with isolated_plugin_runtime(config, paths):
        publish_grant(load_oauth_providers(config, paths)["partner_atlassian"], manager, PARTNER_TOKEN)
        result = await _call(
            _build("partner_atlassian", paths, requester_id="@bob:example.org"),
            "partner_confluence_get_page",
            page_id="1",
        )

    assert result["oauth_connection_required"] is True
    assert gateway.requests == []
