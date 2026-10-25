"""Verified in-place edits of a hand-written config, its ``!include`` files, and its ``.env``.

The config is edited as text, so comments, quote styles, and indentation survive.
Spans come from ``yaml.compose``, which needs no constructors, so custom tags such as ``!include`` stay as node tags.
Every edit is verified before it is written: the new text is composed again and its node tree must equal the old tree
with exactly the chosen scalars changed.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from dotenv.parser import parse_stream

from mindroom.agent_rename.steps import FileRewrite, RenameRefusedError
from mindroom.config.yaml_includes import load_yaml_config_source
from mindroom.yaml_io import SafeLoader

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

    from dotenv.parser import Binding

_STR_TAG = "tag:yaml.org,2002:str"
_MERGE_TAG = "tag:yaml.org,2002:merge"
_INCLUDE_TEXT_TAG = "!include_text"
_EXPAND_TAG = "!expand"
_INCLUDE_TAGS = frozenset(
    {
        "!include",
        _EXPAND_TAG,
        _INCLUDE_TEXT_TAG,
        "!include_dir_list",
        "!include_dir_named",
        "!include_dir_merge_list",
        "!include_dir_merge_named",
    },
)
_YAML_SUFFIXES = (".yaml", ".yml")
_BLOCK_STYLES = frozenset({"|", ">"})
# A node's start mark includes its anchor and tag, which must stay in front of the replaced scalar.
_PROPERTIES = re.compile(r"(?:[&!]\S*\s+(?:#[^\r\n]*\s+)*)*")
_FLOW_INDICATORS = ",[]{}"
_BOM = "\ufeff"
_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t", "\r": "\\r"}
# The mappings whose keys are agent names.
_AGENT_KEYED_MAPPINGS = frozenset({("agents",), ("calls", "agents")})
# The placeholders MindRoom expands in configured paths, matched as `mindroom.constants` matches them.
_PATH_PLACEHOLDER = re.compile(r"\$(?:\{(?P<braced>[A-Z0-9_]+)\}|(?P<bare>[A-Z0-9_]+))")
# What may follow `agents/<old>` in a value that is a path: nothing, or more path components without whitespace.
_PATH_REST = re.compile(r"(?:/\S*)?")

# Where a node sits in the merged config: the mapping keys above it, with None for a list item or a non-scalar key.
type _Place = tuple[str | None, ...]
# The scalars an edit changes, by node identity, with their new values.
type _Changes = dict[int, tuple[yaml.ScalarNode, str]]


def config_value_steps(config_path: Path, *, old_value: str, new_value: str) -> list[FileRewrite]:
    """Return one step per config file that holds a string value equal to ``old_value``.

    The files are the config itself plus every YAML file it includes; files read with ``!include_text`` are prose.
    Each step replaces those values with ``new_value`` and keeps their quote style.
    Mapping keys are never edited.
    Building the steps already runs every edit through its verification, so a dry run refuses what an apply would.
    """
    files = _config_files(config_path)
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
        for node, _, _ in _occurrences(root, ())
        if node.tag == _INCLUDE_TEXT_TAG and isinstance(node, yaml.ScalarNode)
    }

    steps: list[FileRewrite] = []
    for path, content in files.items():
        if path in text_files:
            continue
        if path in unparsable:
            msg = f"Cannot read the config file {path}: {unparsable[path]}"
            raise RenameRefusedError(msg)
        changes = partial(_value_changes, path=path, old_value=old_value, new_value=new_value)
        rewrite = partial(_rewrite, path=path, changes=changes)
        if rewrite(content) != content:
            description = f"Replace {old_value} with {new_value} in {path}"
            steps.append(FileRewrite(path=path, rewrite=rewrite, description=description))
    return steps


def config_entity_steps(config_path: Path, *, old_name: str, new_name: str, storage_root: Path) -> list[FileRewrite]:
    """Return one step per config file that names the agent ``old_name``, renaming it to ``new_name``.

    The files are the config itself plus every YAML file it includes, each judged by where it sits in the config.
    The step renames the keys ``agents.<old_name>`` and ``calls.agents.<old_name>``, the items equal to ``old_name``
    in ``teams.*.agents`` and ``agents.*.delegate_to``, and ``personal_rooms.agent`` when it equals ``old_name``.
    It also renames the ``agents/<old_name>`` component of every string value that is a path in the agent's
    state root ``<storage_root>/agents/<old_name>``, spelled absolute, ``~``-prefixed, relative to the config
    directory, or with a path placeholder such as ``${MINDROOM_STORAGE_PATH}``. Prose that mentions the agent,
    even prose starting with such a path, is not edited.
    Building the steps already runs every edit through its verification, so a dry run refuses what an apply would.
    """
    files = _config_files(config_path)
    rename = _AgentRename(
        old=old_name,
        new=new_name,
        config_path=config_path.resolve(),
        storage_root=storage_root.expanduser().resolve(),
    )
    steps: list[FileRewrite] = []
    for path, places in sorted(_file_places(config_path, files, rename).items()):
        changes = partial(_entity_changes, path=path, places=places, rename=rename)
        rewrite = partial(_rewrite, path=path, changes=changes)
        if rewrite(files[path]) != files[path]:
            description = f"Rename the agent {old_name} to {new_name} in {path}"
            steps.append(FileRewrite(path=path, rewrite=rewrite, description=description))
    return steps


def env_value_steps(env_path: Path, *, variable: str, old_value: str, new_value: str) -> list[FileRewrite]:
    """Return a step that sets ``variable`` from exactly ``old_value`` to ``new_value`` in a ``.env`` file.

    Only that value's text changes, keeping its quotes, comments, and every other line.
    python-dotenv, which loads the file, must read the result back as the same bindings with only that value changed.
    """
    if not env_path.exists():
        return []
    path = env_path.resolve()
    try:
        content = path.read_bytes()
    except OSError as exc:
        msg = f"Cannot read {path}: {exc}"
        raise RenameRefusedError(msg) from exc
    rewrite = partial(_replace_env_value, path=path, variable=variable, old_value=old_value, new_value=new_value)
    if rewrite(content) == content:
        return []
    description = f"Set {variable} from {old_value} to {new_value} in {path}"
    return [FileRewrite(path=path, rewrite=rewrite, description=description)]


def _config_files(config_path: Path) -> dict[Path, bytes]:
    """Return the bytes of the config and every file it includes, by resolved path."""
    try:
        _, source_files = load_yaml_config_source(config_path)
        return {path: path.read_bytes() for path in sorted(source_files)}
    except (OSError, yaml.YAMLError, UnicodeError) as exc:
        msg = f"Cannot read the config {config_path}: {exc}"
        raise RenameRefusedError(msg) from exc


@dataclass(frozen=True)
class _AgentRename:
    """Which scalars name the agent ``old``, and what they become."""

    old: str
    new: str
    config_path: Path
    storage_root: Path

    def new_value(self, node: yaml.Node, place: _Place, *, is_key: bool) -> str | None:
        """Return the new value of a scalar that names the agent at ``place``, or None to keep it."""
        if not isinstance(node, yaml.ScalarNode) or node.tag != _STR_TAG:
            return None
        if is_key:
            return self.new if node.value == self.old and place in _AGENT_KEYED_MAPPINGS else None
        if node.value == self.old and _names_an_agent(place):
            return self.new
        return self._renamed_state_path(node.value)

    def _renamed_state_path(self, value: str) -> str | None:
        """Return ``value`` with ``agents/<old>`` renamed when the value is a path in the agent's state root.

        A path is one line with no whitespace after ``agents/<old>``, so prose that starts with a path is not one.
        The part before ``agents/<old>`` is read as MindRoom reads configured paths: placeholders and ``~`` expanded,
        relative to the config directory. Only that part is resolved: the state root moves before the config edit.
        """
        if value.splitlines() != [value]:
            return None
        for match in re.finditer(rf"(?<![^/])agents/{re.escape(self.old)}(?![^/])", value):
            if not _PATH_REST.fullmatch(value, match.end()):
                continue
            prefix = value[: match.start()]
            try:
                root = (self.config_path.parent / Path(self._expand_placeholders(prefix)).expanduser()).resolve()
            except (OSError, RuntimeError, ValueError):
                # ValueError: a placeholder MindRoom rejects, or a NUL, which no path holds.
                continue
            if root == self.storage_root:
                return f"{prefix}agents/{self.new}{value[match.end() :]}"
        return None

    def _expand_placeholders(self, prefix: str) -> str:
        """Expand the path placeholders MindRoom allows; any other placeholder raises ``ValueError`` as in MindRoom."""
        values = {"MINDROOM_CONFIG_PATH": str(self.config_path), "MINDROOM_STORAGE_PATH": str(self.storage_root)}

        def replace(match: re.Match[str]) -> str:
            name = match.group("braced") or match.group("bare")
            if name not in values:
                msg = f"Unsupported path placeholder {name}"
                raise ValueError(msg)
            return values[name]

        return _PATH_PLACEHOLDER.sub(replace, prefix)


def _names_an_agent(place: _Place) -> bool:
    """Return whether a string value at ``place`` is an agent name."""
    match place:
        case ("teams", str(), "agents", None) | ("agents", str(), "delegate_to", None) | ("personal_rooms", "agent"):
            return True
    return False


def _file_places(config_path: Path, files: Mapping[Path, bytes], rename: _AgentRename) -> dict[Path, frozenset[_Place]]:
    """Return where each YAML file of the config sits in the merged config, following its include tags."""
    places: dict[Path, set[_Place]] = {}
    roots: dict[Path, yaml.Node | None] = {}
    text_files: set[Path] = set()
    pending: list[tuple[Path, _Place]] = [(config_path.resolve(), ())]
    while pending:
        path, place = pending.pop()
        if place in places.setdefault(path, set()):
            continue
        places[path].add(place)
        if path not in roots:
            try:
                roots[path] = _compose(files[path].decode("utf-8"))
            except (KeyError, UnicodeError, yaml.YAMLError) as exc:
                msg = f"Cannot read the config file {path}: {exc}"
                raise RenameRefusedError(msg) from exc
        for node, node_place, _ in _occurrences(roots[path], place):
            if not isinstance(node, yaml.ScalarNode) or node.tag not in _INCLUDE_TAGS:
                continue
            target = (path.parent / node.value).resolve()
            if node.tag == _INCLUDE_TEXT_TAG:
                text_files.add(target)
            elif node.tag in {"!include", _EXPAND_TAG}:
                pending.append((target, node_place))
            else:
                pending.extend(_directory_places(node.tag, target, node_place, files, rename))
    unplaced = sorted(files.keys() - places.keys() - text_files)
    if unplaced:
        msg = f"Cannot tell which part of the config {unplaced[0]} holds; rename the agent {rename.old} in it by hand."
        raise RenameRefusedError(msg)
    return {path: frozenset(file_places) for path, file_places in places.items()}


def _directory_places(
    tag: str,
    directory: Path,
    place: _Place,
    files: Mapping[Path, bytes],
    rename: _AgentRename,
) -> list[tuple[Path, _Place]]:
    """Return the files a directory include reads, with where each one's content sits in the config."""
    included = [
        path
        for path in sorted(files)
        if path.is_relative_to(directory)
        and path.suffix in _YAML_SUFFIXES
        and not any(part.startswith((".", "_")) for part in path.relative_to(directory).parts)
    ]
    if tag == "!include_dir_list":
        return [(path, (*place, None)) for path in included]
    if tag == "!include_dir_named":
        for path in included:
            if path.stem == rename.old and place in _AGENT_KEYED_MAPPINGS:
                msg = f"The agent {rename.old} is named by the file {path}; rename the file and the agent by hand."
                raise RenameRefusedError(msg)
        return [(path, (*place, path.stem)) for path in included]
    # The merge variants put each file's list items or mapping pairs straight into the including node.
    return [(path, place) for path in included]


