"""Worker brokered egress configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


@dataclass(frozen=True)
class ResolvedWorkerEgressBroker:
    """Resolved worker egress broker env for one agent."""

    name: str
    execution_env: dict[str, str]
    config: WorkerEgressBrokerConfig

    def execution_env_for_worker_target(
        self,
        worker_target: ResolvedWorkerTarget | None,
    ) -> dict[str, str]:
        """Return broker env for one resolved worker target."""
        return self.config.execution_env_for_worker_target(worker_target)


class WorkerEgressBrokerConfig(BaseModel):
    """HTTP proxy and CA settings injected into worker execution tools."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["static", "worker_scoped_proxy"] = Field(
        description="Broker resolver kind: static proxy URL or worker-scoped service proxy",
    )
    proxy_url: str | None = Field(
        default=None,
        description="HTTP proxy URL reachable from worker containers or pods",
    )
    service_name_prefix: str = Field(
        default="agent-vault-bridge",
        description="Service-name prefix used for worker-scoped proxy services",
    )
    port: int = Field(
        default=18080,
        ge=1,
        le=65535,
        description="Worker-scoped proxy service port",
    )
    ca_bundle: str | None = Field(
        default=None,
        description="Optional CA bundle path inside the worker runtime for TLS interception",
    )
    no_proxy: str | None = Field(
        default=None,
        description="Optional NO_PROXY value for local or cluster-local addresses",
    )

    @field_validator("proxy_url", "service_name_prefix", "ca_bundle", "no_proxy")
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
    def validate_proxy_url_scheme(cls, value: str | None) -> str | None:
        """Reject proxy URLs that common HTTP clients cannot parse."""
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            msg = "proxy_url must include an http:// or https:// scheme"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def validate_kind_fields(self) -> WorkerEgressBrokerConfig:
        """Ensure required fields exist for the selected broker kind."""
        if self.kind == "static" and self.proxy_url is None:
            msg = "proxy_url is required for static worker egress brokers"
            raise ValueError(msg)
        return self

    def execution_env(self) -> dict[str, str]:
        """Return process env overlay for worker execution tools."""
        if self.kind == "worker_scoped_proxy":
            msg = "Worker-scoped proxy broker requires a resolved worker key"
            raise ValueError(msg)
        return self._proxy_execution_env(self._required_proxy_url())

    def execution_env_for_worker_target(
        self,
        worker_target: ResolvedWorkerTarget | None,
    ) -> dict[str, str]:
        """Return process env overlay for one worker target."""
        if self.kind == "static":
            return self.execution_env()

        worker_key = worker_target.worker_key if worker_target is not None else None
        if not worker_key:
            msg = "Worker-scoped proxy broker requires a resolved worker key"
            raise ValueError(msg)

        from mindroom.workers.backends.kubernetes_resources import worker_id_for_key  # noqa: PLC0415

        service_name = worker_id_for_key(worker_key, prefix=self.service_name_prefix)
        proxy_url = f"http://{service_name}:{self.port}"
        return self._proxy_execution_env(proxy_url)

    def _required_proxy_url(self) -> str:
        if self.proxy_url is None:
            msg = "proxy_url is required for static worker egress brokers"
            raise ValueError(msg)
        return self.proxy_url

    def _proxy_execution_env(self, proxy_url: str) -> dict[str, str]:
        env = {
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "http_proxy": proxy_url,
            "https_proxy": proxy_url,
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
