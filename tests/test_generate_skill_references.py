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


def test_restore_source_line_breaks_preserves_fenced_code_language() -> None:
    """Generated references should preserve language tags from source code fences."""
    generator = _load_generator()
    source_text = """\
## Delivery Policy

```yaml
matrix_delivery:
  ignore_unverified_devices: false
```
"""
    rendered_text = """\
## Delivery Policy

```
matrix_delivery:
  ignore_unverified_devices: false
```
"""

    assert generator._restore_source_line_breaks(rendered_text, source_text) == source_text