def _entity_changes(
    root: yaml.Node | None,
    *,
    path: Path,
    places: frozenset[_Place],
    rename: _AgentRename,
) -> _Changes:
    """Return the scalars of one file that name the agent, judged at every place the file sits in the config."""
    changes: _Changes = {}
    kept: set[int] = set()
    roles: dict[int, set[bool]] = {}
    for file_place in places:
        for node, place, is_key in _occurrences(root, file_place):
            roles.setdefault(id(node), set()).add(is_key)
            new_value = rename.new_value(node, place, is_key=is_key)
            if new_value is None:
                kept.add(id(node))
            elif isinstance(node, yaml.ScalarNode):
                changes[id(node)] = (node, new_value)
    for key, (node, _) in changes.items():
        if key in kept:
            use = "is shared through an alias or include by a place that names the agent and one that does not"
            raise _shared(node, path, use)
    _refuse_keys_used_as_values(changes, roles, path=path)
    return changes


def _value_changes(root: yaml.Node | None, *, path: Path, old_value: str, new_value: str) -> _Changes:
    """Return every string value equal to ``old_value``; mapping keys are never changed."""
    changes: _Changes = {}
    roles: dict[int, set[bool]] = {}
    for node, _, is_key in _occurrences(root, ()):
        roles.setdefault(id(node), set()).add(is_key)
        if not is_key and isinstance(node, yaml.ScalarNode) and node.tag == _STR_TAG and node.value == old_value:
            changes[id(node)] = (node, new_value)
    _refuse_keys_used_as_values(changes, roles, path=path)
    return changes


