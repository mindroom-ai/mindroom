---
icon: lucide/rocket
---

# Getting Started

This guide will help you set up MindRoom and create your first AI agent.

## Recommended: Full Stack Docker Compose (backend + frontend + Matrix + Element)

MindRoom depends on a Matrix homeserver plus supporting services. The easiest onboarding is the full stack Docker Compose repo, which brings everything up together.

**Prereqs:** Docker + Docker Compose.

### 1. Clone the full stack repo

```bash
git clone https://github.com/mindroom-ai/mindroom-stack
cd mindroom-stack
```

### 2. Add your API keys

```bash
cp .env.example .env
$EDITOR .env  # add at least one AI provider key
```

### 3. Start everything

```bash
docker compose up -d
```

Open:

- MindRoom UI: http://localhost:3003
- Element: http://localhost:8080
- Matrix homeserver: http://matrix.localhost:8008

## Manual Install (advanced)

Use this if you already have a Matrix homeserver and want to run MindRoom directly.

### Prerequisites

- Python 3.12 or higher
- A Matrix homeserver (or use a public one like matrix.org)
- API keys for your preferred AI provider (Anthropic, OpenAI, etc.)

### Installation

=== "uv (recommended)"

    ```bash
    uv tool install mindroom
    ```

=== "pip"

    ```bash
    pip install mindroom
    ```

=== "From source"

    ```bash
    git clone https://github.com/mindroom-ai/mindroom
    cd mindroom
    uv sync
    source .venv/bin/activate
    ```

### Configuration

#### 1. Create your config file

Create a `config.yaml` in your working directory:

```yaml
agents:
  assistant:
    display_name: Assistant
    role: A helpful AI assistant that can answer questions
    model: sonnet
    rooms: [lobby]

models:
  sonnet:
    provider: anthropic
    id: claude-sonnet-4-latest

defaults:
  num_history_runs: 5
  markdown: true

timezone: America/Los_Angeles
```

#### 2. Set up environment variables

Create a `.env` file with your credentials:

```bash
# Matrix homeserver (must allow open registration for agent accounts)
MATRIX_HOMESERVER=https://matrix.example.com

# Optional: For self-signed certificates (development)
# MATRIX_SSL_VERIFY=false

# Optional: For federation setups where server_name differs from homeserver hostname
# MATRIX_SERVER_NAME=example.com

# AI provider API keys
ANTHROPIC_API_KEY=your_anthropic_key
# OPENAI_API_KEY=your_openai_key
# GOOGLE_API_KEY=your_google_key
```

> [!NOTE]
> MindRoom automatically creates Matrix user accounts for each agent. Your Matrix homeserver must allow open registration, or you need to configure it to allow registration from localhost. If registration fails, check your homeserver's registration settings.

#### 3. Run MindRoom

```bash
mindroom run
```

MindRoom will:

1. Connect to your Matrix homeserver
2. Create Matrix users for each agent
3. Create any rooms that don't exist and join them
4. Start listening for messages

## Next Steps

- Learn about [agent configuration](configuration/agents.md)
- Explore [available tools](tools/index.md)
- Set up [teams for multi-agent collaboration](configuration/teams.md)
