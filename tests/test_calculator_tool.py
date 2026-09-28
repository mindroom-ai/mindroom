"""Tests for the bounded calculator toolkit."""

from __future__ import annotations

import json
import math

from mindroom.tools.calculator import calculator_tools


def test_factorial_rejects_arguments_whose_result_cannot_be_returned() -> None:
    """Huge factorials are refused before math.factorial holds the GIL for minutes."""
    tools = calculator_tools()()

    assert json.loads(tools.factorial(100_000)) == {
        "operation": "factorial",
        "error": "Factorial is limited to n <= 1558",
    }
    assert json.loads(tools.factorial(1558))["result"] == math.factorial(1558)
    assert json.loads(tools.factorial(6)) == {"operation": "factorial", "result": 720}
    assert "error" in json.loads(tools.factorial(-1))


def test_is_prime_rejects_numbers_beyond_cheap_trial_division() -> None:
    """Prime checks stay within about a million trial divisions."""
    tools = calculator_tools()()

    assert json.loads(tools.is_prime(1_000_000_000_039)) == {
        "operation": "prime_check",
        "error": "Prime checks are limited to n <= 10**12",
    }
    assert json.loads(tools.is_prime(999_999_000_001)) == {"operation": "prime_check", "result": True}
    assert json.loads(tools.is_prime(97)) == {"operation": "prime_check", "result": True}
    assert json.loads(tools.is_prime(1)) == {"operation": "prime_check", "result": False}


def test_calculator_registers_bounded_functions() -> None:
    """The toolkit exposes the bounded methods under the upstream function names."""
    tools = calculator_tools()()

    assert set(tools.functions) == {
        "add",
        "divide",
        "exponentiate",
        "factorial",
        "is_prime",
        "multiply",
        "square_root",
        "subtract",
    }
    assert json.loads(tools.functions["factorial"].entrypoint(n=100_000))["error"]
