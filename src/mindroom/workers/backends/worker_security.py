"""Security policy shared by dedicated worker backends."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from importlib.resources import files

_WORKER_COMPUTER_SECCOMP_PROFILE_SHA256 = "578ef2b662d8e9a886132148a0b75f0388efff92ed902b16d7be2777ae3788fa"
_DOCKER_WORKER_SECURITY_POLICY_VERSION = "cap-drop-all-nnp-v1"
_WORKER_COMPUTER_SECCOMP_RESOURCE = "seccomp/worker-computer.json"


@lru_cache(maxsize=1)
def _worker_computer_seccomp_profile_json() -> str:
    """Load the reviewed OCI seccomp profile and verify its packaged identity."""
    profile = files("mindroom.workers.backends").joinpath(_WORKER_COMPUTER_SECCOMP_RESOURCE).read_text(encoding="utf-8")
    digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()
    if digest != _WORKER_COMPUTER_SECCOMP_PROFILE_SHA256:
        msg = "Packaged worker computer seccomp profile failed its integrity check."
        raise RuntimeError(msg)
    return profile


def docker_worker_security_options() -> list[str]:
    """Return Docker security options for the explicitly selected Computer pool policy."""
    return ["no-new-privileges:true", f"seccomp={_worker_computer_seccomp_profile_json()}"]


def docker_worker_security_policy_signature() -> tuple[str, str]:
    """Return the stable identity of the reviewed Computer Docker host security settings."""
    return (_DOCKER_WORKER_SECURITY_POLICY_VERSION, _WORKER_COMPUTER_SECCOMP_PROFILE_SHA256)
