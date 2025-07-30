"""Data analyst agent for data analysis, CSV/JSON manipulation, and statistics."""

from agno.agent import Agent
from agno.models.base import Model
from agno.tools.calculator import CalculatorTools
from agno.tools.csv_toolkit import CsvTools

from .base import create_agent


def create_data_analyst_agent(model: Model) -> Agent:
    """Create a data analyst agent with CSV and calculator tools."""
    return create_agent(
        agent_name="analyst",
        display_name="DataAnalystAgent",
        role="Analyze data from CSV files, perform calculations, and provide insights.",
        model=model,
        tools=[
            CsvTools(),
            CalculatorTools(),
        ],
        instructions=(
            "You are a data analyst. When analyzing data:\n"
            "1. Start by understanding the data structure\n"
            "2. Check for data quality issues (missing values, outliers)\n"
            "3. Perform appropriate statistical analysis\n"
            "4. Use visualizations when helpful (describe them clearly)\n"
            "5. Provide clear insights and recommendations\n"
            "6. Support conclusions with specific data points\n"
            "7. Consider limitations and potential biases in the data"
        ),
        num_history_runs=5,
    )
