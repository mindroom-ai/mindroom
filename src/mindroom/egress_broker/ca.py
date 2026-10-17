"""Local certificate authority for broker TLS interception."""

from __future__ import annotations

import ipaddress
import os
import ssl
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from mindroom.atomic_file import atomic_write_bytes_at

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "BrokerCA",
    "materialize_ca_bundle",
]

_CACHE_SIZE = 1024
_LEAF_VALIDITY_HOURS = 24
_CA_VALIDITY_YEARS = 10
_CACHE_REFRESH_MARGIN_SECONDS = 3600  # Replace cached context when <1h validity remains
_CA_CERT_NAME = "ca.pem"
_CA_KEY_NAME = "ca.key"


class BrokerCA:
    """Certificate authority for broker leaf certificates."""

    def __init__(
        self,
        *,
        ca_cert: x509.Certificate,
        ca_key: ec.EllipticCurvePrivateKey,
        cert_pem: str,
        directory: Path,
    ) -> None:
        """Initialize CA with certificate and key.

        Use load_or_create instead of calling this directly.
        """
        self._ca_cert = ca_cert
        self._ca_key = ca_key
        self._cert_pem = cert_pem

        # Leaf key, shared across all contexts in this process; each leaf's chain file ends with the CA and this key.
        self._leaf_key = ec.generate_private_key(ec.SECP256R1())
        self._chain_tail = self._ca_cert.public_bytes(serialization.Encoding.PEM) + self._leaf_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        # Cache: host -> (context, valid_until_timestamp)
        self._cache: OrderedDict[str, tuple[ssl.SSLContext, float]] = OrderedDict()
        self._cache_lock = threading.Lock()

        # Ensure leaf temp directory exists with mode 0700
        self._leaf_tmp_dir = directory / "leaf-tmp"
        self._leaf_tmp_dir.mkdir(mode=0o700, exist_ok=True)

    @property
    def cert_pem(self) -> str:
        """Return CA certificate in PEM format."""
        return self._cert_pem

    @staticmethod
    def load_or_create(
        directory: Path,
        *,
        key_password: bytes | None,
    ) -> BrokerCA:
        """Load existing CA from directory or create new one.

        CA files: ca.pem (0644) and ca.key (0600, PKCS8, encrypted when key_password is given).
        Parent directory is created with mode 0700 if it does not exist.
        """
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)

        cert_path = directory / _CA_CERT_NAME
        key_path = directory / _CA_KEY_NAME

        if cert_path.exists() and key_path.exists():
            cert_pem = cert_path.read_bytes()
            return BrokerCA(
                ca_cert=x509.load_pem_x509_certificate(cert_pem),
                ca_key=_load_ca_key(key_path, key_password),
                cert_pem=cert_pem.decode("utf-8"),
                directory=directory,
            )

        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_cert = _issue_ca_certificate(ca_key)
        cert_pem_bytes = _write_ca_files(directory, ca_cert=ca_cert, ca_key=ca_key, key_password=key_password)
        return BrokerCA(
            ca_cert=ca_cert,
            ca_key=ca_key,
            cert_pem=cert_pem_bytes.decode("utf-8"),
            directory=directory,
        )

    def server_context(self, host: str) -> ssl.SSLContext:
        """Return server TLS context with leaf certificate for the given host.

        The context is cached; cached contexts are replaced when less than 1h validity remains.
        host should be the target as it appears in a CONNECT line (IPv6 without brackets).
        """
        now = time.time()

        with self._cache_lock:
            # Check cache
            if host in self._cache:
                ctx, valid_until = self._cache[host]
                # Replace if less than 1h validity remains
                if now + _CACHE_REFRESH_MARGIN_SECONDS < valid_until:
                    # Move to end (LRU)
                    self._cache.move_to_end(host)
                    return ctx

            # Generate new leaf certificate
            leaf_cert, valid_until_ts = self._generate_leaf_cert(host)

            # Create server context
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.set_alpn_protocols(["http/1.1"])
            ctx.sni_callback = _sni_guard(host)

            # Load certificate chain into context via temp file
            self._load_cert_chain_to_context(ctx, leaf_cert)

            # Update cache
            self._cache[host] = (ctx, valid_until_ts)
            self._cache.move_to_end(host)

            # Evict oldest if over limit
            if len(self._cache) > _CACHE_SIZE:
                self._cache.popitem(last=False)

            return ctx

    def _generate_leaf_cert(self, host: str) -> tuple[x509.Certificate, float]:
        """Generate a leaf certificate for the given host.

        Returns (certificate, valid_until_timestamp).
        """
        # Determine if host is an IP address or DNS name
        try:
            ip = ipaddress.ip_address(host)
            san = x509.SubjectAlternativeName([x509.IPAddress(ip)])
        except ValueError:
            # Not an IP, treat as DNS name
            san = x509.SubjectAlternativeName([x509.DNSName(host)])

        subject = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, host),
            ],
        )

        now = datetime.now(UTC)
        not_before = now - timedelta(hours=1)  # 1h in the past
        not_after = now + timedelta(hours=_LEAF_VALIDITY_HOURS)

        leaf_cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self._ca_cert.subject)
            .public_key(self._leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(self._leaf_key.public_key()),
                critical=False,
            )
            .add_extension(
                # The same key id as the CA's SubjectKeyIdentifier, which `_issue_ca_certificate` derives from its key.
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._ca_key.public_key()),
                critical=False,
            )
            .add_extension(san, critical=False)
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=True,
            )
            .sign(self._ca_key, hashes.SHA256())
        )

        return leaf_cert, not_after.timestamp()

    def _load_cert_chain_to_context(
        self,
        ctx: ssl.SSLContext,
        leaf_cert: x509.Certificate,
    ) -> None:
        """Load the leaf, the CA, and the leaf key into `ctx` through a private temp file, since ssl reads only files.

        The file is created exclusively with mode 0600 in the 0700 leaf directory and removed once loaded.
        """
        with tempfile.NamedTemporaryFile(dir=self._leaf_tmp_dir, suffix=".pem") as tmp:
            tmp.write(leaf_cert.public_bytes(serialization.Encoding.PEM) + self._chain_tail)
            tmp.flush()
            ctx.load_cert_chain(tmp.name)


