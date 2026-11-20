"""The root `tailnet_ssh` config section: who may sign in to their worker from their tailnet, and with which keys."""

import base64
import binascii
import re

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.serialization import load_ssh_public_key
from pydantic import BaseModel, ConfigDict, Field, field_validator

from mindroom.config.access import validate_concrete_matrix_user_ids
from mindroom.config.schema_hints import dashboard_hint

# OpenSSH key type names, such as ssh-ed25519, ecdsa-sha2-nistp256, or sk-ssh-ed25519@openssh.com.
_KEY_TYPE_PATTERN = re.compile(r"(?:ssh|ecdsa|sk)-[A-Za-z0-9.@-]+")
# The armor line a pasted OpenSSH, PEM, or PKCS#8 private key starts with.
_PRIVATE_KEY_HEADER = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_NOT_A_KEY = "That is not an SSH public key line; paste one line from a .pub file."


def _is_canonical_base64(token: str) -> bool:
    """Return whether a key blob is padded base64 in OpenSSH's spelling; the loader skips characters OpenSSH refuses."""
    try:
        return base64.b64encode(base64.b64decode(token, validate=True)).decode() == token
    except binascii.Error:
        return False


def normalize_public_key(line: str) -> str:
    """Return one OpenSSH public key line as ``<type> <base64>[ <comment>]``, or raise ``ValueError`` with a sentence.

    Options such as ``command=`` are refused, because the projected ``authorized_keys`` sets its own.
    """
    text = line.strip()
    if not text:
        msg = "The key is empty; paste one line from a .pub file."
        raise ValueError(msg)
    if _PRIVATE_KEY_HEADER.match(text):
        msg = "That is a private key; paste the .pub file."
        raise ValueError(msg)
    if any(character in text for character in "\r\n\0"):
        msg = "Paste one key per line."
        raise ValueError(msg)
    fields = text.split(maxsplit=2)
    if not _KEY_TYPE_PATTERN.fullmatch(fields[0]) and any(_KEY_TYPE_PATTERN.fullmatch(field) for field in text.split()):
        msg = "Key options such as command= are not accepted; paste the key line without them."
        raise ValueError(msg)
    if len(fields) < 2 or not _is_canonical_base64(fields[1]):
        raise ValueError(_NOT_A_KEY)
    try:
        load_ssh_public_key(f"{fields[0]} {fields[1]}".encode())
    except (ValueError, UnsupportedAlgorithm):
        raise ValueError(_NOT_A_KEY) from None
    return " ".join(fields)


class TailnetSshConfig(BaseModel):
    """Operator-supplied tailnet SSH settings, keyed by concrete Matrix user ID."""

    model_config = ConfigDict(extra="forbid")

    logins: dict[str, str] = Field(
        default_factory=dict,
        description="The Tailscale login, such as bas@github, each person's operator keys admit from their tailnet",
        json_schema_extra=dashboard_hint(key_placeholder="@alice:example.org"),
    )
    authorized_keys: dict[str, list[str]] = Field(
        default_factory=dict,
        description="SSH public key lines each person may sign in with, for installs where the operator knows them",
        json_schema_extra=dashboard_hint(key_placeholder="@alice:example.org"),
    )

    @field_validator("logins")
    @classmethod
    def _validate_logins(cls, value: dict[str, str]) -> dict[str, str]:
        validate_concrete_matrix_user_ids(list(value), field_name="tailnet_ssh.logins")
        return value

    @field_validator("authorized_keys")
    @classmethod
    def _validate_authorized_keys(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        validate_concrete_matrix_user_ids(list(value), field_name="tailnet_ssh.authorized_keys")
        return {user_id: [normalize_public_key(line) for line in lines] for user_id, lines in value.items()}
