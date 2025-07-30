"""Research agent for web research, fact-checking, and finding sources.

NOTE: This agent requires additional dependencies:
- pip install agno[ddg,arxiv,wikipedia]
"""

from agno.agent import Agent
from agno.models.base import Model

from .base import create_agent

try:
    from agno.tools.arxiv import ArxivTools
    from agno.tools.duckduckgo import DuckDuckGoTools
    from agno.tools.wikipedia import WikipediaTools

    TOOLS_AVAILABLE = True
except ImportError:
    TOOLS_AVAILABLE = False


def create_research_agent(model: Model) -> Agent:
    """Create a research agent with web search and knowledge base tools."""
    if not TOOLS_AVAILABLE:
        raise ImportError(
            "Research agent requires additional dependencies. Install with: pip install agno[ddg,arxiv,wikipedia]"
        )

    return create_agent(
        agent_name="research",
        display_name="ResearchAgent",
        role="Conduct thorough research using web search, academic papers, and encyclopedic knowledge.",
        model=model,
        tools=[
            DuckDuckGoTools(),
            ArxivTools(),
            WikipediaTools(),
        ],
        instructions=[
            "Start with a web search to get current information",
            "Check Wikipedia for established facts and background",
            "Search Arxiv for academic papers if the topic is scientific",
            "Synthesize information from multiple sources",
            "Always cite your sources",
            "Distinguish between facts and speculation",
        ],
        num_history_runs=5,
    )
