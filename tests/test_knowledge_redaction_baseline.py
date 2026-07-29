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

import hashlib
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

#: The redactor this branch must not regress against, vendored rather than read
#: out of Git history: CI checks out shallow, so a ``git show`` of a pinned SHA
#: is unavailable exactly where this test matters most. A frozen copy is also
#: the more honest artifact -- a baseline that can be resolved differently in
#: different environments is not pinned.
_BASELINE_SHA = "8d17749ca154910477ecd601bc02ebd47e0b1c49"
_BASELINE_DIR = Path(__file__).resolve().parent / "baselines"
#: Digests of the vendored copies, so the integrity check needs no history.
_BASELINE_DIGESTS = {
    "git_urls_8d17749c.py.txt": "f19615d937fe2909e223774a4e3d662e06e6887ac950f53c90431b033851b1d1",
    "redaction_8d17749c.py.txt": "b55c6e32a1cebd97028b8a96565aa8f66152f5ec1685aa2cee5ac16aef5b7759",
}

_SECRET = "SUPERSECRETCANARY"  # noqa: S105


class _Redactor(Protocol):
    def __call__(self, value: str, /) -> str: ...


def _load_baseline_redactor() -> _Redactor:
    """Load the vendored baseline redactor without touching the real import graph."""
    # ``redaction`` imports ``mindroom.git_urls``; give it the baseline's copy in
    # a throwaway module so nothing here can shadow the installed package.
    git_urls = types.ModuleType("mindroom.git_urls")
    git_urls_source = (_BASELINE_DIR / f"git_urls_{_BASELINE_SHA[:8]}.py.txt").read_text(encoding="utf-8")
    exec(compile(git_urls_source, "<baseline git_urls>", "exec"), git_urls.__dict__)  # noqa: S102

    baseline = types.ModuleType("_baseline_redaction")
    redaction_source = (_BASELINE_DIR / f"redaction_{_BASELINE_SHA[:8]}.py.txt").read_text(encoding="utf-8")
    saved = sys.modules.get("mindroom.git_urls")
    sys.modules["mindroom.git_urls"] = git_urls
    try:
        exec(compile(redaction_source, "<baseline redaction>", "exec"), baseline.__dict__)  # noqa: S102
    finally:
        if saved is None:
            del sys.modules["mindroom.git_urls"]
        else:
            sys.modules["mindroom.git_urls"] = saved
    return baseline.redact_credentials_in_text  # type: ignore[no-any-return]


def test_the_vendored_baseline_is_the_file_it_claims_to_be() -> None:
    """The frozen copies must be unmodified, checkable without Git history.

    A digest rather than a ``git show``: CI checks out shallow, so a comparison
    against the commit skips exactly where it matters, and a skipped integrity
    check reads as proof while permitting the baseline to be edited into a
    no-op redactor that leaves all of the assertions above green.
    """
    for filename, expected in _BASELINE_DIGESTS.items():
        actual = hashlib.sha256((_BASELINE_DIR / filename).read_bytes()).hexdigest()
        assert actual == expected, f"{filename} has been modified since it was vendored"


