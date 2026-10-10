"""Local certificate authority for broker TLS interception."""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import os
import ssl
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from mindroom.atomic_file import atomic_write_bytes_at

__all__ = [
    "BrokerCA",
    "materialize_ca_bundle",
]

_CACHE_SIZE = 1024
_LEAF_VALIDITY_HOURS = 24
_CA_VALIDITY_YEARS = 10
_CACHE_REFRESH_MARGIN_SECONDS = 3600  # Replace cached context when <1h validity remains


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
        self._directory = directory

        # Leaf certificate and key (shared across all contexts in this process)
        self._leaf_key = ec.generate_private_key(ec.SECP256R1())

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

    @property
    def fingerprint(self) -> str:
        """Return SHA-256 fingerprint of CA certificate in hex."""
        der = self._ca_cert.public_bytes(serialization.Encoding.DER)
        return hashlib.sha256(der).hexdigest()

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

        cert_path = directory / "ca.pem"
        key_path = directory / "ca.key"

        if cert_path.exists() and key_path.exists():
            # Load existing CA
            cert_pem = cert_path.read_bytes()
            ca_cert = x509.load_pem_x509_certificate(cert_pem)

            # Check if CA has SubjectKeyIdentifier (required for strict TLS clients)
            try:
                ca_cert.extensions.get_extension_for_oid(x509.oid.ExtensionOID.SUBJECT_KEY_IDENTIFIER)
                has_ski = True
            except x509.ExtensionNotFound:
                has_ski = False

            # If CA lacks SKI, regenerate it (old CA created before this fix)
            if not has_ski:
                # Load the key and fall through to regeneration
                key_pem = key_path.read_bytes()
                ca_key = serialization.load_pem_private_key(
                    key_pem,
                    password=key_password,
                )
                if not isinstance(ca_key, ec.EllipticCurvePrivateKey):
                    msg = "CA key is not an ECDSA key"
                    raise TypeError(msg)
                # Fall through to regenerate CA with SKI below
            else:
                # Load key and return existing CA
                key_pem = key_path.read_bytes()
                ca_key = serialization.load_pem_private_key(
                    key_pem,
                    password=key_password,
                )
                if not isinstance(ca_key, ec.EllipticCurvePrivateKey):
                    msg = "CA key is not an ECDSA key"
                    raise TypeError(msg)

                return BrokerCA(
                    ca_cert=ca_cert,
                    ca_key=ca_key,
                    cert_pem=cert_pem.decode("utf-8"),
                    directory=directory,
                )
        else:
            # Generate new key if files don't exist
            ca_key = ec.generate_private_key(ec.SECP256R1())

        # Generate CA certificate (new or regenerated with SKI)
        subject = issuer = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, "MindRoom Egress Broker CA"),
            ],
        )

        now = datetime.now(UTC)
        ca_cert = (
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

        # Write certificate with mode 0644
        cert_pem_bytes = ca_cert.public_bytes(serialization.Encoding.PEM)
        cert_path.write_bytes(cert_pem_bytes)
        cert_path.chmod(0o644)

        # Write private key with mode 0600
        encryption = (
            serialization.BestAvailableEncryption(key_password) if key_password else serialization.NoEncryption()
        )
        key_pem_bytes = ca_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=encryption,
        )
        key_path.touch(mode=0o600)
        key_path.write_bytes(key_pem_bytes)
        key_path.chmod(0o600)

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

        # Get CA's SubjectKeyIdentifier for AuthorityKeyIdentifier
        ca_ski_ext = self._ca_cert.extensions.get_extension_for_oid(
            x509.oid.ExtensionOID.SUBJECT_KEY_IDENTIFIER,
        )
        assert isinstance(ca_ski_ext.value, x509.SubjectKeyIdentifier)
        ca_ski = ca_ski_ext.value

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
                x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski),
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
        """Load leaf certificate and key into context via temp file."""
        # Write cert chain (leaf + CA) and key to temp file
        cert_pem = leaf_cert.public_bytes(serialization.Encoding.PEM)
        ca_pem = self._ca_cert.public_bytes(serialization.Encoding.PEM)
        key_pem = self._leaf_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        # Create temp file in leaf temp directory
        tmp_path = self._leaf_tmp_dir / f"tmp-{os.getpid()}-{time.time_ns()}.pem"
        fd = os.open(
            tmp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            # Write cert chain and key
            os.write(fd, cert_pem + ca_pem + key_pem)
            os.close(fd)
            fd = -1  # Mark as closed

            # Load into context
            ctx.load_cert_chain(tmp_path)
        finally:
            if fd != -1:
                os.close(fd)
            # Unlink temp file
            with contextlib.suppress(Exception):
                tmp_path.unlink()


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
    """Materialize CA bundle files in the given directory.

    Returns (combined_bundle, broker_ca_only) where:
    - combined_bundle: system roots followed by broker CA
    - broker_ca_only: just the broker CA

    File names: bundle-<fingerprint[:16]>.pem and ca-<fingerprint[:16]>.pem.
    Idempotent: existing files are reused only when their content matches,
    and every write goes through a temp file and an atomic rename.
    """
    # Compute fingerprint for file naming
    cert = x509.load_pem_x509_certificate(ca_pem.encode("utf-8"))
    der = cert.public_bytes(serialization.Encoding.DER)
    fingerprint = hashlib.sha256(der).hexdigest()
    prefix = fingerprint[:16]

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
    directory.mkdir(parents=True, exist_ok=True)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _publish_unless_current(directory_fd, ca_only_path.name, ca_pem.encode("utf-8"))
        _publish_unless_current(directory_fd, combined_path.name, combined.encode("utf-8"))
    finally:
        os.close(directory_fd)

    return combined_path, ca_only_path
