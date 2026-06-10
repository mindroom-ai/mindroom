"""Worker brokered egress configuration."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WorkerEgressBrokerConfig(BaseModel):
    """HTTP proxy and CA settings injected into worker execution tools.

    This is the backend-neutral ``static`` broker: one configured proxy URL is
    injected into python/shell execution env. Per-worker Agent Vault isolation
    on the Kubernetes backend does not use this broker; that backend mints a
    per-worker token into the worker pod and the runner composes the proxy URL
    itself (see ``mindroom.constants.worker_proxy_execution_env``).
    """

    model_config = ConfigDict(extra="forbid")

    proxy_url: str = Field(
        description="HTTP proxy URL reachable from worker containers or pods",
    )
    ca_bundle: str | None = Field(
        default=None,
        description="Optional CA bundle path inside the worker runtime for TLS interception",
    )
    no_proxy: str | None = Field(
        default=None,
        description="Optional NO_PROXY value for local or cluster-local addresses",
    )

    @field_validator("proxy_url", "ca_bundle", "no_proxy")
    @classmethod
    def validate_non_empty_string(cls, value: str | None) -> str | None:
        """Reject empty strings for authored env values."""
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            msg = "Worker egress broker values must not be empty"
            raise ValueError(msg)
        return stripped

    @field_validator("proxy_url")
    @classmethod
    def validate_proxy_url_scheme(cls, value: str) -> str:
        """Reject proxy URLs that common HTTP clients cannot parse."""
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            msg = "proxy_url must include an http:// or https:// scheme"
            raise ValueError(msg)
        return value

    def execution_env(self) -> dict[str, str]:
        """Return the process env overlay injected into python/shell execution."""
        env = {
            "HTTP_PROXY": self.proxy_url,
            "HTTPS_PROXY": self.proxy_url,
            "http_proxy": self.proxy_url,
            "https_proxy": self.proxy_url,
        }
        if self.ca_bundle:
            env.update(
                {
                    "REQUESTS_CA_BUNDLE": self.ca_bundle,
                    "CURL_CA_BUNDLE": self.ca_bundle,
                    "SSL_CERT_FILE": self.ca_bundle,
                },
            )
        if self.no_proxy:
            env.update({"NO_PROXY": self.no_proxy, "no_proxy": self.no_proxy})
        return env
