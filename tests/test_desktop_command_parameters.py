"""Tests for strict, bounded desktop command parameters."""

from __future__ import annotations

import pytest

from mindroom.desktop.command_parameters import (
    optional_bool_parameter,
    optional_int_parameter,
    optional_str_parameter,
    reject_unexpected_parameters,
    required_int_parameter,
    required_object_parameter,
    required_str_parameter,
)
from mindroom.desktop.protocol import DesktopProtocolError


def test_reject_unexpected_parameters_names_every_extra_parameter_in_sorted_order() -> None:
    """Accepted parameters pass, and every extra one is named once in sorted order."""
    reject_unexpected_parameters({"app": "x", "state_id": "s"}, allowed=frozenset({"app", "state_id"}))
    reject_unexpected_parameters({}, allowed=frozenset({"app"}))
    with pytest.raises(DesktopProtocolError, match=r"^Unexpected desktop parameters: b, c\.$"):
        reject_unexpected_parameters({"c": 1, "app": "x", "b": 2}, allowed=frozenset({"app"}))


def test_required_int_parameter_rejects_booleans_and_non_integers() -> None:
    """Only real integers are accepted; True and False are not integers here."""
    assert required_int_parameter({"x": 0}, "x") == 0
    assert required_int_parameter({"x": -3}, "x") == -3
    for parameters in ({"x": True}, {"x": False}, {"x": "3"}, {"x": 3.0}, {"x": None}, {}):
        with pytest.raises(DesktopProtocolError, match=r"^Desktop parameter x must be an integer\.$"):
            required_int_parameter(parameters, "x")


def test_optional_int_parameter_is_none_only_when_absent() -> None:
    """An absent key is None, while a present key must still be a real integer."""
    assert optional_int_parameter({}, "offset") is None
    assert optional_int_parameter({"offset": 0}, "offset") == 0
    for value in (None, True, "8"):
        with pytest.raises(DesktopProtocolError, match=r"^Desktop parameter offset must be an integer\.$"):
            optional_int_parameter({"offset": value}, "offset")


def test_optional_bool_parameter_defaults_to_false_and_rejects_non_booleans() -> None:
    """An absent flag is False, and only real booleans are accepted."""
    assert optional_bool_parameter({}, "force") is False
    assert optional_bool_parameter({"force": True}, "force") is True
    assert optional_bool_parameter({"force": False}, "force") is False
    for value in (1, 0, "yes", None):
        with pytest.raises(DesktopProtocolError, match=r"^Desktop parameter force must be a boolean\.$"):
            optional_bool_parameter({"force": value}, "force")


@pytest.mark.parametrize(
    ("key", "limit"),
    [
        ("text", 2_000),
        ("value", 2_000),
        ("path", 4_096),
        ("cwd", 4_096),
        ("command", 8_192),
        ("app", 256),
        ("root_id", 256),
        ("handle", 256),
    ],
)
def test_required_str_parameter_bounds_each_key_by_its_own_limit(key: str, limit: int) -> None:
    """Free text, paths, and commands have their own limits; every other key is an identifier of at most 256."""
    assert required_str_parameter({key: "é" * limit}, key) == "é" * limit
    with pytest.raises(DesktopProtocolError, match=rf"^Desktop parameter {key} must not exceed {limit} characters\.$"):
        required_str_parameter({key: "é" * (limit + 1)}, key)


def test_required_str_parameter_rejects_missing_empty_and_non_string_values() -> None:
    """A required string must be present, a string, and non-empty unless empty is explicitly allowed."""
    for parameters in ({}, {"app": ""}, {"app": 1}, {"app": None}):
        with pytest.raises(DesktopProtocolError, match=r"^Desktop parameter app must be a non-empty string\.$"):
            required_str_parameter(parameters, "app")


def test_required_str_parameter_allow_empty_accepts_only_strings_within_the_limit() -> None:
    """Clearing a value is allowed only as an empty string, and the length limit still applies."""
    assert required_str_parameter({"value": ""}, "value", allow_empty=True) == ""
    assert required_str_parameter({"value": "x"}, "value", allow_empty=True) == "x"
    for parameters in ({}, {"value": 1}, {"value": None}):
        with pytest.raises(DesktopProtocolError, match=r"^Desktop parameter value must be a string\.$"):
            required_str_parameter(parameters, "value", allow_empty=True)
    with pytest.raises(DesktopProtocolError, match=r"^Desktop parameter value must not exceed 2000 characters\.$"):
        required_str_parameter({"value": "x" * 2_001}, "value", allow_empty=True)


def test_required_object_parameter_requires_an_object_with_string_keys() -> None:
    """Only a mapping whose keys are all strings is accepted, and it is returned as a new dictionary."""
    value: dict[object, object] = {"request": {"kind": "click"}, "ref": "e3"}
    parsed = required_object_parameter({"browser_parameters": value}, "browser_parameters")
    assert parsed == value
    assert parsed is not value
    for parameters in (
        {},
        {"browser_parameters": ["ref"]},
        {"browser_parameters": "x"},
        {"browser_parameters": {1: "x"}},
    ):
        with pytest.raises(
            DesktopProtocolError,
            match=r"^Desktop parameter browser_parameters must be an object with string keys\.$",
        ):
            required_object_parameter(parameters, "browser_parameters")


def test_optional_str_parameter_uses_the_default_only_when_absent() -> None:
    """An absent key takes the default, while a present key must be a bounded non-empty string."""
    assert optional_str_parameter({}, "path", default=".") == "."
    assert optional_str_parameter({"path": "docs"}, "path", default=".") == "docs"
    with pytest.raises(DesktopProtocolError, match=r"^Desktop parameter path must be a non-empty string\.$"):
        optional_str_parameter({"path": ""}, "path", default=".")
    with pytest.raises(DesktopProtocolError, match=r"^Desktop parameter path must not exceed 4096 characters\.$"):
        optional_str_parameter({"path": "p" * 4_097}, "path", default=".")
