"""Hardened ``git`` invocation for every MindRoom-owned Git command.

MindRoom runs ``git`` against checkouts that agents, their tools and worker
containers can write. Git resolves configuration, hooks and filters relative to
the repository it operates on, so a writable repository is a search path for
commands the *calling* process executes. Two rules keep that from turning into
code execution inside a MindRoom process:

* The Git directory itself is kept out of every writable tree, and the commands
  below are pointed at it explicitly through ``GIT_DIR``/``GIT_WORK_TREE`` so
  Git never discovers a ``.git`` that lives beside the worktree files. This is
  the rule that matters: command-line configuration cannot neutralize an
  attacker-defined ``filter.<name>.smudge``, because the filter name is
  arbitrary and only the repository's own config can define it.
* Every command additionally carries ``-c`` overrides for the configuration
  keys that name a command, and every child starts from a small allowlisted
  environment instead of the caller's. A MindRoom process holds provider API
  keys, Matrix credentials and the credential encryption key; none of them
  belong in a Git child, let alone in anything a repository convinces Git to
  run.

Callers add the repository credential as an explicit environment override, so
it reaches only the commands that talk to the remote.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

__all__ = ["hardened_git_command", "hardened_git_env"]

#: Hooks live in the Git directory, which MindRoom keeps outside every writable
#: tree. Pointing ``core.hooksPath`` at the null device disables the mechanism
#: outright, including for a repository laid out by an older release: nothing
#: can ever be created beneath it, which a fixed "nonexistent" directory cannot
#: promise, and Git reports "no hook" for it without a warning.
_DISABLED_HOOKS_PATH = os.devnull

#: Configuration keys whose value Git executes as a command. Command-line
#: configuration outranks every configuration file, so these win over a
#: repository, global or system value. Keys with an open-ended name --
#: ``filter.<name>.*``, ``alias.<name>`` -- cannot be pinned this way, which is
#: why a command must never let Git discover a repository somebody else wrote:
#: name ``git_dir`` explicitly, or bound discovery with ``search_ceiling``.
_HARDENED_GIT_CONFIG: tuple[tuple[str, str], ...] = (
    ("core.fsmonitor", "false"),
    ("core.hooksPath", _DISABLED_HOOKS_PATH),
    ("core.sshCommand", "ssh"),
    ("core.askPass", ""),
    ("core.gitProxy", ""),
    ("credential.helper", ""),
    ("diff.external", ""),
    ("protocol.ext.allow", "never"),
    ("submodule.recurse", "false"),
)

#: Environment the child needs to find its tools, reach the network,
#: authenticate as the operator configured it, and be traced when an operator
#: asks for it. Everything else -- provider API keys, ``MINDROOM_API_KEY``,
#: ``CREDENTIALS_ENCRYPTION_KEY``, Matrix credentials, the whole ``.env`` -- is
#: deliberately left behind. ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_*``/
#: ``GIT_CONFIG_VALUE_*`` and ``GIT_CONFIG_PARAMETERS`` are not listed on
#: purpose: those are the channels this module and the credential injection
#: own, and an inherited value would collide with them.
_INHERITED_GIT_ENV_VARS = frozenset(
    {
        "ALL_PROXY",
        "APPDATA",
        "COMSPEC",
        "CURL_CA_BUNDLE",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_SYSTEM",
        "GIT_EXEC_PATH",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_SSL_CAINFO",
        "GIT_SSL_CAPATH",
        "GIT_TRACE",
        "GIT_TRACE2",
        "GIT_TRACE2_EVENT",
        "GIT_TRACE2_PERF",
        "GIT_TRACE_PACKET",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LOCALAPPDATA",
        "LOGNAME",
        "NIX_SSL_CERT_FILE",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "SSH_AUTH_SOCK",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "SystemRoot",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERNAME",
        "USERPROFILE",
        "XDG_CONFIG_HOME",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    },
)

#: Never wait on a terminal that is not there: an unauthenticated remote must
#: fail the sync instead of hanging until the command timeout.
_FORCED_GIT_ENV: Mapping[str, str] = {"GIT_TERMINAL_PROMPT": "0"}


def hardened_git_command(args: list[str]) -> list[str]:
    """Return the full argv for one ``git`` command, hardening included."""
    overrides: list[str] = []
    for key, value in _HARDENED_GIT_CONFIG:
        overrides.extend(["-c", f"{key}={value}"])
    return ["git", "--no-pager", *overrides, *args]


def hardened_git_env(
    overrides: Mapping[str, str] | None = None,
    *,
    git_dir: Path | None = None,
    work_tree: Path | None = None,
    search_ceiling: Path | None = None,
) -> dict[str, str]:
    """Return a minimal environment for one ``git`` child.

    ``git_dir`` and ``work_tree`` are passed through the environment rather
    than as flags so that every process Git re-executes -- ``git-lfs`` above
    all -- operates on the same repository instead of rediscovering one from
    the working directory. A command that runs before any repository exists
    passes ``search_ceiling`` instead: its working directory, which Git then
    checks alone rather than walking up into trees it does not own.

    These are applied after ``overrides`` so no caller-supplied value can
    replace them.
    """
    env = {name: value for name, value in os.environ.items() if name in _INHERITED_GIT_ENV_VARS}
    env.setdefault("PATH", os.defpath)
    env.update(_FORCED_GIT_ENV)
    if overrides:
        env.update(overrides)
    if git_dir is not None:
        env["GIT_DIR"] = str(git_dir)
    if work_tree is not None:
        env["GIT_WORK_TREE"] = str(work_tree)
    if search_ceiling is not None:
        env["GIT_CEILING_DIRECTORIES"] = str(search_ceiling.parent)
    return env
