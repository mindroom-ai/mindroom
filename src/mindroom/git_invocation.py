"""Hardened ``git`` invocation shared by every Git command MindRoom runs itself.

MindRoom runs Git from processes that hold every runtime secret, against trees
agents and worker containers can write, and Git runs programs that repository
metadata names. Knowledge sync therefore names a MindRoom-owned Git directory
explicitly, since no ``-c`` override can anticipate an arbitrary
``filter.<name>``; every command also carries overrides for the keys that name
a program, reads no system or global configuration, and gets a minimal
environment. Callers add the repository credential only to remote commands.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = ["hardened_git_command", "hardened_git_env"]

#: Command-line configuration outranks every configuration file. The null
#: device disables hooks outright: nothing can ever be created beneath it.
#: ``safe.bareRepository=explicit`` stops a discovering command from adopting a
#: bare-repository layout built without any ``.git`` path component.
_HARDENED_GIT_CONFIG = (
    "core.fsmonitor=false",
    f"core.hooksPath={os.devnull}",
    "core.sshCommand=ssh",
    "core.askPass=",
    "core.gitProxy=",
    "credential.helper=",
    "diff.external=",
    "protocol.ext.allow=never",
    "safe.bareRepository=explicit",
    "submodule.recurse=false",
)

#: What a Git child needs to reach the network and authenticate as the operator
#: configured it. Everything else in the caller's environment stays behind.
_INHERITED_ENV = (
    "ALL_PROXY",
    "CURL_CA_BUNDLE",
    "GIT_SSH_COMMAND",
    "GIT_SSL_CAINFO",
    "GIT_SSL_CAPATH",
    "HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NIX_SSL_CERT_FILE",
    "NO_PROXY",
    "SSH_AUTH_SOCK",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)

#: Transports a fetch may use. Everything else, remote helpers and ``ext::``
#: included, is refused regardless of any ``protocol.*`` configuration.
_REMOTE_PROTOCOLS = "file:git:http:https:ssh"


def hardened_git_command(args: Sequence[str]) -> list[str]:
    """Return the argv for one ``git`` command with the hardening overrides."""
    overrides = [arg for override in _HARDENED_GIT_CONFIG for arg in ("-c", override)]
    return ["git", "--no-pager", *overrides, *args]


def hardened_git_env(
    overrides: Mapping[str, str] | None = None,
    *,
    git_dir: Path | None = None,
    work_tree: Path | None = None,
    remote: bool = False,
) -> dict[str, str]:
    """Return the minimal environment for one ``git`` child.

    ``git_dir`` and ``work_tree`` travel in the environment rather than as flags
    so that every program Git starts, ``git-lfs`` above all, operates on the
    same repository instead of rediscovering one. Only commands that talk to the
    remote pass ``remote``; every other command may open no transport at all,
    so an index read that would lazily fetch missing objects cannot reach one.
    The fixed values are applied after ``overrides`` so no caller replaces them.

    Relative ``PATH`` entries are dropped because they would resolve against a
    working directory somebody else can write, and so would an empty ``PATH``.
    """
    path_entries = os.environ.get("PATH", os.defpath).split(os.pathsep)
    env = {name: value for name in _INHERITED_ENV if (value := os.environ.get(name))}
    env["PATH"] = os.pathsep.join(entry for entry in path_entries if Path(entry).is_absolute()) or os.defpath
    env["GIT_TERMINAL_PROMPT"] = "0"
    if overrides:
        env.update(overrides)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_ALLOW_PROTOCOL"] = _REMOTE_PROTOCOLS if remote else ""
    env["GIT_NO_LAZY_FETCH"] = "1"
    if git_dir is not None:
        env["GIT_DIR"] = str(git_dir)
    if work_tree is not None:
        env["GIT_WORK_TREE"] = str(work_tree)
    return env
