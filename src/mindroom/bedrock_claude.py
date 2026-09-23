"""MindRoom compatibility adapter for Claude through Bedrock Mantle."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from agno.models.aws.claude import Claude as AwsBedrockClaude
from anthropic.lib.bedrock import AnthropicBedrockMantle, AsyncAnthropicBedrockMantle

from mindroom.claude_compat import ClaudeProviderCompat
from mindroom.logging_config import get_logger

logger = get_logger(__name__)


# AGNO_COMPAT: Bedrock Claude hard-codes pre-Mantle SDK client construction.
# Reason: Agno constructs AnthropicBedrock clients and exposes client parameters
# only through a private helper. Selecting Mantle requires local client factories.
# Upstream issue: No matching Bedrock Mantle client support issue identified.
# Upstream PR: None identified; Mantle support or injectable factories remain untracked.
# Remove when: Agno supports Mantle clients or public typed client factories;
# retain AWS credential resolution and the owner's async model-lifetime client.
# Coverage: tests/test_model_loading.py::test_bedrock_current_claude_uses_mantle_endpoint;
# tests/test_extra_kwargs.py::test_session_backed_bedrock_async_client_is_retained.
@dataclass
class MindRoomBedrockClaude(ClaudeProviderCompat, AwsBedrockClaude):
    """Bedrock Claude model using the current Mantle Messages endpoint."""

    client: AnthropicBedrockMantle | None = None
    async_client: AsyncAnthropicBedrockMantle | None = None

    def get_client(self) -> AnthropicBedrockMantle:  # ty: ignore[invalid-method-override]  # Agno types only legacy clients
        """Return a synchronous Mantle client with current AWS credentials."""
        if not self.session and self.client is not None and not self.client.is_closed():
            return self.client

        client_params = self._get_client_params()
        if self.http_client is not None:
            if isinstance(self.http_client, httpx.Client):
                client_params["http_client"] = self.http_client
            else:
                logger.warning("bedrock_claude_sync_http_client_ignored")

        if self.session and self.client is not None and not self.client.is_closed():
            self.client.close()

        client = AnthropicBedrockMantle(**client_params)
        if not self.session:
            self.client = client
        return client

    def get_async_client(self) -> AsyncAnthropicBedrockMantle:  # ty: ignore[invalid-method-override]  # Agno types only legacy clients
        """Return this model lifetime's asynchronous Mantle client."""
        if self.async_client is not None and not self.async_client.is_closed():
            return self.async_client

        client_params = self._get_client_params()
        if self.http_client is not None:
            if isinstance(self.http_client, httpx.AsyncClient):
                client_params["http_client"] = self.http_client
            else:
                logger.warning("bedrock_claude_async_http_client_ignored")

        client = AsyncAnthropicBedrockMantle(**client_params)
        self.async_client = client
        return client