def _load_ca_key(key_path: Path, key_password: bytes | None) -> ec.EllipticCurvePrivateKey:
    """Read the CA's private key; raise TypeError when it is not an ECDSA key."""
    ca_key = serialization.load_pem_private_key(key_path.read_bytes(), password=key_password)
    if not isinstance(ca_key, ec.EllipticCurvePrivateKey):
        msg = "CA key is not an ECDSA key"
        raise TypeError(msg)
    return ca_key


def _issue_ca_certificate(ca_key: ec.EllipticCurvePrivateKey) -> x509.Certificate:
    """Return a new self-signed CA certificate for `ca_key`, valid for `_CA_VALIDITY_YEARS`."""
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "MindRoom Egress Broker CA"),
        ],
    )

    now = datetime.now(UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=_CA_VALIDITY_YEARS * 365))
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0),
            critical=True,
        )
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
        .sign(ca_key, hashes.SHA256())
    )


def _write_ca_files(
    directory: Path,
    *,
    ca_cert: x509.Certificate,
    ca_key: ec.EllipticCurvePrivateKey,
    key_password: bytes | None,
) -> bytes:
    """Atomically publish the certificate with mode 0644, then the PKCS8 key with mode 0600; return the certificate PEM.

    Both files live in `directory`, which is opened once, as for the token key.
    """
    cert_pem_bytes = ca_cert.public_bytes(serialization.Encoding.PEM)
    encryption = serialization.BestAvailableEncryption(key_password) if key_password else serialization.NoEncryption()
    key_pem_bytes = ca_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=encryption,
    )
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name, content, mode in ((_CA_CERT_NAME, cert_pem_bytes, 0o644), (_CA_KEY_NAME, key_pem_bytes, 0o600)):
            atomic_write_bytes_at(directory_fd, name, content, file_mode=mode, temp_prefix=f".{name}.")
    finally:
        os.close(directory_fd)
    return cert_pem_bytes


def _sni_guard(host: str) -> Callable[[ssl.SSLObject, str | None, ssl.SSLContext], int | None]:
    """Return an SNI callback that aborts the handshake when the client names a host other than `host`.

    The leaf is for the CONNECT host, so a client claiming another name gets no tunnel at all. A client
    without SNI, as for IP literals, is served. Names compare without case or a trailing dot.
    """
    expected = host.lower().removesuffix(".")

    def check(_connection: ssl.SSLObject, server_name: str | None, _context: ssl.SSLContext) -> int | None:
        if server_name is None or server_name.lower().removesuffix(".") == expected:
            return None
        return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME

    return check


def _publish_unless_current(directory_fd: int, filename: str, content: bytes) -> None:
    """Atomically write `content` unless `filename` is already a file, not a symlink, holding exactly it.

    The bundle directory can sit in a shared /tmp, so an existing file is
    never trusted by name: a symlink or a file with other content is replaced.
    """
    try:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except OSError:
        existing = None
    else:
        with os.fdopen(fd, "rb") as existing_file:
            existing = existing_file.read()
    if existing != content:
        atomic_write_bytes_at(directory_fd, filename, content, file_mode=0o644, temp_prefix=f".{filename}.")


def materialize_ca_bundle(ca_pem: str, directory: Path) -> tuple[Path, Path]:
    """Materialize CA bundle files in the given directory, which is created with mode 0700 if missing.

    Returns (combined_bundle, broker_ca_only) where:
    - combined_bundle: system roots followed by broker CA
    - broker_ca_only: just the broker CA

    File names: bundle-<fingerprint[:16]>.pem and ca-<fingerprint[:16]>.pem.
    Idempotent: existing files are reused only when their content matches,
    and every write goes through a temp file and an atomic rename.
    """
    # Name the files by the certificate's SHA-256 fingerprint
    prefix = x509.load_pem_x509_certificate(ca_pem.encode("utf-8")).fingerprint(hashes.SHA256()).hex()[:16]

    combined_path = directory / f"bundle-{prefix}.pem"
    ca_only_path = directory / f"ca-{prefix}.pem"

    # Build combined bundle: system roots + broker CA
    # Try ssl.get_default_verify_paths().cafile first, fall back to certifi
    default_paths = ssl.get_default_verify_paths()
    if default_paths.cafile and Path(default_paths.cafile).exists():
        system_roots = Path(default_paths.cafile).read_text()
    else:
        system_roots = Path(certifi.where()).read_text()

    # Ensure system roots ends with newline before appending broker CA
    combined = system_roots
    if not combined.endswith("\n"):
        combined += "\n"
    combined += ca_pem

    # Ensure directory exists
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _publish_unless_current(directory_fd, ca_only_path.name, ca_pem.encode("utf-8"))
        _publish_unless_current(directory_fd, combined_path.name, combined.encode("utf-8"))
    finally:
        os.close(directory_fd)

    return combined_path, ca_only_path
