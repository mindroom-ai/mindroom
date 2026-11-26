"""Native calculator toolkit whose arguments stay within cheap, returnable results."""

from __future__ import annotations

import json
import math

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit, tool

logger = get_logger(__name__)

# The largest n whose factorial fits Python's default 4300-digit integer-to-string limit.
_MAX_FACTORIAL_ARGUMENT = 1558
# Trial division up to the square root of this bound takes about half a million odd checks.
_MAX_PRIME_CHECK_ARGUMENT = 10**12


class CalculatorTools(Toolkit):
    """Mathematical calculator with basic and advanced operations bounded for the shared runtime."""

    name = "calculator"

    def __init__(self) -> None:
        super().__init__(
            name="calculator",
            tools=(
                self.add,
                self.subtract,
                self.multiply,
                self.divide,
                self.exponentiate,
                self.factorial,
                self.is_prime,
                self.square_root,
            ),
        )

    @tool
    def add(self, a: float, b: float) -> str:
        """Add two numbers and return the result.

        Args:
            a (float): First number.
            b (float): Second number.

        Returns:
            str: JSON string of the result.

        """
        result = a + b
        logger.debug("Adding numbers", a=a, b=b, result=result)
        return json.dumps({"operation": "addition", "result": result})

    @tool
    def subtract(self, a: float, b: float) -> str:
        """Subtract second number from first and return the result.

        Args:
            a (float): First number.
            b (float): Second number.

        Returns:
            str: JSON string of the result.

        """
        result = a - b
        logger.debug("Subtracting numbers", a=a, b=b, result=result)
        return json.dumps({"operation": "subtraction", "result": result})

    @tool
    def multiply(self, a: float, b: float) -> str:
        """Multiply two numbers and return the result.

        Args:
            a (float): First number.
            b (float): Second number.

        Returns:
            str: JSON string of the result.

        """
        result = a * b
        logger.debug("Multiplying numbers", a=a, b=b, result=result)
        return json.dumps({"operation": "multiplication", "result": result})

    @tool
    def divide(self, a: float, b: float) -> str:
        """Divide first number by second and return the result.

        Args:
            a (float): Numerator.
            b (float): Denominator.

        Returns:
            str: JSON string of the result.

        """
        if b == 0:
            logger.error("Attempt to divide by zero")
            return json.dumps({"operation": "division", "error": "Division by zero is undefined"})
        try:
            result = a / b
        except Exception as exc:
            return json.dumps({"operation": "division", "error": str(exc), "result": "Error"})
        logger.debug("Dividing numbers", a=a, b=b, result=result)
        return json.dumps({"operation": "division", "result": result})

    @tool
    def exponentiate(self, a: float, b: float) -> str:
        """Raise first number to the power of the second number and return the result.

        Args:
            a (float): Base.
            b (float): Exponent.

        Returns:
            str: JSON string of the result.

        """
        result = math.pow(a, b)
        logger.debug("Exponentiating numbers", a=a, b=b, result=result)
        return json.dumps({"operation": "exponentiation", "result": result})

    @tool
    def factorial(self, n: int) -> str:
        """Calculate the factorial of a number up to 1558 and return the result as a JSON string.

        Args:
            n (int): Number to calculate the factorial of, at most 1558.

        """
        if n < 0:
            logger.error("Attempt to calculate factorial of a negative number", n=n)
            return json.dumps({"operation": "factorial", "error": "Factorial of a negative number is undefined"})
        if n > _MAX_FACTORIAL_ARGUMENT:
            return json.dumps(
                {"operation": "factorial", "error": f"Factorial is limited to n <= {_MAX_FACTORIAL_ARGUMENT}"},
            )
        result = math.factorial(n)
        logger.debug("Calculated factorial", n=n)
        return json.dumps({"operation": "factorial", "result": result})

    @tool
    def is_prime(self, n: int) -> str:
        """Check if a number up to 10**12 is prime and return the result as a JSON string.

        Args:
            n (int): Number to check if prime, at most 10**12.

        """
        if n > _MAX_PRIME_CHECK_ARGUMENT:
            return json.dumps({"operation": "prime_check", "error": "Prime checks are limited to n <= 10**12"})
        if n <= 1:
            return json.dumps({"operation": "prime_check", "result": False})
        if n == 2:
            return json.dumps({"operation": "prime_check", "result": True})
        if n % 2 == 0:
            return json.dumps({"operation": "prime_check", "result": False})
        for divisor in range(3, math.isqrt(n) + 1, 2):
            if n % divisor == 0:
                return json.dumps({"operation": "prime_check", "result": False})
        return json.dumps({"operation": "prime_check", "result": True})

    @tool
    def square_root(self, n: float) -> str:
        """Calculate the square root of a number and return the result.

        Args:
            n (float): Number to calculate the square root of.

        Returns:
            str: JSON string of the result.

        """
        if n < 0:
            logger.error("Attempt to calculate square root of a negative number", n=n)
            return json.dumps({"operation": "square_root", "error": "Square root of a negative number is undefined"})
        result = math.sqrt(n)
        logger.debug("Calculated square root", n=n, result=result)
        return json.dumps({"operation": "square_root", "result": result})
