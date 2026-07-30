"""MindRoom compatibility adapter for Claude through Bedrock Mantle."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from agno.models.aws.claude import Claude as AwsBedrockClaude
from anthropic.lib.bedrock import AnthropicBedrockMantle, AsyncAnthropicBedrockMantle

from mindroom.claude_compat import ClaudeProviderCompat
from mindroom.logging_config import get_logger

logger = get_logger(__name__)


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
        """Return an asynchronous Mantle client with current AWS credentials."""
        if not self.session and self.async_client is not None and not self.async_client.is_closed():
            return self.async_client

        client_params = self._get_client_params()
        if self.http_client is not None:
            if isinstance(self.http_client, httpx.AsyncClient):
                client_params["http_client"] = self.http_client
            else:
                logger.warning("bedrock_claude_async_http_client_ignored")

        client = AsyncAnthropicBedrockMantle(**client_params)
        if not self.session:
            self.async_client = client
        return client
