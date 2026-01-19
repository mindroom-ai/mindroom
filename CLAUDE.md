# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MindRoom - AI agents that live in Matrix and work everywhere via bridges. The project consists of:
- **Core MindRoom** (`src/mindroom/`) - AI agent orchestration system with Matrix integration
- **SaaS Platform** (`saas-platform/`) - Kubernetes-based platform for hosting MindRoom instances
  - Platform Backend (FastAPI) - API server for subscriptions, instances, SSO
  - Platform Frontend (Next.js 15) - Dashboard for managing instances
  - Instance deployment via Helm charts

## Architecture

### SaaS Platform (`saas-platform/`)
- **Platform Backend**: Modular FastAPI app with routes in `backend/routes/`
- **Platform Frontend**: Next.js 15 with centralized API client in `lib/api.ts`
- **Authentication**: SSO via HttpOnly cookies across subdomains
- **Deployment**: Kubernetes with Helm charts, dual-mode support (platform/standalone)
- **Database**: Supabase with comprehensive RLS policies

### Core MindRoom (`src/mindroom/`)
- **Agent System**: AI agents with persistent memory in Matrix
- **Matrix Integration**: Synapse server for federation
- **Multi-Platform**: Works via bridges to Slack, Discord, Telegram, etc.

## 1. Core Philosophy

- **Embrace Change, Avoid Backward Compatibility**: This project has no end-users yet. Prioritize innovation and improvement over maintaining backward compatibility.
- **Simplicity is Key**: Implement the simplest possible solution. Avoid over-engineering or generalizing features prematurely.
- **Focus on the Task**: Implement only the requested feature, without adding extras.
- **Functional Over Classes**: Prefer a functional programming style for Python over complex class hierarchies.
- **Keep it DRY**: Don't Repeat Yourself. Reuse code wherever possible.
- **Be Ruthless with Code Removal**: Aggressively remove any unused code, including functions, imports, and variables.
- **Prefer dataclasses**: Use `dataclasses` that can be typed over dictionaries for better type safety and clarity.
- Do not wrap things in try-excepts unless it's necessary. Avoid wrapping things that should not fail.
- NEVER put imports in the function, unless it is to avoid circular imports. Imports should be at the top of the file.

## 2. Workflow

### Step 1: Understand the Context

- **Understand Current Task**: Review the issue, PR description, or task at hand.
- **Explore the Codebase**: List existing files and read the `README.md` to understand the project's structure and purpose.
- **READ THE SOURCE CODE**: This library has a `.venv` folder with all the dependencies installed. So read the source code when in doubt.
- **Consult Documentation**: Review documentation capabilities! If you're unsure, never guess. Do a search online.

### Step 2: Environment & Dependencies

- **Environment Setup**: Use `uv sync --all-extras` to install all dependencies and `source .venv/bin/activate` to activate the virtual environment.
- **Adding Packages**: Use `uv add <package_name>` for new dependencies or `uv add --dev <package_name>` for development-only packages.

### SaaS Platform Commands

#### Development
```bash
# Platform Backend
cd saas-platform/platform-backend
uvicorn main:app --reload --host 0.0.0.0 --port 8765

# Platform Frontend
cd saas-platform/platform-frontend
bun install && bun run dev
```

#### Deployment
```bash
# Set kubeconfig path
export KUBECONFIG=./cluster/terraform/terraform-k8s/mindroom-k8s_kubeconfig.yaml

# Deploy platform
cd cluster/k8s/platform
helm upgrade --install platform . -f values.yaml --namespace mindroom-staging

# Deploy instance - ALWAYS use the provisioner API:
./scripts/mindroom-cli.sh provision 1

# The provisioner handles everything:
# - Creates database records
# - Manages secrets securely
# - Deploys via Helm with proper values
# - Tracks status

# Manual Helm deployment (debugging only, not for production):
# helm upgrade --install instance-1 ./k8s/instance \
#   --namespace mindroom-instances \
#   -f values-with-secrets.yaml  # Never commit this file!

# Quick redeploy of MindRoom backend (updates all instances)
cd saas-platform
./redeploy-mindroom-backend.sh

# Deploy platform frontend or backend
cd saas-platform
./deploy.sh platform-frontend  # Build, push, and deploy frontend
./deploy.sh platform-backend   # Build, push, and deploy backend

# Use the CLI helper for common operations
./scripts/mindroom-cli.sh status
./scripts/mindroom-cli.sh list
./scripts/mindroom-cli.sh logs 1
```

### Step 3: Development & Git

