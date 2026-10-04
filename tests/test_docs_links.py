"""Source-level checks for links, anchors, and redirect stubs in docs/.

Every relative Markdown link must reach an existing file, every fragment must name a heading id or an
explicit `<a id>` anchor on its target page, no page may define the same id twice, no link may point
at a redirect stub, and every redirect stub must point at an existing page and id.  Heading ids follow
Python-Markdown's toc rules (the slugs Zensical publishes), so the check runs without building the site.
"""

from __future__ import annotations

import html
import posixpath
import re
import unicodedata
from functools import cache
from pathlib import Path

import yaml

DOCS = Path(__file__).resolve().parents[1] / "docs"
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING = re.compile(r"^(#{1,6}) (.*?)\s*#*\s*$")
_EXPLICIT_ID = re.compile(r"^(.*?)\s*\{#([^}\s]+)\}$")
_ANCHOR = re.compile(r'<a id="([^"]+)"></a>')
_INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+\1")
_LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_DEFINITION = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+\".*\")?\s*$")


def _pages() -> list[Path]:
    return sorted(p for p in DOCS.rglob("*.md") if "overrides" not in p.relative_to(DOCS).parts)


def _rel(path: Path) -> str:
    return path.relative_to(DOCS).as_posix()


def _outside_fences(lines: list[str]) -> list[tuple[int, str]]:
    out, fence = [], None
    for number, line in enumerate(lines, 1):
        match = _FENCE.match(line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence is None:
            out.append((number, line))
    return out


def _front_matter(text: str) -> dict:
    if not text.startswith("---\n"):
        return {}
    front, separator, _ = text[4:].partition("\n---\n")
    return (yaml.safe_load(front) or {}) if separator else {}


def _slug(text: str) -> str:
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    parts = re.split(r"(`+[^`]*`+)", text)
    text = "".join(
        part.strip("`")
        if part.startswith("`")
        else re.sub(r"(?<!\w)[*_]{1,3}|[*_]{1,3}(?!\w)", "", re.sub(r"<[^>]+>", "", part))
        for part in parts
    )
    text = html.unescape(text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text)


def _unique(candidate: str, used: set[str]) -> str:
    while candidate in used or not candidate:
        match = re.match(r"^(.*)_([0-9]+)$", candidate)
        candidate = f"{match.group(1)}_{int(match.group(2)) + 1}" if match else f"{candidate}_1"
    used.add(candidate)
    return candidate


@cache
def _ids(page: str) -> tuple[list[str], dict]:
    """All ids a page defines (headings, explicit ids, raw anchors) and its front matter."""
    text = (DOCS / page).read_text(encoding="utf-8")
    body = text.split("\n")
    headings, explicit = [], set()
    for _, line in _outside_fences(body):
        match = _HEADING.match(line)
        if match:
            title, fixed = match.group(2), None
            pinned = _EXPLICIT_ID.match(title)
            if pinned:
                title, fixed = pinned.group(1), pinned.group(2)
                explicit.add(fixed)
            headings.append((title, fixed))
    used = set(explicit)
    ids = [fixed or _unique(_slug(title), used) for title, fixed in headings]
    for _, line in _outside_fences(body):
        ids += _ANCHOR.findall(_INLINE_CODE.sub("", line))
    return ids, _front_matter(text)


def _is_stub(page: str) -> bool:
    return _ids(page)[1].get("template") == "redirect.html"


def _links(page: str) -> list[tuple[int, str]]:
    lines = (DOCS / page).read_text(encoding="utf-8").split("\n")
    found = []
    for number, line in _outside_fences(lines):
        if line.startswith(("    ", "\t")):
            continue  # indented code block
        bare = _INLINE_CODE.sub("", line)
        found += [(number, m.group(1)) for m in _LINK.finditer(bare)]
        definition = _DEFINITION.match(bare)
        if definition:
            found.append((number, definition.group(1)))
    return found


def _resolve(page: str, target: str) -> tuple[str, str] | None:
    """Docs-relative path and fragment for a relative link, or None for external links."""
    if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith(("//", "/")):
        return None
    path, _, fragment = target.partition("#")
    resolved = page if not path else posixpath.normpath(posixpath.join(posixpath.dirname(page), path))
    return resolved, fragment


def _url_page(page: str, location: str) -> tuple[str | None, str]:
    """Source page and fragment a redirect stub's directory-style location points at."""
    path, _, fragment = location.partition("#")
    own = "" if page == "index.md" else page[: -len("index.md")] if page.endswith("/index.md") else page[:-3] + "/"
    target = posixpath.normpath(posixpath.join(own, path)).strip("/")
    if target in ("", "."):
        return "index.md", fragment
    for candidate in (f"{target}.md", f"{target}/index.md"):
        if (DOCS / candidate).exists():
            return candidate, fragment
    return None, fragment


def test_links_and_fragments_resolve() -> None:
    """Relative links reach existing files and ids, and never a redirect stub."""
    problems = []
    for path in _pages():
        page = _rel(path)
        if _is_stub(page):
            continue
        for number, target in _links(page):
            resolved = _resolve(page, target)
            if resolved is None:
                continue
            file, fragment = resolved
            where = f"{page}:{number}: {target}"
            if file.startswith("..") or not (DOCS / file).exists():
                if not file.startswith(".."):
                    problems.append(f"{where}: missing file")
                continue
            if not file.endswith(".md"):
                continue
            if _is_stub(file):
                problems.append(f"{where}: links to a redirect stub")
            elif fragment and fragment not in _ids(file)[0]:
                problems.append(f"{where}: no id {fragment!r} on {file}")
    assert problems == []


def test_ids_are_unique_per_page() -> None:
    """No page defines the same heading or anchor id twice."""
    problems = []
    for path in _pages():
        ids = _ids(_rel(path))[0]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            problems.append(f"{_rel(path)}: {duplicates}")
    assert problems == []


def test_redirect_stubs_point_at_existing_sections() -> None:
    """Redirect stubs have no body and point at an existing page and id."""
    problems = []
    for path in _pages():
        page = _rel(path)
        if not _is_stub(page):
            continue
        meta = _ids(page)[1]
        target, default = _url_page(page, str(meta.get("location", "")))
        if target is None or _is_stub(target):
            problems.append(f"{page}: location {meta.get('location')!r} is not a content page")
            continue
        wanted = {default, *dict(meta.get("fragments") or {}).values()} - {""}
        missing = sorted(i for i in wanted if i not in _ids(target)[0])
        if missing:
            problems.append(f"{page}: {target} has no ids {missing}")
        if path.read_text(encoding="utf-8").split("\n---\n", 1)[1].strip():
            problems.append(f"{page}: a redirect stub must not have a body")
    assert problems == []
