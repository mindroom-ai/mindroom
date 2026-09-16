"""Tests for low-level plugin import transaction helpers."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import TYPE_CHECKING

from mindroom.tool_system import plugin_imports

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_prepare_module_installs_package_chain(tmp_path: Path) -> None:
    """Prepared plugin module execution should install packages and expose the module."""
    plugin_root = tmp_path / "plugins" / "demo"
    package_dir = plugin_root / "nested"
    package_dir.mkdir(parents=True)
    module_path = package_dir / "tools.py"
    module_path.write_text("VALUE = 42\n", encoding="utf-8")

    module_name = plugin_imports._module_name("demo", plugin_root, module_path)
    package_names = [
        package_name for package_name, _ in plugin_imports._package_chain_names("demo", plugin_root, module_path)
    ]

    try:
        module, loader, _ = plugin_imports._prepare_module(
            "demo",
            plugin_root,
            module_path,
            module_name,
        )
        loader.exec_module(module)

        assert module.VALUE == 42
        assert sys.modules[module_name] is module
        for package_name in package_names:
            assert package_name in sys.modules
    finally:
        sys.modules.pop(module_name, None)
        for package_name in package_names:
            sys.modules.pop(package_name, None)


def test_prepare_module_restores_package_chain_on_spec_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec failures should restore pre-existing packages and remove synthetic ones."""
    plugin_root = tmp_path / "plugins" / "demo"
    package_dir = plugin_root / "nested"
    package_dir.mkdir(parents=True)
    module_path = package_dir / "tools.py"
    module_path.write_text("VALUE = 42\n", encoding="utf-8")

    package_names = [
        package_name for package_name, _ in plugin_imports._package_chain_names("demo", plugin_root, module_path)
    ]
    existing_package = ModuleType(package_names[0])
    sys.modules[package_names[0]] = existing_package
    monkeypatch.setattr(plugin_imports.util, "spec_from_file_location", lambda *_args, **_kwargs: None)

    try:
        module_execution = plugin_imports._prepare_module(
            "demo",
            plugin_root,
            module_path,
            plugin_imports._module_name("demo", plugin_root, module_path),
        )

        assert module_execution is None
        assert sys.modules[package_names[0]] is existing_package
        for package_name in package_names[1:]:
            assert package_name not in sys.modules
    finally:
        for package_name in package_names:
            sys.modules.pop(package_name, None)
