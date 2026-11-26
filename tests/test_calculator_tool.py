"""Native calculator operations stay accurate and bounded for the shared runtime."""

from __future__ import annotations

import json

from mindroom.custom_tools.calculator import CalculatorTools
from mindroom.tool_system.authoring import Toolkit
from mindroom.tools.calculator import calculator_tools


def test_calculator_toolkit_is_native_with_preserved_order() -> None:
    """The calculator factory returns a native Toolkit with the preserved name and function order."""
    tool_class = calculator_tools()
    calculator = tool_class()

    assert issubclass(tool_class, Toolkit)
    assert calculator.name == "calculator"
    assert list(calculator.functions) == [
        "add",
        "subtract",
        "multiply",
        "divide",
        "exponentiate",
        "factorial",
        "is_prime",
        "square_root",
    ]


def test_calculator_operations_and_error_cases() -> None:
    """Basic and advanced calculator operations return structured JSON results and domain errors."""
    calculator = CalculatorTools()

    assert json.loads(calculator.add(2, 3.5)) == {"operation": "addition", "result": 5.5}
    assert json.loads(calculator.subtract(10, 4)) == {"operation": "subtraction", "result": 6}
    assert json.loads(calculator.multiply(3, 7)) == {"operation": "multiplication", "result": 21}
    assert json.loads(calculator.divide(9, 2)) == {"operation": "division", "result": 4.5}
    assert json.loads(calculator.divide(9, 0)) == {"operation": "division", "error": "Division by zero is undefined"}
    assert json.loads(calculator.exponentiate(2, 5)) == {"operation": "exponentiation", "result": 32.0}
    assert json.loads(calculator.factorial(0)) == {"operation": "factorial", "result": 1}
    assert json.loads(calculator.factorial(5)) == {"operation": "factorial", "result": 120}
    assert json.loads(calculator.factorial(-1)) == {
        "operation": "factorial",
        "error": "Factorial of a negative number is undefined",
    }
    assert json.loads(calculator.is_prime(-3)) == {"operation": "prime_check", "result": False}
    assert json.loads(calculator.is_prime(1)) == {"operation": "prime_check", "result": False}
    assert json.loads(calculator.is_prime(2)) == {"operation": "prime_check", "result": True}
    assert json.loads(calculator.is_prime(9)) == {"operation": "prime_check", "result": False}
    assert json.loads(calculator.is_prime(17)) == {"operation": "prime_check", "result": True}
    assert json.loads(calculator.square_root(9)) == {"operation": "square_root", "result": 3.0}
    assert json.loads(calculator.square_root(-4)) == {
        "operation": "square_root",
        "error": "Square root of a negative number is undefined",
    }


def test_factorial_rejects_arguments_whose_result_cannot_be_returned() -> None:
    """A factorial past 4300 digits is refused before computing it, and one below the cap still returns."""
    calculator = CalculatorTools()

    assert "error" in json.loads(calculator.factorial(10**9))
    assert json.loads(calculator.factorial(1558))["result"] > 0


def test_is_prime_rejects_numbers_beyond_cheap_trial_division() -> None:
    """A prime check past 10**12 is refused instead of trial-dividing for hours."""
    calculator = CalculatorTools()

    assert "error" in json.loads(calculator.is_prime(2**89 - 1))
    assert json.loads(calculator.is_prime(999_999_999_989))["result"] is True
