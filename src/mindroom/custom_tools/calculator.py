"""Calculator toolkit whose arguments stay within cheap, returnable results."""

from __future__ import annotations

import json

from agno.tools.calculator import CalculatorTools as AgnoCalculatorTools

# The largest n whose factorial fits Python's default 4300-digit integer-to-string limit.
# Larger results could never be returned, and math.factorial holds the GIL until it finishes.
_MAX_FACTORIAL_ARGUMENT = 1558
# Trial division up to the square root of this bound takes about a million iterations.
_MAX_PRIME_CHECK_ARGUMENT = 10**12


class CalculatorTools(AgnoCalculatorTools):
    """Agno calculator with argument bounds, because it runs in the process shared by every agent."""

    def factorial(self, n: int) -> str:
        """Calculate the factorial of a number up to 1558 and return the result.

        Args:
            n (int): Number to calculate the factorial of, at most 1558.

        Returns:
            str: JSON string of the result.

        """
        if n > _MAX_FACTORIAL_ARGUMENT:
            return json.dumps(
                {"operation": "factorial", "error": f"Factorial is limited to n <= {_MAX_FACTORIAL_ARGUMENT}"},
            )
        return super().factorial(n)

    def is_prime(self, n: int) -> str:
        """Check if a number up to 10**12 is prime and return the result.

        Args:
            n (int): Number to check if prime, at most 10**12.

        Returns:
            str: JSON string of the result.

        """
        if n > _MAX_PRIME_CHECK_ARGUMENT:
            return json.dumps(
                {"operation": "prime_check", "error": "Prime checks are limited to n <= 10**12"},
            )
        return super().is_prime(n)
