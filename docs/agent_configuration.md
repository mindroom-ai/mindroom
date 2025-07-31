# Agent Configuration Guide

mindroom uses a YAML-based configuration system that makes it easy to customize agents for your specific needs. You can modify existing agents or create entirely new ones by editing a simple configuration file.

## Configuration File

The default agent configuration file is `agents.yaml` in the project root. You can also specify a custom configuration file when starting the bot.

## Available Agents

mindroom comes with several pre-configured agents, each specialized for different tasks:

### Default Agents

1. **general** - A friendly conversational assistant
   - No tools required
   - Provides helpful responses to general questions
   - Maintains conversation context

2. **calculator** - Mathematical problem solver
   - Uses calculator tool for accurate computations
   - Shows step-by-step solutions
   - Explains mathematical concepts

3. **code** - Programming assistant
   - Can read, write, and modify files
   - Executes shell commands
   - Follows coding best practices

4. **shell** - System operations specialist
   - Executes shell commands safely
   - Explains what commands do before running them
   - Suggests safer alternatives when needed

5. **summary** - Text summarization expert
   - Creates concise summaries of long texts
   - Extracts key points and main ideas
   - Preserves important details

6. **research** - Information gathering specialist
   - Searches the web with DuckDuckGo
   - Queries Wikipedia for encyclopedic information
   - Searches academic papers on arXiv
   - Cross-references multiple sources

7. **finance** - Financial data analyst
   - Retrieves real-time stock market data
   - Performs financial calculations
   - Provides market analysis

8. **news** - Current events reporter
   - Searches for recent news
   - Summarizes articles from multiple sources
   - Provides balanced coverage

9. **data_analyst** - Data analysis specialist
   - Processes CSV files
   - Performs statistical analysis
   - Creates insights from data

## Agent Configuration Structure

Each agent in the YAML file follows this structure:

```yaml
agents:
  agent_name:
    display_name: "Human-readable name"
    role: "What the agent does"
    tools:
      - tool_name_1
      - tool_name_2
    instructions:
      - "Specific behavior instruction 1"
      - "Specific behavior instruction 2"
    num_history_runs: 5  # How many previous messages to remember
```

### Configuration Fields

- **agent_name**: The identifier used to call the agent (e.g., `@calculator:`)
- **display_name**: A friendly name shown in conversations
- **role**: A brief description of the agent's purpose
- **tools**: List of tools the agent can use (see Available Tools below)
- **instructions**: Specific guidelines for the agent's behavior
- **num_history_runs**: Number of previous conversation turns to include for context (default: 5)

## Available Tools

Tools give agents the ability to perform specific actions:

### Basic Tools
- **calculator** - Perform mathematical calculations
- **file** - Read, write, and manage files
- **shell** - Execute command line operations
- **python** - Run Python code snippets

### Data & Analysis Tools
- **csv** - Process and analyze CSV files
- **pandas** - Advanced data manipulation and analysis
- **yfinance** - Fetch financial market data

### Research & Information Tools
- **arxiv** - Search academic papers
- **duckduckgo** - Web search
- **googlesearch** - Google search (requires API key)
- **tavily** - AI-powered search (requires API key)
- **wikipedia** - Encyclopedia lookup
- **newspaper** - Parse and extract news articles
- **website** - Extract content from websites
- **jina** - Advanced document processing

### Development Tools
- **docker** - Manage Docker containers (requires Docker installed)
- **github** - Interact with GitHub repositories (requires token)

### Communication Tools
- **email** - Send emails (requires SMTP configuration)
- **telegram** - Send Telegram messages (requires bot token)

## Creating Custom Agents

### Example 1: Simple Helper Agent

```yaml
agents:
  helper:
    display_name: "HelpfulAssistant"
    role: "Provide friendly help and encouragement"
    tools: []
    instructions:
      - "Always be positive and encouraging"
      - "Offer specific, actionable advice"
      - "Ask clarifying questions when needed"
    num_history_runs: 3
```

### Example 2: Project Manager Agent

```yaml
agents:
  project_manager:
    display_name: "ProjectManager"
    role: "Help manage software projects"
    tools:
      - file
      - shell
      - github
    instructions:
      - "Track project tasks and milestones"
      - "Generate status reports"
      - "Help with version control"
      - "Create and update documentation"
    num_history_runs: 10
```

### Example 3: Data Science Agent

```yaml
agents:
  data_scientist:
    display_name: "DataScientist"
    role: "Analyze data and create insights"
    tools:
      - python
      - pandas
      - csv
      - calculator
    instructions:
      - "Perform statistical analysis"
      - "Create data visualizations"
      - "Clean and preprocess data"
      - "Explain findings clearly"
    num_history_runs: 5
```

### Example 4: Research Assistant

```yaml
agents:
  researcher:
    display_name: "ResearchAssistant"
    role: "Comprehensive research and fact-checking"
    tools:
      - arxiv
      - wikipedia
      - duckduckgo
      - website
      - file
    instructions:
      - "Find credible sources"
      - "Cross-reference information"
      - "Create research summaries"
      - "Track sources and citations"
    num_history_runs: 8
```

## Using Agents

To interact with an agent in your Matrix chat:

1. **Mention the agent**: `@agent_name: your message`
   - Example: `@calculator: what is 25 * 4?`
   - Example: `@research: tell me about quantum computing`

2. **Default agent**: If you mention the bot without specifying an agent, it uses the `general` agent
   - Example: `@bot: hello!` (uses general agent)

## Tool Requirements

Some tools need additional setup:

### Tools requiring API keys:
- **googlesearch** - Set up Google API credentials
- **tavily** - Get API key from Tavily
- **github** - Create a GitHub personal access token
- **telegram** - Create a Telegram bot and get token
- **email** - Configure SMTP server details

### Tools requiring software:
- **docker** - Install Docker on your system

### Tools that work immediately:
- **calculator**, **file**, **shell**, **python**, **csv**, **pandas**, **arxiv**, **duckduckgo**, **wikipedia**, **newspaper**, **website**, **jina**, **yfinance**

## Best Practices

1. **Clear Agent Roles**: Give each agent a specific, well-defined purpose
2. **Appropriate Tools**: Only include tools the agent actually needs
3. **Detailed Instructions**: Provide clear behavioral guidelines
4. **Context Management**: Set `num_history_runs` based on how much context is needed
5. **Test Your Agents**: Try different scenarios to ensure they behave as expected

## Tips for Writing Instructions

Good instructions are specific and actionable:

✅ Good: "Always cite your sources with author and publication date"
❌ Vague: "Be accurate"

✅ Good: "Explain technical concepts in simple terms"
❌ Vague: "Be helpful"

✅ Good: "Ask for clarification if the request is ambiguous"
❌ Vague: "Understand the user"

## Custom Configuration Files

You can create multiple configuration files for different purposes:

1. Create a new YAML file (e.g., `my_agents.yaml`)
2. Define your agents using the structure above
3. Use the custom file when starting mindroom

## Troubleshooting

If an agent isn't working as expected:

1. Check that all required tools are properly configured
2. Verify the YAML syntax is correct (proper indentation)
3. Ensure tool names are spelled correctly
4. Test with simpler instructions first
5. Check logs for any error messages

Remember: agents are only as good as their configuration. Take time to craft clear roles and instructions for the best results!
