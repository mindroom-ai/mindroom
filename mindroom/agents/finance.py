"""Finance agent for stock market data, financial calculations, and portfolio analysis."""

from agno.agent import Agent
from agno.models.base import Model
from agno.tools.calculator import CalculatorTools
from agno.tools.yfinance import YFinanceTools

from .base import create_agent


def create_finance_agent(model: Model) -> Agent:
    """Create a finance agent with YFinance and calculator tools."""
    return create_agent(
        agent_name="finance",
        display_name="FinanceAgent",
        role="Provide financial data, stock market information, and perform financial calculations.",
        model=model,
        tools=[
            YFinanceTools(),
            CalculatorTools(),
        ],
        instructions=(
            "You are a financial analyst. When providing financial information:\n"
            "1. Always include current date/time context for market data\n"
            "2. Provide both current and historical data when relevant\n"
            "3. Calculate key financial metrics (P/E ratio, market cap, etc.)\n"
            "4. Explain financial terms in accessible language\n"
            "5. Include appropriate disclaimers about investment advice\n"
            "6. Consider market hours and trading status\n"
            "7. Provide context about market trends and volatility"
        ),
        num_history_runs=5,
    )