def _refuse_keys_used_as_values(changes: _Changes, roles: Mapping[int, set[bool]], *, path: Path) -> None:
    """Refuse a picked scalar that an alias uses both as a mapping key and as a value.

    Its one span cannot be right in both roles, and the reparse compares nodes, so it would not notice.
    """
    for key, (node, _) in changes.items():
        if roles[key] == {True, False}:
            raise _shared(node, path, "is used both as a mapping key and as a value through an alias")


def _shared(node: yaml.ScalarNode, path: Path, use: str) -> RenameRefusedError:
    where = f"{path} (line {node.start_mark.line + 1})" if node.start_mark else str(path)
    return RenameRefusedError(f"The value {node.value} in {where} {use}; edit it by hand.")


def _rewrite(content: bytes, *, path: Path, changes: Callable[[yaml.Node | None], _Changes]) -> bytes:
    """Change the scalars ``changes`` picks and verify the result by reparsing it."""
    try:
        # libyaml does not count a leading byte order mark in its mark indexes, so set it aside.
        text = content.decode("utf-8")
        body = text.removeprefix(_BOM)
        old_root = _compose(body)
        picked = changes(old_root)
        new_body = _splice(body, _edits(body, picked.values(), path=path))
        new_root = _compose(new_body)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise _unverified(path) from exc
    new_values = {key: new_value for key, (_, new_value) in picked.items()}
    if _tree(new_root) != _tree(old_root, new_values=new_values):
        raise _unverified(path)
    return (text[: len(text) - len(body)] + new_body).encode("utf-8")


