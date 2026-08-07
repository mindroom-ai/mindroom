"""Real-browser coverage for sandboxed protected-report asset authentication."""

from __future__ import annotations

import datetime
import ipaddress
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from playwright.sync_api import Browser, Error, Playwright, sync_playwright

from mindroom.api.report_headers import _STATIC_SITE_CSP

if TYPE_CHECKING:
    from pathlib import Path

_SESSION_COOKIE = "report_session=authorized"
_ASSET_PATHS = frozenset({"/app.js", "/style.css", "/pixel.svg"})


class _ProtectedReportHandler(BaseHTTPRequestHandler):
    """Serve one cookie-protected static report with MindRoom's sandbox CSP."""

    requests: list[tuple[str, bool]]

    def do_GET(self) -> None:
        """Serve the report root or one nested asset."""
        authorized = _SESSION_COOKIE in (self.headers.get("Cookie") or "")
        self.requests.append((self.path, authorized))
        if not authorized:
            self.send_error(401)
            return
        if self.path == "/":
            self._send(
                (
                    b"<!doctype html>"
                    b"<link rel='stylesheet' href='style.css'>"
                    b"<script src='app.js'></script>"
                    b"<img id='protected-image' src='pixel.svg'>"
                ),
                "text/html",
                content_security_policy=_STATIC_SITE_CSP,
            )
            return
        if self.path == "/app.js":
            self._send(b"window.protectedScriptLoaded = true;", "text/javascript")
            return
        if self.path == "/style.css":
            self._send(b"body { color: rgb(1, 2, 3); }", "text/css")
            return
        if self.path == "/pixel.svg":
            self._send(
                b"<svg xmlns='http://www.w3.org/2000/svg' width='1' height='1'/>",
                "image/svg+xml",
            )
            return
        self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Keep pytest output quiet."""

    def _send(
        self,
        body: bytes,
        content_type: str,
        *,
        content_security_policy: str | None = None,
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if content_security_policy is not None:
            self.send_header("Content-Security-Policy", content_security_policy)
        self.end_headers()
        self.wfile.write(body)


def _write_localhost_certificate(directory: Path) -> tuple[Path, Path]:
    """Write a short-lived certificate for the local HTTPS test server."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(minutes=10))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ],
            ),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    certificate_path = directory / "certificate.pem"
    private_key_path = directory / "private-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private_key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    return certificate_path, private_key_path


def _launch_browser(playwright: Playwright) -> Browser:
    """Launch Google Chrome when available, then bundled Chromium."""
    try:
        return playwright.chromium.launch(channel="chrome", headless=True)
    except Error:
        try:
            return playwright.chromium.launch(headless=True)
        except Error as error:
            pytest.skip(f"Chrome or Chromium is unavailable: {error}")


@pytest.mark.e2e
def test_sandboxed_report_assets_require_cross_site_cookie(tmp_path: Path) -> None:
    """Opaque sandbox origins should retain only a cross-site authorization cookie."""
    certificate_path, private_key_path = _write_localhost_certificate(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProtectedReportHandler)
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.load_cert_chain(certificate_path, private_key_path)
    server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    base_url = f"https://localhost:{server.server_port}"
    try:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            try:
                for same_site, assets_authorized in (("Lax", False), ("None", True)):
                    _ProtectedReportHandler.requests = []
                    context = browser.new_context(ignore_https_errors=True)
                    context.add_cookies(
                        [
                            {
                                "name": "report_session",
                                "value": "authorized",
                                "url": base_url,
                                "secure": True,
                                "sameSite": same_site,
                            },
                        ],
                    )
                    page = context.new_page()
                    response = page.goto(base_url, wait_until="networkidle")

                    assert response is not None
                    assert response.status == 200
                    asset_requests = [
                        authorized for path, authorized in _ProtectedReportHandler.requests if path in _ASSET_PATHS
                    ]
                    assert len(asset_requests) == len(_ASSET_PATHS)
                    assert asset_requests == [assets_authorized] * len(_ASSET_PATHS)
                    context.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
