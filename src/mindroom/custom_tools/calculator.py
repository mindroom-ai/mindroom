"""Calculator toolkit whose arguments stay within cheap, returnable results."""

from __future__ import annotations

import json

from agno.tools.calculator import CalculatorTools as AgnoCalculatorTools

# The largest n whose factorial fits Python's default 4300-digit integer-to-string limit.
_MAX_FACTORIAL_ARGUMENT = 1558
# Trial division up to the square root of this bound takes about a million iterations.
_MAX_PRIME_CHECK_ARGUMENT = 10**12


class CalculatorTools(AgnoCalculatorTools):
    """Agno calculator with argument bounds, because it runs in the process shared by every agent."""

    # AGNO_COMPAT: CalculatorTools.factorial accepts arguments whose result cannot be returned.
    # Reason: Agno 3.0.9 runs math.factorial(n) for any n, which holds the GIL until it finishes, and json.dumps
    # then refuses results above 4300 digits, so one chat message can stall the primary shared by every agent.
    # Upstream issue: Tracking gap; no matching issue has been verified.
    # Upstream PR: No matching fix has been verified.
    # Remove when: Agno refuses factorial arguments whose result cannot be serialized before computing them;
    # the 1558 cap is MindRoom policy for the shared primary and stays if Agno picks another bound.
    # Coverage: tests/test_calculator_tool.py::test_factorial_rejects_arguments_whose_result_cannot_be_returned.
    def factorial(self, n: int) -> str:
        """Calculate the factorial of a number up to 1558 and return the result as a JSON string.

        Args:
            n (int): Number to calculate the factorial of, at most 1558.

        """
        if n > _MAX_FACTORIAL_ARGUMENT:
            return json.dumps(
                {"operation": "factorial", "error": f"Factorial is limited to n <= {_MAX_FACTORIAL_ARGUMENT}"},
            )
        return super().factorial(n)

    # AGNO_COMPAT: CalculatorTools.is_prime trial-divides without a bound.
    # Reason: Agno 3.0.9 trial-divides up to sqrt(n) for any n, so one large prime checks for hours in the
    # primary shared by every agent.
    # Upstream issue: Tracking gap; no matching issue has been verified.
    # Upstream PR: No matching fix has been verified.
    # Remove when: Agno bounds prime-check work, for example with a probabilistic test or an argument limit;
    # the 10**12 cap is MindRoom policy for the shared primary and stays if Agno picks another bound.
    # Coverage: tests/test_calculator_tool.py::test_is_prime_rejects_numbers_beyond_cheap_trial_division.
    def is_prime(self, n: int) -> str:
        """Check if a number up to 10**12 is prime and return the result as a JSON string.

        Args:
            n (int): Number to check if prime, at most 10**12.

        """
        if n > _MAX_PRIME_CHECK_ARGUMENT:
            return json.dumps({"operation": "prime_check", "error": "Prime checks are limited to n <= 10**12"})
        return super().is_prime(n)
