"""Tests for the native calculator toolkit."""

from __future__ import annotations

import json
import math

import pytest

from mindroom.custom_tools.calculator import CalculatorTools
from mindroom.tool_system.authoring import Toolkit
from mindroom.tools.calculator import calculator_tools


def test_calculator_toolkit_is_native_with_consolidated_function() -> None:
    """Calculator exposes a single action-dispatched calculate function."""
    tool_class = calculator_tools()
    calculator = tool_class()

    assert issubclass(tool_class, Toolkit)
    assert tool_class is CalculatorTools
    assert calculator.name == "calculator"
    assert list(calculator.functions) == ["calculate"]


def test_calculator_operations_and_error_cases() -> None:
    """Basic and advanced calculator operations return structured JSON results and domain errors."""
    calculator = CalculatorTools()

    assert json.loads(calculator.calculate("add", 2, 3.5)) == {"operation": "addition", "result": 5.5}
    assert json.loads(calculator.calculate("subtract", 10, 4)) == {"operation": "subtraction", "result": 6}
    assert json.loads(calculator.calculate("multiply", 3, 7)) == {"operation": "multiplication", "result": 21}
    assert json.loads(calculator.calculate("divide", 9, 2)) == {"operation": "division", "result": 4.5}
    assert json.loads(calculator.calculate("divide", 9, 0)) == {
        "operation": "division",
        "error": "Division by zero is undefined",
    }
    assert json.loads(calculator.calculate("exponentiate", 2, 5)) == {"operation": "exponentiation", "result": 32.0}
    assert json.loads(calculator.calculate("factorial", 0)) == {"operation": "factorial", "result": 1}
    assert json.loads(calculator.calculate("factorial", 5.0)) == {"operation": "factorial", "result": 120}
    assert json.loads(calculator.calculate("factorial", -1)) == {
        "operation": "factorial",
        "error": "Factorial of a negative number is undefined",
    }
    assert json.loads(calculator.calculate("is_prime", -3)) == {"operation": "prime_check", "result": False}
    assert json.loads(calculator.calculate("is_prime", 1)) == {"operation": "prime_check", "result": False}
    assert json.loads(calculator.calculate("is_prime", 2)) == {"operation": "prime_check", "result": True}
    assert json.loads(calculator.calculate("is_prime", 9)) == {"operation": "prime_check", "result": False}
    assert json.loads(calculator.calculate("is_prime", 17.0)) == {"operation": "prime_check", "result": True}
    assert json.loads(calculator.calculate("square_root", 9)) == {"operation": "square_root", "result": 3.0}
    assert json.loads(calculator.calculate("square_root", -4)) == {
        "operation": "square_root",
        "error": "Square root of a negative number is undefined",
    }


@pytest.mark.parametrize(
    ("action", "a", "b", "expected_operation"),
    [
        ("add", 2, None, "addition"),
        ("divide", 2, math.nan, "division"),
        ("multiply", 2, math.inf, "multiplication"),
        ("subtract", 2, True, "subtraction"),
        ("factorial", 3.5, None, "factorial"),
        ("factorial", True, None, "factorial"),
        ("factorial", math.inf, None, "factorial"),
        ("is_prime", 7.2, None, "prime_check"),
        ("is_prime", False, None, "prime_check"),
        ("is_prime", math.nan, None, "prime_check"),
    ],
)
def test_calculator_operand_validation(
    action: str,
    a: float,
    b: float | None,
    expected_operation: str,
) -> None:
    """Binary ops require finite b and integer ops reject booleans, non-finite floats, and non-integers."""
    calculator = CalculatorTools()
    payload = json.loads(calculator.calculate(action, a, b))  # type: ignore[arg-type]

    assert payload["operation"] == expected_operation
    assert "error" in payload


def test_factorial_rejects_arguments_whose_result_cannot_be_returned() -> None:
    """A factorial past 4300 digits is refused before computing it, and one below the cap still returns."""
    calculator = CalculatorTools()

    assert "error" in json.loads(calculator.calculate("factorial", 10**9))
    assert json.loads(calculator.calculate("factorial", 1558))["result"] > 0


def test_is_prime_rejects_numbers_beyond_cheap_trial_division() -> None:
    """A prime check past 10**12 is refused instead of trial-dividing for hours."""
    calculator = CalculatorTools()

    assert "error" in json.loads(calculator.calculate("is_prime", 2**89 - 1))
    assert json.loads(calculator.calculate("is_prime", 999_999_999_989))["result"] is True
