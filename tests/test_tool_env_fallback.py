"""Tool settings read from the environment and the `.env` beside `config.yaml`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import mindroom.tools  # noqa: F401
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.credentials.manager import CredentialsManager
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, ToolCategory, ToolFileAccess
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tool_system.registry_state import (
    BUILTIN_TOOL_METADATA,
    capture_tool_registry_snapshot,
    restore_tool_registry_snapshot,
)

if TYPE_CHECKING:
    from pathlib import Path

# The environment variables behind each toolkit constructor parameter: the `getenv` reads in Agno 3.0.9's
# `agno/tools` modules, and the reads of MindRoom's own `agentql` and `github` subclasses.
# Re-check them on every Agno upgrade.
# Reads left undeclared on purpose:
# - `gemini` `vertexai` (`GOOGLE_GENAI_USE_VERTEXAI`): Agno counts only the exact text `true` and google-genai also
#   counts `1`, while the boolean field accepts only `true` and `false`, so a declaration would fail builds that work.
# - `openai` and `groq` `api_key` (`OPENAI_API_KEY`, `GROQ_API_KEY`): the provider key import supplies them for
#   unscoped agents; whether scoped agents also read them from the environment is an open decision.
# - `docker` writes `DOCKER_CONFIG` instead of reading it; the Google toolkits read `GOOGLE_API_TIMEOUT` and
#   `GOOGLE_PROJECT_ID` in code paths MindRoom's own authentication replaces, and no constructor parameter backs them.
AGNO_ENV_READS: dict[str, dict[str, tuple[str, ...]]] = {
    "agentql": {"api_key": ("AGENTQL_API_KEY",)},
    "apify": {"apify_api_token": ("APIFY_API_TOKEN",)},
    "bitbucket": {
        "username": ("BITBUCKET_USERNAME",),
        "password": ("BITBUCKET_PASSWORD",),
        "token": ("BITBUCKET_TOKEN",),
    },
    "brandfetch": {"api_key": ("BRANDFETCH_API_KEY",), "client_id": ("BRANDFETCH_CLIENT_ID",)},
    # Agno lets the process environment override a saved zone; MindRoom keeps that.
    "brightdata": {
        "api_key": ("BRIGHT_DATA_API_KEY",),
        "web_unlocker_zone": ("BRIGHT_DATA_WEB_UNLOCKER_ZONE",),
        "serp_zone": ("BRIGHT_DATA_SERP_ZONE",),
    },
    "browserbase": {
        "api_key": ("BROWSERBASE_API_KEY",),
        "project_id": ("BROWSERBASE_PROJECT_ID",),
        "base_url": ("BROWSERBASE_BASE_URL",),
    },
    "cal_com": {"api_key": ("CALCOM_API_KEY",), "event_type_id": ("CALCOM_EVENT_TYPE_ID",)},
    "cartesia": {"api_key": ("CARTESIA_API_KEY",)},
    "clickup": {"api_key": ("CLICKUP_API_KEY",), "master_space_id": ("MASTER_SPACE_ID",)},
    "confluence": {
        "url": ("CONFLUENCE_URL",),
        "username": ("CONFLUENCE_USERNAME",),
        "api_key": ("CONFLUENCE_API_KEY",),
        "password": ("CONFLUENCE_PASSWORD",),
    },
    "daytona": {"api_key": ("DAYTONA_API_KEY",), "api_url": ("DAYTONA_API_URL",)},
    "discord": {"bot_token": ("DISCORD_BOT_TOKEN",)},
    "e2b": {"api_key": ("E2B_API_KEY",)},
    "eleven_labs": {"api_key": ("ELEVEN_LABS_API_KEY",)},
    "exa": {"api_key": ("EXA_API_KEY",)},
    "fal": {"api_key": ("FAL_API_KEY",)},
    "financial_datasets_api": {"api_key": ("FINANCIAL_DATASETS_API_KEY",)},
    "firecrawl": {"api_key": ("FIRECRAWL_API_KEY",)},
    "gemini": {
        "api_key": ("GOOGLE_API_KEY",),
        "project_id": ("GOOGLE_CLOUD_PROJECT",),
        "location": ("GOOGLE_CLOUD_LOCATION",),
    },
    "giphy": {"api_key": ("GIPHY_API_KEY",)},
    "github": {"access_token": ("GITHUB_ACCESS_TOKEN",)},
    "google_maps": {"key": ("GOOGLE_MAPS_API_KEY",)},
    "jina": {"api_key": ("JINA_API_KEY",)},
    "jira": {
        "server_url": ("JIRA_SERVER_URL",),
        "username": ("JIRA_USERNAME",),
        "password": ("JIRA_PASSWORD",),
        "token": ("JIRA_TOKEN",),
    },
    "linear": {"api_key": ("LINEAR_API_KEY",)},
    "linkup": {"api_key": ("LINKUP_API_KEY",)},
    "lumalabs": {"api_key": ("LUMAAI_API_KEY",)},
    "mem0": {"api_key": ("MEM0_API_KEY",), "org_id": ("MEM0_ORG_ID",), "project_id": ("MEM0_PROJECT_ID",)},
    "modelslabs": {"api_key": ("MODELS_LAB_API_KEY",)},
    "neo4j": {"uri": ("NEO4J_URI",), "user": ("NEO4J_USERNAME",), "password": ("NEO4J_PASSWORD",)},
    "notion": {"api_key": ("NOTION_API_KEY",), "database_id": ("NOTION_DATABASE_ID",)},
    "openbb": {"openbb_pat": ("OPENBB_PAT",)},
    "openweather": {"api_key": ("OPENWEATHER_API_KEY",)},
    "oxylabs": {"username": ("OXYLABS_USERNAME",), "password": ("OXYLABS_PASSWORD",)},
    "reddit": {
        "client_id": ("REDDIT_CLIENT_ID",),
        "client_secret": ("REDDIT_CLIENT_SECRET",),
        "user_agent": ("REDDIT_USER_AGENT",),
        "username": ("REDDIT_USERNAME",),
        "password": ("REDDIT_PASSWORD",),
    },
    "redshift": {
        "host": ("REDSHIFT_HOST",),
        "database": ("REDSHIFT_DATABASE",),
        "cluster_identifier": ("REDSHIFT_CLUSTER_IDENTIFIER",),
        "region": ("AWS_REGION", "AWS_DEFAULT_REGION"),
        "db_user": ("REDSHIFT_DB_USER",),
        "access_key_id": ("AWS_ACCESS_KEY_ID",),
        "secret_access_key": ("AWS_SECRET_ACCESS_KEY",),
        "session_token": ("AWS_SESSION_TOKEN",),
        "profile": ("AWS_PROFILE",),
    },
    "replicate": {"api_key": ("REPLICATE_API_KEY",)},
    "resend": {"api_key": ("RESEND_API_KEY",)},
    "scrapegraph": {"api_key": ("SGAI_API_KEY",)},
    "serpapi": {"api_key": ("SERP_API_KEY",)},
    "serper": {"api_key": ("SERPER_API_KEY",)},
    "shopify": {"shop_name": ("SHOPIFY_SHOP_NAME",), "access_token": ("SHOPIFY_ACCESS_TOKEN",)},
    "tavily": {"api_key": ("TAVILY_API_KEY",), "api_base_url": ("TAVILY_API_BASE_URL",)},
    "telegram": {"token": ("TELEGRAM_TOKEN",), "chat_id": ("TELEGRAM_CHAT_ID",)},
    "todoist": {"api_token": ("TODOIST_API_TOKEN",)},
    "trello": {"api_key": ("TRELLO_API_KEY",), "api_secret": ("TRELLO_API_SECRET",), "token": ("TRELLO_TOKEN",)},
    "twilio": {
        "account_sid": ("TWILIO_ACCOUNT_SID",),
        "auth_token": ("TWILIO_AUTH_TOKEN",),
        "api_key": ("TWILIO_API_KEY",),
        "api_secret": ("TWILIO_API_SECRET",),
        "region": ("TWILIO_REGION",),
        "edge": ("TWILIO_EDGE",),
    },
    "unsplash": {"access_key": ("UNSPLASH_ACCESS_KEY",)},
    "webex": {"access_token": ("WEBEX_ACCESS_TOKEN",)},
    "whatsapp": {
        "access_token": ("WHATSAPP_ACCESS_TOKEN",),
        "phone_number_id": ("WHATSAPP_PHONE_NUMBER_ID",),
        "recipient_waid": ("WHATSAPP_RECIPIENT_WAID",),
        "version": ("WHATSAPP_VERSION",),
    },
    "x": {
        "bearer_token": ("X_BEARER_TOKEN",),
        "consumer_key": ("X_CONSUMER_KEY",),
        "consumer_secret": ("X_CONSUMER_SECRET",),
        "access_token": ("X_ACCESS_TOKEN",),
        "access_token_secret": ("X_ACCESS_TOKEN_SECRET",),
    },
    "zendesk": {
        "username": ("ZENDESK_USERNAME",),
        "password": ("ZENDESK_PASSWORD",),
        "company_name": ("ZENDESK_COMPANY_NAME",),
    },
    "zep": {"api_key": ("ZEP_API_KEY",)},
    "zoom": {
        "account_id": ("ZOOM_ACCOUNT_ID",),
        "client_id": ("ZOOM_CLIENT_ID",),
        "client_secret": ("ZOOM_CLIENT_SECRET",),
    },
}


@pytest.mark.parametrize("tool_name", sorted(BUILTIN_TOOL_METADATA))
def test_each_tool_declares_the_environment_variables_its_toolkit_reads(tool_name: str) -> None:
    """A tool declares exactly the variables behind its toolkit's constructor parameters, in the toolkit's order."""
    declared = {field.name: field.env for field in BUILTIN_TOOL_METADATA[tool_name].config_fields or () if field.env}

    assert declared == AGNO_ENV_READS.get(tool_name, {})


def _tavily(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    process_env: dict[str, str] | None = None,
    env_file: str = "",
    saved: dict[str, Any] | None = None,
    inline: dict[str, object] | None = None,
) -> Any:  # noqa: ANN401
    """Build `tavily` the way a bare `mindroom run` does, with the given environment, `.env`, and settings."""
    # A bare run reads the process environment once, into its runtime; nothing outside it may reach the toolkit.
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_BASE_URL", raising=False)
    (tmp_path / "config.yaml").write_text("agents: {}\n")
    (tmp_path / ".env").write_text(env_file)
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env={"MINDROOM_NO_AUTO_INSTALL_TOOLS": "1", **(process_env or {})},
    )
    credentials = CredentialsManager(tmp_path / "credentials")
    if saved is not None:
        credentials.save_credentials("tavily", saved)
    return get_tool_by_name(
        "tavily",
        runtime_paths,
        credentials_manager=credentials,
        tool_config_overrides=inline,
        runs_in_worker=False,
        worker_target=None,
    )


def test_a_bare_run_reads_a_tool_setting_set_only_in_the_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key and endpoint only in the `.env` beside `config.yaml` configure the toolkit."""
    tool = _tavily(
        tmp_path,
        monkeypatch,
        env_file="TAVILY_API_KEY=env-file-key\nTAVILY_API_BASE_URL=https://env-file.example\n",
    )

    assert (tool.api_key, tool.api_base_url) == ("env-file-key", "https://env-file.example")


