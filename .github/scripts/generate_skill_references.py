"""Generate reference files for the bundled mindroom-docs skill."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"
ZENSICAL_CONFIG = REPO_ROOT / "zensical.toml"
SKILL_DIR = REPO_ROOT / "skills" / "mindroom-docs"
REFERENCES_DIR = SKILL_DIR / "references"


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


def _flatten_page_references(site_dir: Path) -> dict[str, str]:
    built_to_reference: dict[str, str] = {}
    for source in sorted(site_dir.rglob("*.md")):
        relative = source.relative_to(site_dir).as_posix()
        reference_name = f"page__{relative.replace('/', '__')}"
        shutil.copyfile(source, REFERENCES_DIR / reference_name)
        built_to_reference[relative] = reference_name
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
    project, nav_pages = _load_project_and_nav()
    assert nav_pages, "No docs pages found in zensical.toml navigation"
    with tempfile.TemporaryDirectory(prefix="mindroom-docs-skill-") as tmp:
        tmp_dir = Path(tmp)
        site_dir = tmp_dir / "site"
        config = _mkdocs_config(project, nav_pages, site_dir)
        generated_site_dir = _run_mkdocs_build(config, tmp_dir)

        _clear_reference_dir()
        _copy_main_outputs(generated_site_dir)
        built_to_reference = _flatten_page_references(generated_site_dir)
        _write_reference_index(nav_pages, built_to_reference)

    print(f"Generated {len(list(REFERENCES_DIR.glob('*')))} files in {REFERENCES_DIR}")


if __name__ == "__main__":
    main()