def test_the_vendored_baseline_matches_the_commit_it_names() -> None:
    """Corroborate the digests against Git where the history is available.

    Skipped in a shallow clone. This proves the *provenance* the digests only
    freeze, so it is the weaker of the two checks and the one allowed to skip.
    """
    repository = Path(__file__).resolve().parent.parent
    for module, vendored in (
        ("src/mindroom/git_urls.py", f"git_urls_{_BASELINE_SHA[:8]}.py.txt"),
        ("src/mindroom/knowledge/redaction.py", f"redaction_{_BASELINE_SHA[:8]}.py.txt"),
    ):
        shown = subprocess.run(
            ["git", "-C", str(repository), "show", f"{_BASELINE_SHA}:{module}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if shown.returncode != 0:
            pytest.skip(f"{_BASELINE_SHA} is not present in this clone")
        assert (_BASELINE_DIR / vendored).read_text(encoding="utf-8") == shown.stdout


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


#: Credential-free strings that Git and Git LFS really emit, generated against
#: the same framings as the leak corpus rather than hand-listed. A hand-written
#: list's escape hatch is *omission*: a diagnostic this branch started mangling
#: would simply never appear in it, and nothing would say so.
_CLEAN_URLS = [
    "https://github.com/example/repo.git",
    "https://host/@scope/pkg.git",
    "https://host:8443/a@b",
    "https://host/org/repo.git@v1",
    "https://[::1]:8080/repo.git",
    "http://[fe80::1%25eth0]/x",
    "ssh://git@example.com/org/repo.git",
    "git@github.com:org/repo.git",
    "file:///srv/repos/repo.git",
    "/srv/repos/repo.git",
    # The string this codebase builds itself in ``_git_auth_env``, and the shape
    # ``git config --list`` prints back.
    "url.https://example.com/a@b.insteadOf=https://x/y",
]

#: Diagnostics with no URL to find, which must survive verbatim.
_CLEAN_PROSE = [
    "error: pathspec 'main' did not match any file(s) known to git",
    "git@github.com: Permission denied (publickey).",
    "fatal: invalid refspec '+refs/heads/main:refs/remotes/origin/@{upstream}'",
    "git merge-base HEAD:@{upstream} HEAD",
    "warning: refname 'HEAD@{upstream}' is ambiguous",
    "main@{0}: commit: initial import",
    "Author: Bas Nijholt <bas@example.com>",
    "git config --global user.email you@example.com",
    "error: RPC failed; curl 92 HTTP/2 stream 5 was not closed cleanly",
    "Cloning into '/srv/knowledge/docs'...",
    "Note: switching to 'origin/main'.",
    "remote: Support for password authentication was removed on August 13, 2021.",
    "Submodule 'vendor/x' (git@github.com:example/x.git) registered for path 'vendor/x'",
]

#: Credential-free shapes this branch deliberately says less about than the
#: baseline. Each costs a diagnostic, so each needs a reason, and the companion
#: test below fails if one stops diverging so the list cannot rot.
_INTENTIONAL_LOSSES = frozenset(
    {
        # An scp remote's userinfo is an SSH username rather than a password --
        # but nothing distinguishes ``git@`` from a token pasted into the same
        # position, and that token would be a credential.
        "git@github.com:org/repo.git",
        "Submodule 'vendor/x' (git@github.com:example/x.git) registered for path 'vendor/x'",
        # Two URLs in one token. The second could carry userinfo the first's
        # authority check cannot see, and telling them apart needs the parse
        # this shape is precisely too malformed to support.
        "url.https://example.com/a@b.insteadOf=https://x/y",
    },
)


def _clean_texts() -> Iterator[tuple[str, str]]:
    """Yield every generated (shape id, credential-free text)."""
    for url, (_frame_id, frame) in itertools.product(_CLEAN_URLS, _FRAMES):
        yield url, frame.format(url=url)
    for prose in _CLEAN_PROSE:
        yield prose, prose


def _clean_corpus() -> list[ParameterSet]:
    return [pytest.param(shape, text, id=f"{shape[:44]}-{index}") for index, (shape, text) in enumerate(_clean_texts())]


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


@pytest.mark.parametrize(("shape", "text"), _clean_corpus())
def test_clean_output_is_never_mangled_relative_to_the_baseline(
    shape: str,
    text: str,
    baseline_redact: _Redactor,
) -> None:
    """Redaction must not cost a diagnostic the baseline preserves."""
    here = redact_credentials_in_text(text)
    there = baseline_redact(text)
    if here != there and shape not in _INTENTIONAL_LOSSES:
        pytest.fail(f"undeclared loss for {shape!r}\n  here: {here!r}\n  base: {there!r}")


def test_every_declared_loss_is_still_a_loss(baseline_redact: _Redactor) -> None:
    """A declared loss that stopped diverging must be removed, not left to rot.

    The improvement allowlist already had a test like this; the loss direction
    did not, which is how a diagnostic could start being mangled with nothing to
    say so.
    """
    diverging = {shape for shape, text in _clean_texts() if redact_credentials_in_text(text) != baseline_redact(text)}

    assert diverging == _INTENTIONAL_LOSSES


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
