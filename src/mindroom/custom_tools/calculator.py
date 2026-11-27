"""Native calculator toolkit whose arguments stay within cheap, returnable results."""

from __future__ import annotations

import json
import math
from typing import Literal

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit, tool

logger = get_logger(__name__)

# The largest n whose factorial fits Python's default 4300-digit integer-to-string limit.
_MAX_FACTORIAL_ARGUMENT = 1558
# Trial division up to the square root of this bound takes about half a million odd checks.
_MAX_PRIME_CHECK_ARGUMENT = 10**12

_CalculateAction = Literal[
    "add",
    "subtract",
    "multiply",
    "divide",
    "exponentiate",
    "factorial",
    "is_prime",
    "square_root",
]
_BINARY_OPERATIONS: dict[str, str] = {
    "add": "addition",
    "subtract": "subtraction",
    "multiply": "multiplication",
    "divide": "division",
    "exponentiate": "exponentiation",
}


def _parse_int_operand(value: float, operation: str) -> int | str:
    """Validate that ``value`` is a finite integer (not a bool) or return a JSON error string."""
    if isinstance(value, bool):
        return json.dumps({"operation": operation, "error": "Operand 'a' must be an integer, not a boolean"})
    if isinstance(value, int):
        return value
    if not math.isfinite(value) or not value.is_integer():
        return json.dumps({"operation": operation, "error": "Operand 'a' must be a finite integer"})
    return int(value)


def _is_prime_int(n: int) -> bool:
    if n <= 1:
        return False
    if n == 2:
        return True
    if n % 2 == 0:
        return False
    return all(n % divisor != 0 for divisor in range(3, math.isqrt(n) + 1, 2))


class CalculatorTools(Toolkit):
    """Mathematical calculator with basic and advanced operations bounded for the shared runtime."""

    name = "calculator"
    offer_background_after = False
    offer_output_path = False

    def __init__(self) -> None:
        super().__init__(name="calculator", tools=(self.calculate,))

    @tool
    def calculate(self, action: _CalculateAction, a: float, b: float | None = None) -> str:
        """Evaluate a math operation and return JSON ``{"operation", "result"}`` or ``{"operation", "error"}``.

        - `add`, `subtract`, `multiply`, `divide`, `exponentiate`: Binary ops on finite `a` and `b`.
        - `factorial`: Factorial of integer `a` (0 <= a <= 1558).
        - `is_prime`: Primality check for integer `a` (a <= 10**12).
        - `square_root`: Square root of non-negative `a`.
        """
        if action in _BINARY_OPERATIONS:
            return self._calculate_binary(action, _BINARY_OPERATIONS[action], a, b)
        if action == "factorial":
            return self._calculate_factorial(a)
        if action == "is_prime":
            return self._calculate_is_prime(a)
        if action == "square_root":
            return self._calculate_square_root(a)
        return json.dumps({"operation": action, "error": f"Unsupported action: {action}"})

    @staticmethod
    def _calculate_binary(action: str, operation: str, a: float, b: float | None) -> str:
        if isinstance(a, bool) or not math.isfinite(a):
            return json.dumps({"operation": operation, "error": f"Operand 'a' must be a finite number for {action}"})
        if b is None or isinstance(b, bool) or not math.isfinite(b):
            return json.dumps({"operation": operation, "error": f"Operand 'b' must be a finite number for {action}"})
        if action == "add":
            result = a + b
        elif action == "subtract":
            result = a - b
        elif action == "multiply":
            result = a * b
        elif action == "divide":
            if b == 0:
                logger.error("Attempt to divide by zero")
                return json.dumps({"operation": operation, "error": "Division by zero is undefined"})
            try:
                result = a / b
            except Exception as exc:
                return json.dumps({"operation": operation, "error": str(exc), "result": "Error"})
        else:
            result = math.pow(a, b)
        logger.debug("Calculated binary operation", operation=operation, a=a, b=b, result=result)
        return json.dumps({"operation": operation, "result": result})

    @staticmethod
    def _calculate_factorial(a: float) -> str:
        parsed = _parse_int_operand(a, "factorial")
        if isinstance(parsed, str):
            return parsed
        if parsed < 0:
            logger.error("Attempt to calculate factorial of a negative number", n=parsed)
            return json.dumps({"operation": "factorial", "error": "Factorial of a negative number is undefined"})
        if parsed > _MAX_FACTORIAL_ARGUMENT:
            return json.dumps(
                {"operation": "factorial", "error": f"Factorial is limited to n <= {_MAX_FACTORIAL_ARGUMENT}"},
            )
        result = math.factorial(parsed)
        logger.debug("Calculated factorial", n=parsed)
        return json.dumps({"operation": "factorial", "result": result})

    @staticmethod
    def _calculate_is_prime(a: float) -> str:
        parsed = _parse_int_operand(a, "prime_check")
        if isinstance(parsed, str):
            return parsed
        if parsed > _MAX_PRIME_CHECK_ARGUMENT:
            return json.dumps({"operation": "prime_check", "error": "Prime checks are limited to n <= 10**12"})
        return json.dumps({"operation": "prime_check", "result": _is_prime_int(parsed)})

    @staticmethod
    def _calculate_square_root(a: float) -> str:
        if isinstance(a, bool) or not math.isfinite(a):
            return json.dumps({"operation": "square_root", "error": "Operand 'a' must be a finite number"})
        if a < 0:
            logger.error("Attempt to calculate square root of a negative number", n=a)
            return json.dumps({"operation": "square_root", "error": "Square root of a negative number is undefined"})
        result = math.sqrt(a)
        logger.debug("Calculated square root", n=a, result=result)
        return json.dumps({"operation": "square_root", "result": result})
