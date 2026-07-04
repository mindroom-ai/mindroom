"""Home-Assistant-style ``!include`` tags for splitting the YAML config across files.

Supported tags (Home Assistant semantics unless noted):

- ``!include rel/path.yaml`` — replace the node with the parsed content of that file.
- ``!include_text rel/path.md`` — MindRoom extension: replace the node with the file's
  raw text (UTF-8, one trailing newline stripped).
- ``!include_dir_list rel/dir`` — a list with one item per YAML file in the directory.
- ``!include_dir_named rel/dir`` — a mapping of filename-without-extension to content.
- ``!include_dir_merge_list rel/dir`` — concatenate the lists contained in each file.
- ``!include_dir_merge_named rel/dir`` — merge the mappings contained in each file.

Relative paths resolve against the directory of the file containing the tag.
Every resolved path must stay inside the top-level config file's directory.
Directory includes recurse into subdirectories, take only ``.yaml``/``.yml`` files in
lexicographic order of their relative path, and skip names starting with ``.`` or ``_``.
Unlike Home Assistant, duplicate keys across ``!include_dir_merge_named`` files and
duplicate filename stems across ``!include_dir_named`` files raise an error instead of
silently letting the later file win, and empty files contribute nothing to directory
includes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import yaml

_YAML_SUFFIXES = (".yaml", ".yml")


class ConfigIncludeError(yaml.YAMLError):
    """User-facing error raised when resolving config ``!include`` tags fails."""


class _IncludeLoader(yaml.SafeLoader):
    """SafeLoader that resolves include tags relative to the file being parsed."""

    def __init__(
        self,
        stream: str,
        *,
        source_path: Path,
        root_dir: Path,
        files_read: dict[Path, str],
        file_texts: dict[Path, str],
        include_chain: tuple[Path, ...],
    ) -> None:
        super().__init__(stream)
        # PyYAML names string streams '<unicode string>'; use the file path so
        # error marks report the offending file.
        self.name = str(source_path)
        self.source_path = source_path
        self.root_dir = root_dir
        self.files_read = files_read
        self.file_texts = file_texts
        self.include_chain = include_chain


def _include_error(message: str, node: yaml.Node) -> ConfigIncludeError:
    """Return one include error annotated with the tag's file and line."""
    mark = node.start_mark
    return ConfigIncludeError(f"{message} (in {mark.name}, line {mark.line + 1})")


def _display_path(path: Path, root_dir: Path) -> str:
    """Render one resolved path relative to the config directory when possible."""
    if path.is_relative_to(root_dir):
        return path.relative_to(root_dir).as_posix()
    return str(path)


def _resolve_include_path(loader: _IncludeLoader, node: yaml.Node) -> Path:
    """Resolve one include tag value against the including file with containment checks."""
    raw = loader.construct_scalar(cast("yaml.ScalarNode", node))
    if not isinstance(raw, str) or not raw:
        msg = f"{node.tag} expects a relative file path"
        raise _include_error(msg, node)
    if Path(raw).is_absolute():
        msg = f"{node.tag} does not allow absolute paths: '{raw}'"
        raise _include_error(msg, node)
    resolved = (loader.source_path.parent / raw).resolve()
    if not resolved.is_relative_to(loader.root_dir):
        msg = f"{node.tag}: '{raw}' resolves outside the configuration directory"
        raise _include_error(msg, node)
    return resolved


def _require_included_file(loader: _IncludeLoader, path: Path, node: yaml.Node) -> None:
    """Raise when one resolved include target is not an existing file."""
    if not path.is_file():
        display = _display_path(path, loader.root_dir)
        msg = f"{node.tag}: included file '{display}' does not exist"
        raise _include_error(msg, node)


def _check_include_cycle(loader: _IncludeLoader, path: Path, node: yaml.Node) -> None:
    """Raise when including ``path`` would re-enter a file currently being parsed."""
    if path not in loader.include_chain:
        return
    chain = " -> ".join(_display_path(entry, loader.root_dir) for entry in (*loader.include_chain, path))
    msg = f"{node.tag}: include cycle detected: {chain}"
    raise _include_error(msg, node)


def _read_file_recording_digest(
    path: Path,
    files_read: dict[Path, str],
    file_texts: dict[Path, str],
    *,
    source: bytes | None = None,
) -> str:
    """Read one file at most once per load, recording the digest of the bytes actually read.

    Digests captured at read time keep multi-file fingerprints coherent with the
    parsed content even when a file is edited mid-load; the text cache guarantees
    a file reachable through multiple include paths contributes one consistent
    copy instead of re-reading (and re-digesting) possibly changed bytes.
    """
    if path in file_texts:
        return file_texts[path]
    raw = path.read_bytes() if source is None else source
    files_read[path] = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8")
    file_texts[path] = text
    return text


