"""Verified in-place edits of string values in a hand-written config and its ``!include`` files.

The config is edited as text, so comments, quote styles, and indentation survive.
Spans come from ``yaml.compose``, which needs no constructors, so custom tags such as ``!include`` stay as node tags.
Every edit is verified before it is written: the new text is composed again and its node tree must equal the old tree
with exactly the matching string values replaced.
"""

from __future__ import annotations

import re
from functools import partial
from typing import TYPE_CHECKING

import yaml

from mindroom.agent_rename.steps import FileRewrite, RenameRefusedError
from mindroom.config.yaml_includes import load_yaml_config_source
from mindroom.yaml_io import SafeLoader

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_STR_TAG = "tag:yaml.org,2002:str"
_INCLUDE_TEXT_TAG = "!include_text"
_BLOCK_STYLES = frozenset({"|", ">"})
# A node's start mark includes its anchor and tag, which must stay in front of the replaced scalar.
_PROPERTIES = re.compile(r"(?:[&!]\S*\s+(?:#[^\r\n]*\s+)*)*")
_FLOW_INDICATORS = ",[]{}"
_BOM = "\ufeff"
_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t", "\r": "\\r"}


def config_value_steps(config_path: Path, *, old_value: str, new_value: str) -> list[FileRewrite]:
    """Return one step per config file that holds a string value equal to ``old_value``.

    The files are the config itself plus every YAML file it includes; files read with ``!include_text`` are prose.
    Each step replaces those values with ``new_value`` and keeps their quote style.
    Mapping keys are never edited.
    Building the steps already runs every edit through its verification, so a dry run refuses what an apply would.
    """
    try:
        _, source_files = load_yaml_config_source(config_path)
        files = {path: path.read_bytes() for path in sorted(source_files)}
    except (OSError, yaml.YAMLError, UnicodeError) as exc:
        msg = f"Cannot read the config {config_path}: {exc}"
        raise RenameRefusedError(msg) from exc

    roots: dict[Path, yaml.Node | None] = {}
    unparsable: dict[Path, Exception] = {}
    for path, content in files.items():
        try:
            roots[path] = _compose(content.decode("utf-8"))
        except (UnicodeError, yaml.YAMLError) as exc:
            unparsable[path] = exc
    text_files = {
        (path.parent / node.value).resolve()
        for path, root in roots.items()
        for node, _ in _walk(root)
        if node.tag == _INCLUDE_TEXT_TAG and isinstance(node, yaml.ScalarNode)
    }

    steps: list[FileRewrite] = []
    for path, content in files.items():
        if path in text_files:
            continue
        if path in unparsable:
            msg = f"Cannot read the config file {path}: {unparsable[path]}"
            raise RenameRefusedError(msg)
        rewrite = partial(_replace_values, path=path, old_value=old_value, new_value=new_value)
        if rewrite(content) != content:
            description = f"Replace {old_value} with {new_value} in {path}"
            steps.append(FileRewrite(path=path, rewrite=rewrite, description=description))
    return steps


def _replace_values(content: bytes, *, path: Path, old_value: str, new_value: str) -> bytes:
    """Replace every string value equal to ``old_value`` and verify the result by reparsing it."""
    try:
        # libyaml does not count a leading byte order mark in its mark indexes, so set it aside.
        text = content.decode("utf-8")
        body = text.removeprefix(_BOM)
        old_root = _compose(body)
        new_body = _splice(body, _edits(body, old_root, path=path, old_value=old_value, new_value=new_value))
        new_root = _compose(new_body)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise _unverified(path) from exc
    if _tree(new_root) != _tree(old_root, replacement=(old_value, new_value)):
        raise _unverified(path)
    return (text[: len(text) - len(body)] + new_body).encode("utf-8")


def _unverified(path: Path) -> RenameRefusedError:
    return RenameRefusedError(f"Config edit could not be verified in {path}")


def _compose(text: str) -> yaml.Node | None:
    # The loader MindRoom reads configs with, so the edit is judged by the parser that will read it.
    # Both its variants count mark indexes in characters, exactly what slicing a str needs.
    return yaml.compose(text, Loader=SafeLoader)


