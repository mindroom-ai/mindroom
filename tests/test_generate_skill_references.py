"""Tests for the mindroom-docs skill reference generator."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType


def _load_generator() -> ModuleType:
    script_path = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "generate_skill_references.py"
    spec = importlib.util.spec_from_file_location("generate_skill_references", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_source_page_reference_preserves_authored_markdown(tmp_path: Path) -> None:
    """Generated references should preserve authored Markdown exactly after frontmatter."""
    generator = _load_generator()
    source_text = """\
## Delivery Policy

```yaml
matrix_delivery:
  ignore_unverified_devices: false
```
"""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "delivery.md").write_text(f"---\ntitle: Delivery\n---\n{source_text}", encoding="utf-8")

    original_docs_dir = generator.DOCS_DIR
    generator.DOCS_DIR = docs_dir
    try:
        page = generator.NavPage(title="Delivery", source_path="delivery.md", built_path="delivery/index.md")
        assert generator._source_page_reference(page) == source_text
    finally:
        generator.DOCS_DIR = original_docs_dir


def test_source_page_reference_rewrites_relative_docs_links_to_published_urls(tmp_path: Path) -> None:
    """Generated references should keep source formatting while resolving local docs links."""
    generator = _load_generator()
    source_text = """\
See [Voice](../voice.md), [Models](models.md#file-based-secrets), and [External](https://example.com).
"""
    docs_dir = tmp_path / "docs"
    config_dir = docs_dir / "configuration"
    config_dir.mkdir(parents=True)
    (config_dir / "router.md").write_text(f"---\ntitle: Router\n---\n{source_text}", encoding="utf-8")
    (config_dir / "models.md").write_text("# Models\n", encoding="utf-8")
    (docs_dir / "voice.md").write_text("# Voice\n", encoding="utf-8")

    original_docs_dir = generator.DOCS_DIR
    generator.DOCS_DIR = docs_dir
    try:
        page = generator.NavPage(
            title="Router",
            source_path="configuration/router.md",
            built_path="configuration/router/index.md",
        )
        assert generator._source_page_reference(page) == (
            "See [Voice](https://docs.mindroom.chat/voice/), "
            "[Models](https://docs.mindroom.chat/configuration/models/#file-based-secrets), "
            "and [External](https://example.com).\n"
        )
    finally:
        generator.DOCS_DIR = original_docs_dir


def test_normalize_published_doc_urls_removes_index_markdown() -> None:
    """Generated plugin URLs should match the published trailing-slash docs routes."""
    generator = _load_generator()

    assert generator._normalize_published_doc_urls(
        "[Automation](https://docs.mindroom.chat/tools/automation-and-platforms/index.md) "
        "[Root](https://docs.mindroom.chat/index.md) "
        "[Anchor](https://docs.mindroom.chat/configuration/models/index.md#file-based-secrets)",
        site_url="https://docs.mindroom.chat/",
    ) == (
        "[Automation](https://docs.mindroom.chat/tools/automation-and-platforms/) "
        "[Root](https://docs.mindroom.chat/) "
        "[Anchor](https://docs.mindroom.chat/configuration/models/#file-based-secrets)"
    )