def _read_included_text(loader: _IncludeLoader, path: Path, node: yaml.Node) -> str:
    """Read one included file as UTF-8 text, wrapping I/O failures as include errors."""
    try:
        return _read_file_recording_digest(path, loader.files_read, loader.file_texts)
    except (OSError, UnicodeError) as exc:
        display = _display_path(path, loader.root_dir)
        msg = f"{node.tag}: could not read '{display}': {exc}"
        raise _include_error(msg, node) from exc


def _parse_yaml_file(
    path: Path,
    *,
    root_dir: Path,
    files_read: dict[Path, str],
    file_texts: dict[Path, str],
    include_chain: tuple[Path, ...],
    source: bytes | None = None,
) -> object:
    """Parse one YAML file with include support, recording it in ``files_read``."""
    text = _read_file_recording_digest(path, files_read, file_texts, source=source)
    loader = _IncludeLoader(
        text,
        source_path=path,
        root_dir=root_dir,
        files_read=files_read,
        file_texts=file_texts,
        include_chain=include_chain,
    )
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _parse_included_yaml(loader: _IncludeLoader, path: Path, node: yaml.Node) -> object:
    """Parse one included YAML file after cycle and existence checks."""
    _check_include_cycle(loader, path, node)
    _require_included_file(loader, path, node)
    try:
        return _parse_yaml_file(
            path,
            root_dir=loader.root_dir,
            files_read=loader.files_read,
            file_texts=loader.file_texts,
            include_chain=(*loader.include_chain, path),
        )
    except (OSError, UnicodeError) as exc:
        display = _display_path(path, loader.root_dir)
        msg = f"{node.tag}: could not read '{display}': {exc}"
        raise _include_error(msg, node) from exc


def _included_dir_files(loader: _IncludeLoader, node: yaml.Node) -> list[Path]:
    """Return the directory include's YAML files in lexicographic relative-path order."""
    directory = _resolve_include_path(loader, node)
    if not directory.is_dir():
        display = _display_path(directory, loader.root_dir)
        msg = f"{node.tag}: included directory '{display}' does not exist"
        raise _include_error(msg, node)

    entries: list[tuple[str, Path]] = []
    seen_dirs: set[Path] = set()

    def _walk(current: Path) -> None:
        resolved_dir = current.resolve()
        if resolved_dir in seen_dirs:
            return
        seen_dirs.add(resolved_dir)
        for entry in current.iterdir():
            if entry.name.startswith((".", "_")):
                continue
            if entry.is_dir():
                # Check containment before recursing so a symlinked directory
                # pointing outside the config directory is rejected instead of
                # silently traversed.
                if not entry.resolve().is_relative_to(loader.root_dir):
                    msg = (
                        f"{node.tag}: '{entry.relative_to(directory).as_posix()}' resolves outside "
                        "the configuration directory"
                    )
                    raise _include_error(msg, node)
                _walk(entry)
                continue
            if entry.suffix not in _YAML_SUFFIXES or not entry.is_file():
                continue
            resolved = entry.resolve()
            if not resolved.is_relative_to(loader.root_dir):
                msg = (
                    f"{node.tag}: '{entry.relative_to(directory).as_posix()}' resolves outside "
                    "the configuration directory"
                )
                raise _include_error(msg, node)
            entries.append((entry.relative_to(directory).as_posix(), resolved))

    _walk(directory)
    return [path for _, path in sorted(entries)]


def _construct_include(loader: _IncludeLoader, node: yaml.Node) -> object:
    """``!include``: replace the node with the parsed content of one YAML file."""
    path = _resolve_include_path(loader, node)
    return _parse_included_yaml(loader, path, node)


def _construct_include_text(loader: _IncludeLoader, node: yaml.Node) -> str:
    """``!include_text``: replace the node with one file's raw text."""
    path = _resolve_include_path(loader, node)
    _require_included_file(loader, path, node)
    return _read_included_text(loader, path, node).removesuffix("\n")


def _construct_include_dir_list(loader: _IncludeLoader, node: yaml.Node) -> list[Any]:
    """``!include_dir_list``: one list item per YAML file in the directory."""
    items = (_parse_included_yaml(loader, path, node) for path in _included_dir_files(loader, node))
    return [item for item in items if item is not None]