def _walk(
    node: yaml.Node | None,
    *,
    is_key: bool = False,
    seen: set[int] | None = None,
) -> Iterator[tuple[yaml.Node, bool]]:
    """Yield every node once, with whether it sits inside a mapping key; aliases revisit nothing."""
    seen = set() if seen is None else seen
    if node is None or id(node) in seen:
        return
    seen.add(id(node))
    yield node, is_key
    if isinstance(node, yaml.SequenceNode):
        for child in node.value:
            yield from _walk(child, is_key=is_key, seen=seen)
    elif isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            yield from _walk(key, is_key=True, seen=seen)
            yield from _walk(value, is_key=is_key, seen=seen)


def _tree(
    node: yaml.Node | None,
    *,
    replacement: tuple[str, str] | None = None,
    is_key: bool = False,
    seen: dict[int, int] | None = None,
) -> object:
    """Return a plain comparable form of a node tree: tags and values, no styles or marks."""
    seen = {} if seen is None else seen
    if node is None:
        return None
    if id(node) in seen:
        return ("alias", seen[id(node)])
    seen[id(node)] = len(seen)
    if isinstance(node, yaml.ScalarNode):
        value = node.value
        if replacement is not None and not is_key and node.tag == _STR_TAG and value == replacement[0]:
            value = replacement[1]
        return ("scalar", node.tag, value)
    if isinstance(node, yaml.SequenceNode):
        children = tuple(_tree(child, replacement=replacement, is_key=is_key, seen=seen) for child in node.value)
        return ("sequence", node.tag, children)
    pairs = tuple(
        (
            _tree(key, replacement=replacement, is_key=True, seen=seen),
            _tree(value, replacement=replacement, is_key=is_key, seen=seen),
        )
        for key, value in node.value
    )
    return ("mapping", node.tag, pairs)


def _edits(
    text: str,
    root: yaml.Node | None,
    *,
    path: Path,
    old_value: str,
    new_value: str,
) -> list[tuple[int, int, str]]:
    """Return ``(start, end, replacement)`` for each string value equal to ``old_value``."""
    edits: list[tuple[int, int, str]] = []
    for node, is_key in _walk(root):
        if is_key or not isinstance(node, yaml.ScalarNode) or node.tag != _STR_TAG or node.value != old_value:
            continue
        start_mark, end_mark = node.start_mark, node.end_mark
        if start_mark is None or end_mark is None:
            raise _unverified(path)
        if node.style in _BLOCK_STYLES:
            msg = f"The value {old_value} in {path} (line {start_mark.line + 1}) is a block scalar; edit it by hand."
            raise RenameRefusedError(msg)
        properties = _PROPERTIES.match(text, start_mark.index)
        start = properties.end() if properties else start_mark.index
        edits.append((start, end_mark.index, _render_scalar(new_value, node.style)))
    return edits


def _splice(text: str, edits: list[tuple[int, int, str]]) -> str:
    """Apply non-overlapping edits from the end so earlier offsets stay valid."""
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _render_scalar(value: str, style: str | None) -> str:
    """Write ``value`` in the original scalar ``style`` when that keeps its meaning, else double-quoted.

    The libyaml loader reports a plain scalar's style as ``""``, the pure-Python one as ``None``.
    """
    if not style and _can_be_plain(value):
        return value
    if style == "'" and value.isprintable():
        return "'" + value.replace("'", "''") + "'"
    return _double_quoted(value)


def _can_be_plain(value: str) -> bool:
    """Return whether ``value`` reads back as the same plain string in block and flow context."""
    if not value.isprintable() or any(character in value for character in _FLOW_INDICATORS):
        return False
    try:
        node = _compose(value)
    except yaml.YAMLError:
        return False
    return isinstance(node, yaml.ScalarNode) and not node.style and node.tag == _STR_TAG and node.value == value


def _double_quoted(value: str) -> str:
    """Quote ``value`` with YAML double-quote escapes for everything that is not printable."""
    pieces = ['"']
    for character in value:
        if character in _ESCAPES:
            pieces.append(_ESCAPES[character])
        elif character.isprintable():
            pieces.append(character)
        elif ord(character) <= 0xFF:
            pieces.append(f"\\x{ord(character):02x}")
        elif ord(character) <= 0xFFFF:
            pieces.append(f"\\u{ord(character):04x}")
        else:
            pieces.append(f"\\U{ord(character):08x}")
    pieces.append('"')
    return "".join(pieces)
