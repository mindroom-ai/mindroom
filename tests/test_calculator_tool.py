"""Calculator arguments stay within cheap, returnable results."""

from __future__ import annotations

import json

from mindroom.custom_tools.calculator import CalculatorTools


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