def _unverified(path: Path) -> RenameRefusedError:
    return RenameRefusedError(f"Config edit could not be verified in {path}")


def _compose(text: str) -> yaml.Node | None:
    # The loader MindRoom reads configs with, so the edit is judged by the parser that will read it.
    # Both its variants count mark indexes in characters, exactly what slicing a str needs.
    return yaml.compose(text, Loader=SafeLoader)


def _occurrences(
    node: yaml.Node | None,
    place: _Place,
    *,
    is_key: bool = False,
    seen: set[tuple[int, _Place, bool]] | None = None,
    ancestors: frozenset[int] = frozenset(),
) -> Iterator[tuple[yaml.Node, _Place, bool]]:
    """Yield every node with its place and whether it sits inside a mapping key.

    An aliased node is yielded once for each place it is used in; a recursive alias stops at its anchor.
    An ``!expand`` item and a merged mapping sit at the place of the list or mapping they are spliced into.
    """
    seen = set() if seen is None else seen
    if node is None or id(node) in ancestors or (id(node), place, is_key) in seen:
        return
    seen.add((id(node), place, is_key))
    yield node, place, is_key
    walk = partial(_occurrences, seen=seen, ancestors=ancestors | {id(node)})
    if isinstance(node, yaml.SequenceNode):
        for child in node.value:
            yield from walk(child, place if child.tag == _EXPAND_TAG else (*place, None), is_key=is_key)
    elif isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            yield from walk(key, place, is_key=True)
            if key.tag == _MERGE_TAG:
                for merged in value.value if isinstance(value, yaml.SequenceNode) else [value]:
                    yield from walk(merged, place, is_key=is_key)
            else:
                segment = key.value if isinstance(key, yaml.ScalarNode) else None
                yield from walk(value, (*place, segment), is_key=is_key)