@pytest.mark.parametrize(
    ("process_env", "env_file", "saved", "expected"),
    [
        pytest.param({"TAVILY_API_KEY": "process-key"}, "TAVILY_API_KEY=file-key\n", None, "process-key", id="process"),
        pytest.param(
            {"TAVILY_API_KEY": "process-key"},
            "TAVILY_API_KEY=file-key\n",
            {"api_key": "saved-key"},
            "saved-key",
            id="saved",
        ),
        pytest.param({}, "TAVILY_API_KEY=file-key\n", {"api_key": "  "}, "file-key", id="blank-saved"),
        pytest.param({"TAVILY_API_KEY": " "}, "TAVILY_API_KEY=file-key\n", None, "file-key", id="blank-process"),
    ],
)
def test_saved_settings_then_the_process_environment_win_over_the_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process_env: dict[str, str],
    env_file: str,
    saved: dict[str, Any] | None,
    expected: str,
) -> None:
    """A saved value wins, then the process environment, then the `.env`; a blank value counts as unset."""
    tool = _tavily(tmp_path, monkeypatch, process_env=process_env, env_file=env_file, saved=saved)

    assert tool.api_key == expected


def test_an_inline_setting_wins_over_saved_settings_and_the_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An authored `tools:` setting beats every lower layer."""
    tool = _tavily(
        tmp_path,
        monkeypatch,
        process_env={"TAVILY_API_BASE_URL": "https://process.example"},
        env_file="TAVILY_API_BASE_URL=https://env-file.example\n",
        saved={"api_key": "saved-key", "api_base_url": "https://saved.example"},
        inline={"api_base_url": "https://inline.example"},
    )

    assert tool.api_base_url == "https://inline.example"


def test_every_variable_in_the_process_environment_wins_over_the_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A field with two variables reads both from the process environment before either from the `.env`."""
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    (tmp_path / "config.yaml").write_text("agents: {}\n")
    (tmp_path / ".env").write_text("AWS_REGION=file-region\n")
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env={"MINDROOM_NO_AUTO_INSTALL_TOOLS": "1", "AWS_DEFAULT_REGION": "process-region"},
    )

    tool = get_tool_by_name(
        "redshift",
        runtime_paths,
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        runs_in_worker=False,
        worker_target=None,
    )

    assert tool.region == "process-region"