def _construct_include_dir_named(loader: _IncludeLoader, node: yaml.Node) -> dict[str, Any]:
    """``!include_dir_named``: map filename-without-extension to parsed file content."""
    named: dict[str, Any] = {}
    key_sources: dict[str, Path] = {}
    for path in _included_dir_files(loader, node):
        content = _parse_included_yaml(loader, path, node)
        if content is None:
            continue
        key = path.stem
        if key in key_sources:
            msg = (
                f"{node.tag}: duplicate name '{key}' from both "
                f"'{_display_path(key_sources[key], loader.root_dir)}' and "
                f"'{_display_path(path, loader.root_dir)}'"
            )
            raise _include_error(msg, node)
        key_sources[key] = path
        named[key] = content
    return named


def _construct_include_dir_merge_list(loader: _IncludeLoader, node: yaml.Node) -> list[Any]:
    """``!include_dir_merge_list``: concatenate the lists contained in each file."""
    merged: list[Any] = []
    for path in _included_dir_files(loader, node):
        content = _parse_included_yaml(loader, path, node)
        if content is None:
            continue
        if not isinstance(content, list):
            display = _display_path(path, loader.root_dir)
            msg = f"{node.tag}: '{display}' must contain a YAML list, got {type(content).__name__}"
            raise _include_error(msg, node)
        merged.extend(content)
    return merged


def _construct_include_dir_merge_named(loader: _IncludeLoader, node: yaml.Node) -> dict[Any, Any]:
    """``!include_dir_merge_named``: merge the mappings contained in each file."""
    merged: dict[Any, Any] = {}
    key_sources: dict[Any, Path] = {}
    for path in _included_dir_files(loader, node):
        content = _parse_included_yaml(loader, path, node)
        if content is None:
            continue
        if not isinstance(content, dict):
            display = _display_path(path, loader.root_dir)
            msg = f"{node.tag}: '{display}' must contain a YAML mapping, got {type(content).__name__}"
            raise _include_error(msg, node)
        for key, value in content.items():
            if key in key_sources:
                msg = (
                    f"{node.tag}: duplicate key '{key}' defined in both "
                    f"'{_display_path(key_sources[key], loader.root_dir)}' and "
                    f"'{_display_path(path, loader.root_dir)}'"
                )
                raise _include_error(msg, node)
            key_sources[key] = path
            merged[key] = value
    return merged


_IncludeLoader.add_constructor("!include", _construct_include)
_IncludeLoader.add_constructor("!include_text", _construct_include_text)
_IncludeLoader.add_constructor("!include_dir_list", _construct_include_dir_list)
_IncludeLoader.add_constructor("!include_dir_named", _construct_include_dir_named)
_IncludeLoader.add_constructor("!include_dir_merge_list", _construct_include_dir_merge_list)
_IncludeLoader.add_constructor("!include_dir_merge_named", _construct_include_dir_merge_named)


def load_yaml_config_source_with_digests(
    path: Path,
    *,
    source: bytes | None = None,
) -> tuple[dict[str, Any], dict[Path, str]]:
    """Parse a config file, returning (data, sha256 hexdigest per file read).

    ``source`` optionally supplies the top-level file's bytes so a caller that
    already read the file parses and fingerprints exactly those bytes.
    """
    top = path.resolve()
    files_read: dict[Path, str] = {}
    file_texts: dict[Path, str] = {}
    data = _parse_yaml_file(
        top,
        root_dir=top.parent,
        files_read=files_read,
        file_texts=file_texts,
        include_chain=(top,),
        source=source,
    )
    return cast("dict[str, Any]", data or {}), files_read


def load_yaml_config_source(path: Path) -> tuple[dict[str, Any], frozenset[Path]]:
    """Parse a config file, resolving include tags. Returns (data, all_files_read).

    ``all_files_read`` includes ``path`` itself plus every transitively included file.
    Include targets must stay inside the top-level config file's directory.
    """
    data, files_read = load_yaml_config_source_with_digests(path)
    return data, frozenset(files_read)


def source_files_fingerprint(config_path: Path, source_digests: dict[Path, str]) -> str:
    """Return the stable identity of one config from per-file digests captured at read time.

    A single-file config keeps the plain content sha256 so the fingerprint still
    matches what callers compute from the written text alone.
    """
    if len(source_digests) == 1:
        return next(iter(source_digests.values()))
    root_dir = config_path.resolve().parent
    digest = hashlib.sha256()
    for file, file_digest in sorted(source_digests.items(), key=lambda item: item[0].as_posix()):
        digest.update(file.relative_to(root_dir).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(file_digest))
    return digest.hexdigest()
