"""Generate reference files for the bundled mindroom-docs skill."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"
ZENSICAL_CONFIG = REPO_ROOT / "zensical.toml"
SKILL_DIR = REPO_ROOT / "skills" / "mindroom-docs"
REFERENCES_DIR = SKILL_DIR / "references"
CACHE_PATH = REPO_ROOT / ".cache" / "mindroom-docs-skill-references.sha256"


@dataclass(frozen=True)
class NavPage:
    """Structured docs page entry extracted from zensical.toml navigation."""

    title: str
    source_path: str
    built_path: str


def _source_to_built_path(source_path: str) -> str:
    source = Path(source_path)
    parent = source.parent.as_posix()
    if source.name in {"index.md", "README.md"}:
        return "index.md" if parent == "." else f"{parent}/index.md"

    stem = source.stem
    return f"{stem}/index.md" if parent == "." else f"{parent}/{stem}/index.md"


def _collect_nav_pages(items: list[Any], pages: list[NavPage]) -> None:
    for item in items:
        assert isinstance(item, dict), "Expected each project.nav entry to be a table in zensical.toml"
        for title, value in item.items():
            if isinstance(value, str):
                pages.append(
                    NavPage(
                        title=str(title),
                        source_path=value,
                        built_path=_source_to_built_path(value),
                    ),
                )
                continue
            assert isinstance(value, list), f"Expected nested nav list for {title!r} in zensical.toml"
            _collect_nav_pages(value, pages)


def _load_project_and_nav() -> tuple[dict[str, Any], list[NavPage]]:
    parsed = tomllib.loads(ZENSICAL_CONFIG.read_text(encoding="utf-8"))
    project = parsed.get("project", {})
    assert isinstance(project, dict), "Expected [project] table in zensical.toml"
    nav = project.get("nav", [])
    assert isinstance(nav, list), "Expected project.nav to be a list in zensical.toml"

    pages: list[NavPage] = []
    _collect_nav_pages(nav, pages)
    return project, pages


def _digest_paths(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        relative_path = path.relative_to(REPO_ROOT).as_posix()
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _cache_key() -> str:
    inputs = sorted([Path(__file__).resolve(), ZENSICAL_CONFIG, *DOCS_DIR.rglob("*.md")])
    references = sorted(path for path in REFERENCES_DIR.rglob("*") if path.is_file()) if REFERENCES_DIR.exists() else []
    return f"{_digest_paths(inputs)}\n{_digest_paths(references)}"


def _mkdocs_config(project: dict[str, Any], nav_pages: list[NavPage], site_dir: Path) -> dict[str, Any]:
    site_name = str(project.get("site_name", "MindRoom"))
    site_description = str(project.get("site_description", "MindRoom documentation"))
    site_url = str(project.get("site_url", "https://docs.mindroom.chat/"))
    nav = project.get("nav", [])
    assert isinstance(nav, list), "Expected project.nav to be a list in zensical.toml"

    return {
        "site_name": site_name,
        "site_description": site_description,
        "site_url": site_url,
        "docs_dir": str(DOCS_DIR),
        "site_dir": str(site_dir),
        "nav": nav,
        "plugins": [
            "search",
            {
                "llmstxt": {
                    "full_output": "llms-full.txt",
                    "sections": {
                        "MindRoom Docs": [page.source_path for page in nav_pages],
                    },
                },
            },
        ],
    }


def _run_mkdocs_build(config: dict[str, Any], temp_dir: Path) -> Path:
    config_path = temp_dir / "mkdocs.llms.yml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    subprocess.run(
        [
            "uvx",
            "--with",
            "mkdocs",
            "--with",
            "mkdocs-llmstxt",
            "mkdocs",
            "build",
            "-f",
            str(config_path),
        ],
        cwd=REPO_ROOT,
        check=True,
    )

    return Path(config["site_dir"]).resolve()


def _clear_reference_dir() -> None:
    REFERENCES_DIR.mkdir(parents=True, exist_ok=True)
    for path in REFERENCES_DIR.iterdir():
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)


def _copy_main_outputs(site_dir: Path) -> None:
    for filename in ("llms.txt", "llms-full.txt"):
        source = site_dir / filename
        assert source.exists(), f"Expected generated file: {source}"
        shutil.copyfile(source, REFERENCES_DIR / filename)


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return text

    _, separator, rest = text[4:].partition("\n---\n")
    return rest if separator else text


def _source_paragraph_lines(source_text: str) -> list[list[str]]:
    paragraphs: list[list[str]] = []
    current: list[str] = []
    in_code = False

    for line in _strip_frontmatter(source_text).splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code

        starts_block = (
            in_code
            or not stripped
            or line.startswith((" ", "\t"))
            or stripped.startswith(("#", "-", "*", "|", ">", "```", "===", "!!!"))
            or stripped[0].isdigit()
        )
        if starts_block:
            if current:
                paragraphs.append(current)
                current = []
            continue

        current.append(stripped)

    if current:
        paragraphs.append(current)

    return paragraphs


def _source_fence_openings(source_text: str) -> list[str]:
    openings: list[str] = []
    in_code = False
    for line in _strip_frontmatter(source_text).splitlines():
        stripped = line.strip()
        if not stripped.startswith("```"):
            continue
        if not in_code:
            openings.append(stripped)
        in_code = not in_code
    return openings


def _restore_source_fence_languages(rendered_text: str, source_text: str) -> str:
    source_openings = _source_fence_openings(source_text)
    if not source_openings:
        return rendered_text

    restored_lines: list[str] = []
    in_code = False
    opening_index = 0
    for line in rendered_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("```"):
            restored_lines.append(line)
            continue

        if in_code:
            restored_lines.append(line)
            in_code = False
            continue

        source_opening = source_openings[opening_index] if opening_index < len(source_openings) else stripped
        opening_index += 1
        in_code = True
        if source_opening == stripped:
            restored_lines.append(line)
            continue

        prefix = line[: len(line) - len(line.lstrip())]
        restored_lines.append(f"{prefix}{source_opening}")

    suffix = "\n" if rendered_text.endswith("\n") else ""
    return "\n".join(restored_lines) + suffix


def _restore_source_line_breaks(rendered_text: str, source_text: str) -> str:
    for paragraph in _source_paragraph_lines(source_text):
        for left, right in pairwise(paragraph):
            rendered_text = rendered_text.replace(f"{left} {right}", f"{left}\n{right}")

    return _restore_source_fence_languages(rendered_text, source_text)


def _flatten_page_references(site_dir: Path, nav_pages: list[NavPage]) -> dict[str, str]:
    built_to_reference: dict[str, str] = {}
    for page in nav_pages:
        generated = site_dir / page.built_path
        assert generated.exists(), f"Expected generated page: {generated}"
        source_path = DOCS_DIR / page.source_path
        assert source_path.exists(), f"Expected docs source page: {source_path}"
        reference_name = f"page__{page.built_path.replace('/', '__')}"
        rendered_text = generated.read_text(encoding="utf-8")
        source_text = source_path.read_text(encoding="utf-8")
        restored_text = _restore_source_line_breaks(rendered_text, source_text)
        (REFERENCES_DIR / reference_name).write_text(restored_text, encoding="utf-8")
        built_to_reference[page.built_path] = reference_name
    return built_to_reference


def _write_reference_index(nav_pages: list[NavPage], built_to_reference: dict[str, str]) -> None:
    lines = [
        "# MindRoom Docs Reference Index",
        "",
        "Generated from `docs/` via `.github/scripts/generate_skill_references.py`.",
        "",
        "## Primary references",
        "",
        "- `llms.txt`: compact documentation index.",
        "- `llms-full.txt`: full merged documentation corpus.",
        "",
        "## Page references",
        "",
        "| Title | Source page | Built markdown | Reference file |",
        "| --- | --- | --- | --- |",
    ]

    for page in nav_pages:
        reference_name = built_to_reference.get(page.built_path)
        assert reference_name is not None, f"Missing built page for nav source {page.source_path!r}"
        lines.append(
            f"| {page.title} | `{page.source_path}` | `{page.built_path}` | `{reference_name}` |",
        )

    (REFERENCES_DIR / "reference-index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    """Build and sync generated docs references into the mindroom-docs skill."""
    if CACHE_PATH.exists() and CACHE_PATH.read_text(encoding="utf-8") == _cache_key():
        print(f"Generated references already up to date in {REFERENCES_DIR}")
        return

    project, nav_pages = _load_project_and_nav()
    assert nav_pages, "No docs pages found in zensical.toml navigation"
    with tempfile.TemporaryDirectory(prefix="mindroom-docs-skill-") as tmp:
        tmp_dir = Path(tmp)
        site_dir = tmp_dir / "site"
        config = _mkdocs_config(project, nav_pages, site_dir)
        generated_site_dir = _run_mkdocs_build(config, tmp_dir)

        _clear_reference_dir()
        _copy_main_outputs(generated_site_dir)
        built_to_reference = _flatten_page_references(generated_site_dir, nav_pages)
        _write_reference_index(nav_pages, built_to_reference)

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(_cache_key(), encoding="utf-8")
    print(f"Generated {len(list(REFERENCES_DIR.glob('*')))} files in {REFERENCES_DIR}")


if __name__ == "__main__":
    main()
