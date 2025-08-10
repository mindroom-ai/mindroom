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
    from agno.tools.arxiv import ArxivTools
    from agno.tools.calculator import CalculatorTools
    from agno.tools.csv_toolkit import CsvTools
    from agno.tools.docker import DockerTools
    from agno.tools.duckduckgo import DuckDuckGoTools
    from agno.tools.email import EmailTools
    from agno.tools.file import FileTools
    from agno.tools.github import GithubTools
    from agno.tools.googlesearch import GoogleSearchTools
    from agno.tools.jina import JinaReaderTools
    from agno.tools.newspaper import NewspaperTools
    from agno.tools.pandas import PandasTools
    from agno.tools.python import PythonTools
    from agno.tools.shell import ShellTools
    from agno.tools.tavily import TavilyTools
    from agno.tools.telegram import TelegramTools
    from agno.tools.website import WebsiteTools
    from agno.tools.wikipedia import WikipediaTools
    from agno.tools.yfinance import YFinanceTools
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
def calculator_tools() -> type[CalculatorTools]:
    """Return calculator tools for mathematical operations."""
    from agno.tools.calculator import CalculatorTools  # noqa: PLC0415

    return CalculatorTools


@register_tool("file")
def file_tools() -> type[FileTools]:
    """Return file tools for file system operations."""
    from agno.tools.file import FileTools  # noqa: PLC0415

    return FileTools


@register_tool("shell")
def shell_tools() -> type[ShellTools]:
    """Return shell tools for command execution."""
    from agno.tools.shell import ShellTools  # noqa: PLC0415

    return ShellTools


@register_tool("csv")
def csv_tools() -> type[CsvTools]:
    """Return CSV tools for data processing."""
    from agno.tools.csv_toolkit import CsvTools  # noqa: PLC0415

    return CsvTools


@register_tool("arxiv")
def arxiv_tools() -> type[ArxivTools]:
    """Return ArXiv tools for academic paper research."""
    from agno.tools.arxiv import ArxivTools  # noqa: PLC0415

    return ArxivTools


@register_tool("duckduckgo")
def duckduckgo_tools() -> type[DuckDuckGoTools]:
    """Return DuckDuckGo tools for web search."""
    from agno.tools.duckduckgo import DuckDuckGoTools  # noqa: PLC0415

    return DuckDuckGoTools


@register_tool("wikipedia")
def wikipedia_tools() -> type[WikipediaTools]:
    """Return Wikipedia tools for encyclopedia research."""
    from agno.tools.wikipedia import WikipediaTools  # noqa: PLC0415

    return WikipediaTools


@register_tool("newspaper")
def newspaper_tools() -> type[NewspaperTools]:
    """Return newspaper tools for article extraction."""
    from agno.tools.newspaper import NewspaperTools  # noqa: PLC0415

    return NewspaperTools


@register_tool("yfinance")
def yfinance_tools() -> type[YFinanceTools]:
    """Return Yahoo Finance tools for financial data."""
    from agno.tools.yfinance import YFinanceTools  # noqa: PLC0415

    return YFinanceTools


@register_tool("python")
def python_tools() -> type[PythonTools]:
    """Return Python tools for code execution."""
    from agno.tools.python import PythonTools  # noqa: PLC0415

    return PythonTools


@register_tool("pandas")
def pandas_tools() -> type[PandasTools]:
    """Return Pandas tools for data analysis."""
    from agno.tools.pandas import PandasTools  # noqa: PLC0415

    return PandasTools


@register_tool("docker")
def docker_tools() -> type[DockerTools]:
    """Return Docker tools for container management."""
    from agno.tools.docker import DockerTools  # noqa: PLC0415

    return DockerTools


@register_tool("github")
def github_tools() -> type[GithubTools]:
    """Return GitHub tools for repository management."""
    from agno.tools.github import GithubTools  # noqa: PLC0415

    return GithubTools


@register_tool("email")
def email_tools() -> type[EmailTools]:
    """Return email tools for message handling."""
    from agno.tools.email import EmailTools  # noqa: PLC0415

    return EmailTools


@register_tool("telegram")
def telegram_tools() -> type[TelegramTools]:
    """Return Telegram tools for messaging integration."""
    from agno.tools.telegram import TelegramTools  # noqa: PLC0415

    return TelegramTools


@register_tool("tavily")
def tavily_tools() -> type[TavilyTools]:
    """Return Tavily tools for AI-powered search."""
    from agno.tools.tavily import TavilyTools  # noqa: PLC0415

    return TavilyTools


@register_tool("googlesearch")
def googlesearch_tools() -> type[GoogleSearchTools]:
    """Return Google Search tools for web queries."""
    from agno.tools.googlesearch import GoogleSearchTools  # noqa: PLC0415

    return GoogleSearchTools


@register_tool("website")
def website_tools() -> type[WebsiteTools]:
    """Return website tools for web scraping."""
    from agno.tools.website import WebsiteTools  # noqa: PLC0415

    return WebsiteTools


@register_tool("jina")
def jina_tools() -> type[JinaReaderTools]:
    """Return Jina tools for document reading."""
    from agno.tools.jina import JinaReaderTools  # noqa: PLC0415

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