- **Check for Changes**: Before starting, review the latest changes from the main branch with `git diff origin/main | cat`. Make sure to use `--no-pager`, or pipe the output to `cat`.
- **Commit Frequently**: Make small, frequent commits.
- **Atomic Commits**: Ensure each commit corresponds to a tested, working state.
- **Targeted Adds**: **NEVER** use `git add .`. Always add files individually (`git add <filename>`) to prevent committing unrelated changes.

### Step 4: Testing & Quality

- **Test Before Committing**: **NEVER** claim a task is complete without running `pytest` to ensure all tests pass.
- **Run Pre-commit Hooks**: Always run `pre-commit run --all-files` before committing to enforce code style and quality.
- **Handle Linter Issues**:
  - **False Positives**: The linter may incorrectly flag issues in `pyproject.toml`; these can be ignored.
  - **Test-Related Errors**: If a pre-commit fix breaks a test (e.g., by removing an unused but necessary fixture), suppress the warning with a `# noqa: <error_code>` comment.

### Step 5: Refactoring

- **Be Proactive**: Continuously look for opportunities to refactor and improve the codebase for better organization and readability.
- **Incremental Changes**: Refactor in small, testable steps. Run tests after each change and commit on success.

### Step 6: Viewing the Widget

- **Taking Screenshots**: To view the widget without Jupyter, use `python frontend/take_screenshot.py` from the project root.
- **Manual Screenshot**: From the frontend directory, run `bun run dev` to start the development server, then run `bun run screenshot` in another terminal.
- **Screenshot Location**: Screenshots are saved to `frontend/screenshots/` with timestamps.
- **Use Cases**: This is helpful for visual verification, documentation, and sharing the widget appearance.

## 3. Critical "Don'ts"

- **DO NOT** manually edit the CLI help messages in `README.md`. They are auto-generated.
- **NEVER** use `git add .`.
- **NEVER** claim a task is done without passing all `pytest` tests.

## 4. Interacting with MindRoom Agents via Matty CLI

### Overview
Matty is a Matrix CLI client that allows you to interact with MindRoom AI agents. Use it to send messages and observe agent responses during development and testing.

### Prerequisites
```bash
# Matty is installed as a project dependency
# Activate the virtual environment
source .venv/bin/activate
# Now you can use matty directly
```

### Configuration
The Matrix credentials are already configured in the project's `.env` file. Matty will automatically use these credentials.

### Essential Commands for Agent Interaction

#### 1. List Rooms
```bash
matty rooms  # or: matty r
```

#### 2. View Messages (See Agent Responses)
```bash
matty messages "room_name" --limit 20  # or: matty m "room_name" -l 20
```

#### 3. Send Messages to Agents
```bash
# Direct message
matty send "room_name" "Hello @mindroom_assistant!"

# Multiple agent mentions
matty send "room_name" "@mindroom_research @mindroom_analyst analyze this topic"
```

#### 4. Work with Threads (Agents respond in threads)
```bash
# List threads in a room
matty threads "room_name"

# View thread messages (where agents typically respond)
matty thread "room_name" t1  # View thread with ID t1

# Start a thread (agents will respond here)
matty thread-start "room_name" m2 "Starting discussion with agents"

# Reply in thread
matty thread-reply "room_name" t1 "@mindroom_assistant continue"
```

### Typical Agent Testing Workflow
```bash
# 1. Find the test room
matty rooms

# 2. Send a message mentioning agents
matty send "test_room" "@mindroom_assistant What can you do?"

# 3. Check for agent response (agents respond in threads)
matty threads "test_room"
matty thread "test_room" t1  # View the thread where agent responded

# 4. Continue conversation in thread
matty thread-reply "test_room" t1 "@mindroom_research find information about X"
```

### Important Notes
- **Agents respond in threads**: Always check threads after sending messages
- **Use @mentions**: Tag agents with @ to get their attention
- **Message handles**: Use m1, m2, m3 to reference messages
- **Thread IDs**: Use t1, t2, t3 to reference threads (persistent across sessions)
- **Output formats**: Add `--format json` for machine-readable output
- **Streaming responses**: If you see "⋯" in agent messages, they're still typing. Agents stream responses by editing messages, which may take 10+ seconds to complete. Re-check the thread after waiting.

# Important Instruction Reminders
Do what has been asked; nothing more, nothing less.
NEVER create files unless they're absolutely necessary for achieving your goal.
ALWAYS prefer editing an existing file to creating a new one.
NEVER proactively create documentation files (*.md) or README files. Only create documentation files if explicitly requested by the User.
