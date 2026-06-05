"""Worker brokered egress configuration."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator


@dataclass(frozen=True)
class ResolvedWorkerEgressBroker:
    """Resolved worker egress broker env for one agent."""

    name: str
    execution_env: dict[str, str]


class WorkerEgressBrokerConfig(BaseModel):
    """HTTP proxy and CA settings injected into worker execution tools."""

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

    def execution_env(self) -> dict[str, str]:
        """Return process env overlay for worker execution tools."""
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
