"""News agent for current events, news summaries, and article extraction."""

from agno.agent import Agent
from agno.models.base import Model
from agno.tools.duckduckgo import DuckDuckGoTools
from agno.tools.newspaper import NewspaperTools

from .base import create_agent


def create_news_agent(model: Model) -> Agent:
    """Create a news agent with web search and article extraction tools."""
    return create_agent(
        agent_name="news",
        display_name="NewsAgent",
        role="Find and summarize current news, extract articles, and provide news analysis.",
        model=model,
        tools=[
            DuckDuckGoTools(),
            NewspaperTools(),
        ],
        instructions=(
            "You are a news analyst. When providing news information:\n"
            "1. Search for the most recent and relevant news\n"
            "2. Use NewsspaperTools to extract full articles when needed\n"
            "3. Provide balanced coverage from multiple sources\n"
            "4. Distinguish between news, opinion, and analysis\n"
            "5. Include publication dates and sources\n"
            "6. Summarize key points concisely\n"
            "7. Note any potential bias in sources"
        ),
        num_history_runs=5,
    )
