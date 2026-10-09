"""Shared fixtures for egress broker proxy tests: fake upstreams, a broker factory, and clients."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio
from aiohttp import WSMsgType, web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from mindroom.config.egress_broker import EgressBrokerConfig
from mindroom.egress_broker import AuditLog, BrokerCA, DialPolicy, TokenSigner, WorkerClaims
from mindroom.egress_broker.proxy import EgressBroker, ManageUrl

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
    from pathlib import Path

DEFAULT_CLAIMS = WorkerClaims(
    worker_key="worker-alice-code",
    worker_scope="user_agent",
    routing_agent_name="code",
    tenant_id=None,
    account_id=None,
    channel="matrix",
    agent_name="code",
    requester_id="@alice:example.org",
)


@dataclass(frozen=True)
class UpstreamCA:
    """Test CA that signs the fake upstream's certificate, standing in for public roots."""

    cert: x509.Certificate
    key: ec.EllipticCurvePrivateKey

    @property
    def pem(self) -> str:
        """Return the CA certificate in PEM form."""
        return self.cert.public_bytes(serialization.Encoding.PEM).decode()

    def client_context(self) -> ssl.SSLContext:
        """Return a client context that trusts only this CA."""
        return ssl.create_default_context(cadata=self.pem)

    def server_context(self, directory: Path) -> ssl.SSLContext:
        """Issue a certificate for ``localhost`` and 127.0.0.1 and return a server context serving it."""
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
            .issuer_name(self.cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(hours=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName(
                    [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))],
                ),
                critical=False,
            )
            # Python 3.13 default contexts verify strictly and require the key identifiers.
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), critical=False)
            .sign(self.key, hashes.SHA256())
        )
        cert_path = directory / "upstream.pem"
        key_path = directory / "upstream.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(cert_path, key_path)
        return context


