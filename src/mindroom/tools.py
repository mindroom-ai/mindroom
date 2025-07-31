"""Tools registry for all available Agno tools.

This module provides a centralized registry for all tools that can be used by agents.
Tools are registered by string name and can be instantiated dynamically when loading agents.
"""

from collections.abc import Callable
from typing import Any

from agno.tools import Toolkit
from loguru import logger

# Registry mapping tool names to their factory functions
TOOL_REGISTRY: dict[str, Callable[[], type[Toolkit]]] = {}


def register_tool(name: str) -> Callable[[Callable[[], type[Toolkit]]], Callable[[], type[Toolkit]]]:
    """Decorator to register a tool factory function.

    Args:
        name: The name to register the tool under

    Returns:
        Decorator function
    """

    def decorator(func: Callable[[], type[Toolkit]]) -> Callable[[], type[Toolkit]]:
        TOOL_REGISTRY[name] = func
        return func

    return decorator


# Register all available tools
@register_tool("calculator")
def calculator_tools():
    from agno.tools.calculator import CalculatorTools

    return CalculatorTools


@register_tool("file")
def file_tools():
    from agno.tools.file import FileTools

    return FileTools


@register_tool("shell")
def shell_tools():
    from agno.tools.shell import ShellTools

    return ShellTools


@register_tool("csv")
def csv_tools():
    from agno.tools.csv_toolkit import CsvTools

    return CsvTools


@register_tool("arxiv")
def arxiv_tools():
    from agno.tools.arxiv import ArxivTools

    return ArxivTools


@register_tool("duckduckgo")
def duckduckgo_tools():
    from agno.tools.duckduckgo import DuckDuckGoTools

    return DuckDuckGoTools


@register_tool("wikipedia")
def wikipedia_tools():
    from agno.tools.wikipedia import WikipediaTools

    return WikipediaTools


@register_tool("newspaper")
def newspaper_tools():
    from agno.tools.newspaper import NewspaperTools

    return NewspaperTools


@register_tool("yfinance")
def yfinance_tools():
    from agno.tools.yfinance import YFinanceTools

    return YFinanceTools


@register_tool("python")
def python_tools():
    from agno.tools.python import PythonTools

    return PythonTools


@register_tool("pandas")
def pandas_tools():
    from agno.tools.pandas import PandasTools

    return PandasTools


@register_tool("docker")
def docker_tools():
    from agno.tools.docker import DockerTools

    return DockerTools


@register_tool("github")
def github_tools():
    from agno.tools.github import GithubTools

    return GithubTools


@register_tool("email")
def email_tools():
    from agno.tools.email import EmailTools

    return EmailTools


@register_tool("telegram")
def telegram_tools():
    from agno.tools.telegram import TelegramTools

    return TelegramTools


@register_tool("tavily")
def tavily_tools():
    from agno.tools.tavily import TavilyTools

    return TavilyTools


@register_tool("googlesearch")
def googlesearch_tools():
    from agno.tools.googlesearch import GoogleSearchTools

    return GoogleSearchTools


@register_tool("website")
def website_tools():
    from agno.tools.website import WebsiteTools

    return WebsiteTools


@register_tool("jina")
def jina_tools():
    from agno.tools.jina import JinaReaderTools

    return JinaReaderTools


def get_tool_by_name(tool_name: str) -> Any:
    """Get a tool instance by its registered name.

    Args:
        tool_name: The registered name of the tool

    Returns:
        An instance of the requested tool

    Raises:
        ValueError: If the tool name is not registered
    """
    if tool_name not in TOOL_REGISTRY:
        available = ", ".join(sorted(TOOL_REGISTRY.keys()))
        raise ValueError(f"Unknown tool: {tool_name}. Available tools: {available}")

    try:
        tool_factory = TOOL_REGISTRY[tool_name]
        tool_class = tool_factory()
        return tool_class()
    except ImportError as e:
        logger.warning(f"Could not import tool '{tool_name}': {e}")
        logger.warning(f"Make sure the required dependencies are installed for {tool_name}")
        raise


def list_tools() -> list[str]:
    """Get a list of all registered tool names."""
    return sorted(TOOL_REGISTRY.keys())