def test_a_number_setting_from_the_env_file_reaches_the_toolkit_as_a_number(tmp_path: Path) -> None:
    """A variable is converted to its field's type, as a saved value is."""
    tool_name = "test_env_number_tool"

    class _NumberToolkit(Toolkit):
        def __init__(self, limit: int | None = None) -> None:
            self.limit = limit
            super().__init__(name=tool_name)

    snapshot = capture_tool_registry_snapshot()

    @register_tool_with_metadata(
        name=tool_name,
        file_access=ToolFileAccess.NONE,
        display_name="Env Number Tool",
        description="Test-only coverage of a number setting from the environment.",
        category=ToolCategory.DEVELOPMENT,
        config_fields=[ConfigField(name="limit", label="Limit", type="number", required=False, env=("PROBE_LIMIT",))],
    )
    def _number_tool_factory() -> type[_NumberToolkit]:
        return _NumberToolkit

    (tmp_path / "config.yaml").write_text("agents: {}\n")
    (tmp_path / ".env").write_text("PROBE_LIMIT=42\n")
    runtime_paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})
    try:
        tool = get_tool_by_name(
            tool_name,
            runtime_paths,
            credentials_manager=CredentialsManager(tmp_path / "credentials"),
            runs_in_worker=False,
            worker_target=None,
        )
    finally:
        restore_tool_registry_snapshot(snapshot)

    assert tool.limit == 42
    assert isinstance(tool.limit, int)