@pytest.fixture
def upstream_ca() -> UpstreamCA:
    """Return a fresh test CA for the fake TLS upstream."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Egress Broker Test Upstream CA")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return UpstreamCA(cert=cert, key=key)


# Fake upstream routes. Add a handler here to make it available on both `tls_upstream` and `http_upstream`.


async def _echo(request: web.Request) -> web.Response:
    headers: dict[str, list[str]] = {}
    for name, value in request.headers.items():
        headers.setdefault(name.lower(), []).append(value)
    return web.json_response(
        {"method": request.method, "path": request.path, "query": dict(request.query), "headers": headers},
    )


async def _redirect(_request: web.Request) -> web.Response:
    return web.Response(status=302, headers={"Location": "https://other.invalid/"})


async def _stream(request: web.Request) -> web.StreamResponse:
    response = web.StreamResponse()
    await response.prepare(request)
    for index in range(3):
        if index:
            await asyncio.sleep(0.2)
        await response.write(f"chunk-{index}\n".encode())
    await response.write_eof()
    return response


async def _cookie(_request: web.Request) -> web.Response:
    return web.Response(text="ok", headers={"Set-Cookie": "session=upstream-session; Path=/"})


async def _websocket(request: web.Request) -> web.WebSocketResponse:
    socket = web.WebSocketResponse()
    await socket.prepare(request)
    async for message in socket:
        if message.type == WSMsgType.TEXT:
            await socket.send_str(message.data)
        elif message.type == WSMsgType.BINARY:
            await socket.send_bytes(message.data)
    return socket


async def _upload(request: web.Request) -> web.Response:
    length = 0
    async for chunk in request.content.iter_any():
        length += len(chunk)
    return web.json_response({"length": length})


UPSTREAM_ROUTES: dict[str, Callable[[web.Request], Awaitable[web.StreamResponse]]] = {
    "/echo": _echo,
    "/redirect": _redirect,
    "/stream": _stream,
    "/cookie": _cookie,
    "/ws": _websocket,
    "/upload": _upload,
}


@dataclass
class Upstream:
    """A running fake upstream; ``hits`` lists every request path it served, in order."""

    scheme: str
    host: str
    port: int
    hits: list[str]

    def url(self, path: str) -> str:
        """Return the absolute URL of `path` on this upstream."""
        return f"{self.scheme}://{self.host}:{self.port}{path}"


async def _serve_upstream(ssl_context: ssl.SSLContext | None) -> AsyncIterator[Upstream]:
    hits: list[str] = []

    @web.middleware
    async def record_hit(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        hits.append(request.path)
        return await handler(request)

    app = web.Application(middlewares=[record_hit])
    for path, route in UPSTREAM_ROUTES.items():
        app.router.add_route("*", path, route)
    runner = web.AppRunner(app, shutdown_timeout=1.0)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ssl_context)
        await site.start()
        port = runner.addresses[0][1]
        yield Upstream(scheme="https" if ssl_context else "http", host="localhost", port=port, hits=hits)
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def tls_upstream(upstream_ca: UpstreamCA, tmp_path: Path) -> AsyncIterator[Upstream]:
    """Serve the fake upstream over TLS on ``localhost`` with a certificate from `upstream_ca`."""
    async for upstream in _serve_upstream(upstream_ca.server_context(tmp_path)):
        yield upstream


@pytest_asyncio.fixture
async def http_upstream() -> AsyncIterator[Upstream]:
    """Serve the fake upstream over plain HTTP on ``localhost``."""
    async for upstream in _serve_upstream(None):
        yield upstream


@pytest.fixture
def audit(tmp_path: Path) -> Iterator[AuditLog]:
    """Return the audit log every broker built by the `broker` factory writes to."""
    log = AuditLog(tmp_path / "egress_broker" / "requests.sqlite3")
    yield log
    log.close()


@dataclass
class BrokerFactory:
    """Build and start brokers that share one CA, signer, and audit log; tracks them for teardown."""

    ca: BrokerCA
    signer: TokenSigner
    audit: AuditLog
    upstream_ssl_context: ssl.SSLContext
    brokers: list[EgressBroker] = field(default_factory=list)

    async def __call__(
        self,
        config: EgressBrokerConfig | None = None,
        *,
        secrets: dict[str, str] | None = None,
        dial_policy: DialPolicy | None = None,
        manage_url: ManageUrl | None = None,
        max_body_bytes: int = 1 << 30,
    ) -> EgressBroker:
        """Start a broker on an ephemeral loopback port; `secrets` maps service names to secrets."""
        current = config or EgressBrokerConfig()
        stored = dict(secrets or {})
        broker = EgressBroker(
            ca=self.ca,
            signer=self.signer,
            config_provider=lambda: current,
            resolve_secret=lambda _claims, service: stored.get(service),
            audit=self.audit,
            dial_policy=dial_policy or DialPolicy(allow_loopback=True),
            upstream_ssl_context=self.upstream_ssl_context,
            manage_url=manage_url or (lambda _claims: None),
            max_body_bytes=max_body_bytes,
        )
        await broker.start("127.0.0.1", 0)
        self.brokers.append(broker)
        return broker

    @property
    def latest(self) -> EgressBroker:
        """Return the most recently started broker."""
        return self.brokers[-1]

    def token(self, claims: WorkerClaims = DEFAULT_CLAIMS) -> str:
        """Mint a valid proxy token for these claims."""
        return self.signer.mint(claims)


@pytest_asyncio.fixture
async def broker(upstream_ca: UpstreamCA, audit: AuditLog, tmp_path: Path) -> AsyncIterator[BrokerFactory]:
    """Return a factory for started brokers; loopback dialing is allowed so they reach the fake upstreams."""
    factory = BrokerFactory(
        ca=BrokerCA.load_or_create(tmp_path / "egress_broker", key_password=None),
        signer=TokenSigner(b"k" * 32),
        audit=audit,
        upstream_ssl_context=upstream_ca.client_context(),
    )
    yield factory
    for started in factory.brokers:
        await started.close()


@pytest_asyncio.fixture
async def broker_default_policy(broker: BrokerFactory) -> EgressBroker:
    """Return a started broker with the default dial policy, which blocks loopback and private networks."""
    return await broker(dial_policy=DialPolicy())


@pytest_asyncio.fixture
async def proxy_client(broker: BrokerFactory) -> AsyncIterator[Callable[..., httpx.AsyncClient]]:
    """Return ``make(token, *, verify=None)``: an httpx client using the latest broker as its proxy.

    By default the client trusts only the broker CA, as a worker's bundle would for intercepted hosts.
    """
    clients: list[httpx.AsyncClient] = []

    def make(token: str, *, verify: ssl.SSLContext | None = None) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            proxy=f"http://{token}:@127.0.0.1:{broker.latest.port}",
            verify=verify or ssl.create_default_context(cadata=broker.ca.cert_pem),
            trust_env=False,
        )
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.aclose()


@dataclass(frozen=True)
class RawResponse:
    """A response head (and Content-Length body) read straight off a proxy socket."""

    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> object:
        """Decode the body as JSON."""
        return json.loads(self.body)


async def _raw_proxy_request(port: int, request: bytes) -> RawResponse:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request)
        await writer.drain()
        async with asyncio.timeout(10):
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").removesuffix("\r\n\r\n").split("\r\n")
            headers = {
                name.strip().lower(): value.strip() for name, _, value in (line.partition(":") for line in lines[1:])
            }
            body = await reader.readexactly(int(headers.get("content-length", "0")))
    finally:
        writer.transport.abort()
    return RawResponse(status=int(lines[0].split(" ")[1]), headers=headers, body=body)


@pytest.fixture
def raw_proxy() -> Callable[[int, bytes], Awaitable[RawResponse]]:
    """Return ``send(port, request_bytes)`` for proxy requests that httpx cannot express or inspect."""
    return _raw_proxy_request
