"""Prove this branch's redactor is never worse than the one that ships.

Eight rounds of review found credential shapes one at a time, and the fix for
each was checked against the shapes already known. That is unbounded: the ninth
round found that closing shapes had silently *broken* six others, because
nothing compared the whole surface against what production already does.

So the contract here is comparative, not absolute:

    no worse than the pinned baseline, better in exactly these enumerated ways

Both directions are asserted. A shape the baseline redacts must not survive
here, and a diagnostic the baseline leaves alone must not be mangled here. When
this branch legitimately improves on the baseline, the shape is named in
``_INTENTIONAL_IMPROVEMENTS`` -- an allowlist that has to be extended
deliberately, so an improvement cannot be confused with a divergence.

The consequence, which is the point: a credential shape found later is fixed
here **only if the baseline closes it too**. If the baseline leaks it as well it
is a pre-existing bug and belongs in its own change, not in another round of
this one.
"""

from __future__ import annotations

import itertools
import subprocess
import sys
import types
from base64 import b64encode
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.parse import quote

import pytest

from mindroom.knowledge.redaction import redact_credentials_in_text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from _pytest.mark import ParameterSet

#: The redactor this branch must not regress against, pinned to a commit rather
#: than a branch name so the comparison cannot drift when main moves.
_BASELINE_REV = "8d17749ca154910477ecd601bc02ebd47e0b1c49"

_SECRET = "SUPERSECRETCANARY"  # noqa: S105


class _Redactor(Protocol):
    def __call__(self, value: str, /) -> str: ...


def _load_baseline_redactor() -> _Redactor:
    """Load the pinned redaction module without disturbing the real import graph."""
    repository = Path(__file__).resolve().parent.parent

    def _blob(path: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), "show", f"{_BASELINE_REV}:{path}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    # ``redaction`` imports ``mindroom.git_urls``; give it the baseline's copy in
    # a throwaway module so nothing here can shadow the installed package.
    git_urls = types.ModuleType("mindroom.git_urls")
    exec(compile(_blob("src/mindroom/git_urls.py"), "<baseline git_urls>", "exec"), git_urls.__dict__)  # noqa: S102
    baseline = types.ModuleType("_baseline_redaction")
    saved = sys.modules.get("mindroom.git_urls")
    sys.modules["mindroom.git_urls"] = git_urls
    try:
        exec(compile(_blob("src/mindroom/knowledge/redaction.py"), "<baseline redaction>", "exec"), baseline.__dict__)  # noqa: S102
    finally:
        if saved is None:
            del sys.modules["mindroom.git_urls"]
        else:
            sys.modules["mindroom.git_urls"] = saved
    return baseline.redact_credentials_in_text  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def baseline_redact() -> _Redactor:
    """Return the pinned baseline redactor."""
    return _load_baseline_redactor()


#: Shapes this branch closes that the baseline leaks, and diagnostics it stops
#: mangling. Each entry is a deliberate divergence; anything not listed here
#: must behave identically.
_INTENTIONAL_IMPROVEMENTS = frozenset(
    {
        "empty-authority",
        "nested-scheme",
        "slashless-scheme",
        "protocol-relative",
        "encoded-authority",
        "userinfo-no-scheme",
        "spaced-auth-header",
        "scp-with-path",
    },
)

#: Framing seen in real Git and Git LFS output. A *leading* frame is what breaks
#: an anchored matcher, which is why the empty prefix is only one of these.
_FRAMES = [
    ("bare", "{url}"),
    ("quoted", "'{url}'"),
    ("bracket", "[{url}]"),
    ("paren", "({url})"),
    ("backtick", "`{url}`"),
    ("lfs-endpoint", "Endpoint={url} (auth=none)"),
    ("config-list", "remote.origin.url={url}"),
    ("prose", "fatal: unable to access '{url}': The requested URL returned error: 403"),
    ("trailing-comma", "see {url}, then retry"),
]


def _encoded(value: str, depth: int) -> str:
    """Return `value` with its separators percent-encoded `depth` times."""
    for _ in range(depth):
        value = quote(value, safe="")
    return value


def _credential_urls() -> Iterator[tuple[str, str]]:
    """Yield (shape id, URL) for URLs that carry a credential."""
    for scheme in ("https", "http", "ssh", "git+https"):
        yield "authority-userinfo", f"{scheme}://user:{_SECRET}@example.com/org/repo.git"
        yield "empty-authority", f"{scheme}:///user:{_SECRET}@example.com/org/repo.git"
        yield "slashless-scheme", f"{scheme}:/user:{_SECRET}@example.com/org/repo.git"
        yield "nested-scheme", f"{scheme}://example.com/{scheme}://user:{_SECRET}@inner/x"
    yield "protocol-relative", f"//user:{_SECRET}@example.com/org/repo.git"
    yield "userinfo-no-scheme", f"oauth2:{_SECRET}@gitlab.com:org/repo.git"
    yield "userinfo-no-scheme", f"x-access-token:{_SECRET}@github.com/org/repo.git"
    yield "scp-with-path", f"{_SECRET}@github.com:org/repo.git"
    for depth in (1, 2, 3):
        userinfo = _encoded(f"user:{_SECRET}@", depth)
        yield "encoded-authority", f"https://{userinfo}example.com/org/repo.git"


