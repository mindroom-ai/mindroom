---
description: Initialize MindRoom project understanding
argument-hint: [feature description]
allowed-tools: Bash(git diff:*), Bash(git status:*), Bash(git branch:*)
---

# Initialize MindRoom Understanding

## Your Task

$ARGUMENTS

**Instructions**:
- If a feature description was provided above, initialize your understanding of MindRoom and then start implementing that feature
- If no feature description was provided (empty above), just initialize your understanding and wait for further instructions
- Either way, first understand the current development context and project structure below

## Current Development Context
- Current branch: !`git branch --show-current`
- Changes from main: !`git diff origin/main --stat`
- Full diff: !`git diff origin/main`

**Note**: If there are no changes from main, we're starting fresh. Otherwise, understand the current feature being developed from the diff above.

## Important: Speech-to-Text Usage
**The user often dictates using speech-to-text**, so expect:
- Typos and incorrect words
- Imperfect sentence structure
- Homophone substitutions (e.g., "to/too/two", "there/their")
- Missing punctuation or capitalization

Focus on understanding the intent rather than the exact wording. When in doubt, clarify rather than assume.

## Project Understanding

Read and understand the MindRoom project structure:

1. **Read Core Documentation**
   - Full README.md for project purpose and architecture
   - CLAUDE.md for development guidelines and workflow

2. **Code Structure**
   - Main bot: `src/mindroom/bot.py`
   - Matrix interactions: `src/mindroom/matrix/` module
     - `matrix/client.py` - Core Matrix client functions
     - `matrix/mentions.py` - Mention handling
     - `matrix/message_builder.py` - Message construction
     - `matrix/identity.py` - Matrix ID handling
     - `matrix/rooms.py` - Room management
     - `matrix/state.py` - State management
     - `matrix/users.py` - User management

3. **Development Best Practices**
   - **ALWAYS check for existing helper functions** before implementing new ones
   - Look in `matrix/` module for Matrix utilities
   - Check `thread_utils.py` for thread handling
   - Review `commands.py` for command patterns
   - Use **uv** (not conda) for package management
   - Install: `uv sync --all-extras`
   - Activate: `source .venv/bin/activate`
   - Add packages: `uv add <package>` or `uv add --dev <package>`
   - **Critical Git practices**:
     - NEVER use `git add .` - always add files individually
     - Always run `pytest` before claiming completion
     - Run `pre-commit run --all-files` before commits
   - **Code style**:
     - Prefer functional style over classes
     - Use dataclasses over dictionaries
     - Keep it DRY - aggressively remove unused code
     - Don't wrap in try-except unless necessary
     - Imports at top of file (except for circular imports)

4. **Architecture**
   - Multi-agent system with separate Matrix accounts
   - Agents respond in threads, not main room
   - Mindroom's custom commands in chat are prefixed with "!" (e.g., "!help", "!schedule")
   - Asyncio for concurrent operations
   - Per-thread memory and conversation tracking
   - Dual memory system: agent memory + room memory
   - 80+ tool integrations available in `src/mindroom/tools/`

5. **Key Files and Modules**
   - `src/mindroom/bot.py` - Main bot orchestration
   - `src/mindroom/agents.py` - Agent creation and management
   - `src/mindroom/routing.py` - Agent routing logic
   - `src/mindroom/thread_invites.py` - Thread invitation system
   - `src/mindroom/scheduling.py` - Task scheduling
   - `src/mindroom/background_tasks.py` - Background task management
   - `src/mindroom/memory/` - Memory persistence system
   - `src/mindroom/tools/` - 80+ tool integrations
   - `src/mindroom/custom_tools/` - Custom tool implementations

6. **Testing with Matty CLI**
   - Matty is pre-installed in the project
   - Use `matty` commands to interact with agents during testing
   - Agents respond in threads - always check threads after sending messages
   - Use @mentions to get agent attention
   - See CLAUDE.md section 4 for detailed Matty usage
