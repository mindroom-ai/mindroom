"""Credential-injecting egress broker."""

from typing import TYPE_CHECKING, Any

# Stub exports for static analysis (actual values loaded lazily via __getattr__)
AuditLog: Any
AuditRecord: Any
BrokerCA: Any
materialize_ca_bundle: Any
DestinationBlockedError: Any
DestinationUnresolvableError: Any
DialPolicy: Any
open_upstream: Any
EgressBroker: Any
ManageUrl: Any
SecretResolver: Any
TokenSigner: Any
WorkerClaims: Any

if TYPE_CHECKING:
    from mindroom.egress_broker.audit import AuditLog, AuditRecord
    from mindroom.egress_broker.ca import BrokerCA, materialize_ca_bundle
    from mindroom.egress_broker.dial import (
        DestinationBlockedError,
        DestinationUnresolvableError,
        DialPolicy,
        open_upstream,
    )
    from mindroom.egress_broker.proxy import EgressBroker, ManageUrl, SecretResolver
    from mindroom.egress_broker.tokens import TokenSigner, WorkerClaims


# Lazy imports to keep the runner import light when only env is needed
def __getattr__(name: str) -> object:  # noqa: C901, PLR0911, PLR0912
    if name == "AuditLog":
        from mindroom.egress_broker.audit import AuditLog  # noqa: PLC0415

        return AuditLog
    if name == "AuditRecord":
        from mindroom.egress_broker.audit import AuditRecord  # noqa: PLC0415

        return AuditRecord
    if name == "BrokerCA":
        from mindroom.egress_broker.ca import BrokerCA  # noqa: PLC0415

        return BrokerCA
    if name == "materialize_ca_bundle":
        from mindroom.egress_broker.ca import materialize_ca_bundle  # noqa: PLC0415

        return materialize_ca_bundle
    if name == "DestinationBlockedError":
        from mindroom.egress_broker.dial import DestinationBlockedError  # noqa: PLC0415

        return DestinationBlockedError
    if name == "DestinationUnresolvableError":
        from mindroom.egress_broker.dial import DestinationUnresolvableError  # noqa: PLC0415

        return DestinationUnresolvableError
    if name == "DialPolicy":
        from mindroom.egress_broker.dial import DialPolicy  # noqa: PLC0415

        return DialPolicy
    if name == "open_upstream":
        from mindroom.egress_broker.dial import open_upstream  # noqa: PLC0415

        return open_upstream
    if name == "EgressBroker":
        from mindroom.egress_broker.proxy import EgressBroker  # noqa: PLC0415

        return EgressBroker
    if name == "ManageUrl":
        from mindroom.egress_broker.proxy import ManageUrl  # noqa: PLC0415

        return ManageUrl
    if name == "SecretResolver":
        from mindroom.egress_broker.proxy import SecretResolver  # noqa: PLC0415

        return SecretResolver
    if name == "TokenSigner":
        from mindroom.egress_broker.tokens import TokenSigner  # noqa: PLC0415

        return TokenSigner
    if name == "WorkerClaims":
        from mindroom.egress_broker.tokens import WorkerClaims  # noqa: PLC0415

        return WorkerClaims
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "AuditLog",
    "AuditRecord",
    "BrokerCA",
    "DestinationBlockedError",
    "DestinationUnresolvableError",
    "DialPolicy",
    "EgressBroker",
    "ManageUrl",
    "SecretResolver",
    "TokenSigner",
    "WorkerClaims",
    "materialize_ca_bundle",
    "open_upstream",
]