def _credential_headers() -> Iterator[tuple[str, str]]:
    """Yield (shape id, text) for Authorization headers carrying a credential."""
    token = b64encode(f"x-access-token:{_SECRET}".encode()).decode("ascii")
    for separator in (":", " :", "\t:\t", "  :   "):
        yield "spaced-auth-header", f"Authorization{separator} Basic {token}"
        yield "spaced-auth-header", f"Authorization{separator} Bearer {_SECRET}"


#: Diagnostics with no credential in them. The baseline leaves every one alone,
#: so this branch must too -- these are what an operator reads when a sync fails.
_CLEAN_DIAGNOSTICS = [
    "fatal: repository 'https://github.com/example/repo.git/' not found",
    "fatal: unable to access 'https://host/@scope/pkg.git': 404",
    "fatal: unable to access 'https://host:8443/a@b': 404",
    "fatal: could not read 'https://host/org/repo.git@v1'",
    "git@github.com: Permission denied (publickey).",
    "fatal: invalid refspec '+refs/heads/main:refs/remotes/origin/@{upstream}'",
    "git merge-base HEAD:@{upstream} HEAD",
    "warning: refname 'HEAD@{upstream}' is ambiguous",
    "main@{0}: commit: initial import",
    "Author: Bas Nijholt <bas@example.com>",
    "git config --global user.email you@example.com",
    "error: RPC failed; curl 92 HTTP/2 stream 5 was not closed cleanly",
    "Cloning into '/srv/knowledge/docs'...",
    "https://[::1]:8080/repo.git",
    "http://[fe80::1%25eth0]/x",
    "Note: switching to 'origin/main'.",
]


def _credential_corpus() -> list[ParameterSet]:
    cases = []
    for (shape, url), (frame_id, frame) in itertools.product(_credential_urls(), _FRAMES):
        cases.append(pytest.param(shape, frame.format(url=url), id=f"{shape}-{frame_id}-{len(cases)}"))
    for index, (shape, text) in enumerate(_credential_headers()):
        cases.append(pytest.param(shape, text, id=f"{shape}-{index}"))
    return cases


def _credential_texts() -> Iterator[tuple[str, str]]:
    """Yield every generated (shape id, text) the corpus covers."""
    for (shape, url), (_frame_id, frame) in itertools.product(_credential_urls(), _FRAMES):
        yield shape, frame.format(url=url)
    yield from _credential_headers()


@pytest.mark.parametrize(("shape", "text"), _credential_corpus())
def test_credential_shapes_are_never_worse_than_the_baseline(
    shape: str,
    text: str,
    baseline_redact: _Redactor,
) -> None:
    """A credential the baseline hides must not survive here, at any framing."""
    baseline_leaks = _SECRET in baseline_redact(text)
    branch_leaks = _SECRET in redact_credentials_in_text(text)

    if branch_leaks and not baseline_leaks:
        pytest.fail(f"regression: {shape} leaks here but not in the baseline")
    if not branch_leaks and baseline_leaks and shape not in _INTENTIONAL_IMPROVEMENTS:
        pytest.fail(
            f"undeclared improvement: {shape} is closed here but leaks in the baseline; add it to the allowlist",
        )


@pytest.mark.parametrize("diagnostic", _CLEAN_DIAGNOSTICS)
def test_clean_diagnostics_are_not_mangled_relative_to_the_baseline(
    diagnostic: str,
    baseline_redact: _Redactor,
) -> None:
    """Redaction must not cost a diagnostic the baseline preserves."""
    assert redact_credentials_in_text(diagnostic) == baseline_redact(diagnostic)


def test_every_allowlisted_improvement_is_still_an_improvement(baseline_redact: _Redactor) -> None:
    """The allowlist must not accumulate entries that no longer apply.

    Without this an entry outlives its reason and starts excusing a regression
    that happens to share its name.
    """
    closed_here_and_leaked_by_baseline = {
        shape
        for shape, text in _credential_texts()
        if _SECRET in baseline_redact(text) and _SECRET not in redact_credentials_in_text(text)
    }

    assert closed_here_and_leaked_by_baseline == _INTENTIONAL_IMPROVEMENTS


def test_scp_remotes_lose_their_username_in_diagnostics(baseline_redact: _Redactor) -> None:
    """The one place this branch deliberately says less than the baseline.

    An scp remote's userinfo is an SSH username, not a password, so the baseline
    leaves it. But nothing distinguishes ``git@`` from a token pasted into the
    same position, and that token *is* a credential -- so the username goes and
    the host and path stay, which is what keeps the remote identifiable.
    """
    submodule_line = "Submodule 'vendor/x' (git@github.com:example/x.git) registered for path 'vendor/x'"

    assert baseline_redact(submodule_line) == submodule_line
    assert redact_credentials_in_text(submodule_line) == (
        "Submodule 'vendor/x' (***@github.com:example/x.git) registered for path 'vendor/x'"
    )
