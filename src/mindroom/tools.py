"""Tools registry for all available Agno tools.

This module provides a centralized registry for all tools that can be used by agents.
Tools are registered by string name and can be instantiated dynamically when loading agents.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.tools import Toolkit

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
def calculator_tools() -> Toolkit:
    """Return calculator tools for mathematical operations."""
    from agno.tools.calculator import CalculatorTools

    return CalculatorTools


@register_tool("file")
def file_tools() -> Toolkit:
    """Return file tools for file system operations."""
    from agno.tools.file import FileTools

    return FileTools


@register_tool("shell")
def shell_tools() -> Toolkit:
    """Return shell tools for command execution."""
    from agno.tools.shell import ShellTools

    return ShellTools


@register_tool("csv")
def csv_tools() -> Toolkit:
    """Return CSV tools for data processing."""
    from agno.tools.csv_toolkit import CsvTools

    return CsvTools


@register_tool("arxiv")
def arxiv_tools() -> Toolkit:
    """Return ArXiv tools for academic paper research."""
    from agno.tools.arxiv import ArxivTools

    return ArxivTools


@register_tool("duckduckgo")
def duckduckgo_tools() -> Toolkit:
    """Return DuckDuckGo tools for web search."""
    from agno.tools.duckduckgo import DuckDuckGoTools

    return DuckDuckGoTools


@register_tool("wikipedia")
def wikipedia_tools() -> Toolkit:
    """Return Wikipedia tools for encyclopedia research."""
    from agno.tools.wikipedia import WikipediaTools

    return WikipediaTools


@register_tool("newspaper")
def newspaper_tools() -> Toolkit:
    """Return newspaper tools for article extraction."""
    from agno.tools.newspaper import NewspaperTools

    return NewspaperTools


@register_tool("yfinance")
def yfinance_tools() -> Toolkit:
    """Return Yahoo Finance tools for financial data."""
    from agno.tools.yfinance import YFinanceTools

    return YFinanceTools


@register_tool("python")
def python_tools() -> Toolkit:
    """Return Python tools for code execution."""
    from agno.tools.python import PythonTools

    return PythonTools


@register_tool("pandas")
def pandas_tools() -> Toolkit:
    """Return Pandas tools for data analysis."""
    from agno.tools.pandas import PandasTools

    return PandasTools


@register_tool("docker")
def docker_tools() -> Toolkit:
    """Return Docker tools for container management."""
    from agno.tools.docker import DockerTools

    return DockerTools


@register_tool("github")
def github_tools() -> Toolkit:
    """Return GitHub tools for repository management."""
    from agno.tools.github import GithubTools

    return GithubTools


@register_tool("email")
def email_tools() -> Toolkit:
    """Return email tools for message handling."""
    from agno.tools.email import EmailTools

    return EmailTools


@register_tool("telegram")
def telegram_tools() -> Toolkit:
    """Return Telegram tools for messaging integration."""
    from agno.tools.telegram import TelegramTools

    return TelegramTools


@register_tool("tavily")
def tavily_tools() -> Toolkit:
    """Return Tavily tools for AI-powered search."""
    from agno.tools.tavily import TavilyTools

    return TavilyTools


@register_tool("googlesearch")
def googlesearch_tools() -> Toolkit:
    """Return Google Search tools for web queries."""
    from agno.tools.googlesearch import GoogleSearchTools

    return GoogleSearchTools


@register_tool("website")
def website_tools() -> Toolkit:
    """Return website tools for web scraping."""
    from agno.tools.website import WebsiteTools

    return WebsiteTools


@register_tool("jina")
def jina_tools() -> Toolkit:
    """Return Jina tools for document reading."""
    from agno.tools.jina import JinaReaderTools

    return JinaReaderTools


def get_tool_by_name(tool_name: str) -> Toolkit:
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
        msg = f"Unknown tool: {tool_name}. Available tools: {available}"
        raise ValueError(msg)

    try:
        tool_factory = TOOL_REGISTRY[tool_name]
        tool_class = tool_factory()
        return tool_class()
    except ImportError as e:
        logger.warning(f"Could not import tool '{tool_name}': {e}")
        logger.warning(f"Make sure the required dependencies are installed for {tool_name}")
        raise