def _tree(
    node: yaml.Node | None,
    *,
    new_values: Mapping[int, str] | None = None,
    seen: dict[int, int] | None = None,
) -> object:
    """Return a plain comparable form of a node tree: tags and values, no styles or marks.

    ``new_values`` replaces the values of the scalars it names by node identity.
    """
    new_values = {} if new_values is None else new_values
    seen = {} if seen is None else seen
    if node is None:
        return None
    if id(node) in seen:
        return ("alias", seen[id(node)])
    seen[id(node)] = len(seen)
    if isinstance(node, yaml.ScalarNode):
        return ("scalar", node.tag, new_values.get(id(node), node.value))
    if isinstance(node, yaml.SequenceNode):
        return ("sequence", node.tag, tuple(_tree(child, new_values=new_values, seen=seen) for child in node.value))
    pairs = tuple(
        (_tree(key, new_values=new_values, seen=seen), _tree(value, new_values=new_values, seen=seen))
        for key, value in node.value
    )
    return ("mapping", node.tag, pairs)


def _edits(text: str, picked: Iterable[tuple[yaml.ScalarNode, str]], *, path: Path) -> list[tuple[int, int, str]]:
    """Return ``(start, end, replacement)`` for each picked scalar."""
    edits: list[tuple[int, int, str]] = []
    for node, new_value in picked:
        start_mark, end_mark = node.start_mark, node.end_mark
        if start_mark is None or end_mark is None:
            raise _unverified(path)
        if node.style in _BLOCK_STYLES:
            msg = f"The value {node.value} in {path} (line {start_mark.line + 1}) is a block scalar; edit it by hand."
            raise RenameRefusedError(msg)
        properties = _PROPERTIES.match(text, start_mark.index)
        start = properties.end() if properties else start_mark.index
        # A plain scalar holding a flow indicator can only sit in block context, where the new value may hold one too.
        block_context = not node.style and any(character in node.value for character in _FLOW_INDICATORS)
        edits.append((start, end_mark.index, _render_scalar(new_value, node.style, block_context=block_context)))
    return edits


def _splice(text: str, edits: list[tuple[int, int, str]]) -> str:
    """Apply non-overlapping edits from the end so earlier offsets stay valid."""
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _render_scalar(value: str, style: str | None, *, block_context: bool = False) -> str:
    """Write ``value`` in the original scalar ``style`` when that keeps its meaning, else double-quoted.

    The libyaml loader reports a plain scalar's style as ``""``, the pure-Python one as ``None``.
    """
    if not style and _can_be_plain(value, block_context=block_context):
        return value
    if style == "'" and value.isprintable():
        return "'" + value.replace("'", "''") + "'"
    return _double_quoted(value)


def _can_be_plain(value: str, *, block_context: bool) -> bool:
    """Return whether ``value`` reads back as the same plain string in block context, and in flow context unless known."""
    if not value.isprintable() or (not block_context and any(character in value for character in _FLOW_INDICATORS)):
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


def _replace_env_value(content: bytes, *, path: Path, variable: str, old_value: str, new_value: str) -> bytes:
    """Replace the matching values in ``.env`` text and verify the result by reparsing it with python-dotenv."""
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise _unverified(path) from exc
    bindings = list(parse_stream(io.StringIO(text)))

    def matches(binding: Binding) -> bool:
        return not binding.error and binding.key == variable and binding.value == old_value

    # A binding's text runs from the whitespace before it to its line end, with the value right after the `=`.
    value_start = re.compile(
        rf"(\s*(?:export[^\S\r\n]+)?(?:{re.escape(variable)}|'{re.escape(variable)}')[^\S\r\n]*=[^\S\r\n]*)"
        rf"(['\"]?){re.escape(old_value)}\2",
    )
    pieces: list[str] = []
    for binding in bindings:
        piece = binding.original.string
        if matches(binding):
            found = value_start.match(piece)
            if found is None:
                msg = f"The value of {variable} in {path} (line {binding.original.line}) uses escapes; edit it by hand."
                raise RenameRefusedError(msg)
            quote = found.group(2)
            piece = f"{piece[: found.end(1)]}{quote}{new_value}{quote}{piece[found.end() :]}"
        pieces.append(piece)
    new_text = "".join(pieces)

    expected = [(b.key, new_value if matches(b) else b.value, b.error) for b in bindings]
    if [(b.key, b.value, b.error) for b in parse_stream(io.StringIO(new_text))] != expected:
        raise _unverified(path)
    return new_text.encode("utf-8")
