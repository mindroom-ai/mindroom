---
name: agent-cli-dev
description: Spawns AI coding agents in isolated git worktrees. Use when the user asks to spawn or launch an agent, delegate a task to a separate agent, work in a separate worktree, or parallelize development across features.
---

# Parallel Development with agent-cli dev

This skill teaches you how to spawn parallel AI coding agents in isolated git worktrees using the `agent-cli dev` command.

## Installation

If `agent-cli` is not available, install it first:

```bash
# Install globally
uv tool install agent-cli

# Or run directly without installing
uvx agent-cli dev new <branch-name> --agent --prompt "..."
```

## When to spawn parallel agents

Spawn separate agents when:
- Multiple independent features/tasks can be worked on in parallel
- Tasks benefit from isolation (separate branches, no conflicts)
- Large refactoring that can be split by module/component
- Test-driven development (one agent for tests, one for implementation)

Do NOT spawn when:
- Tasks are small and sequential
- Tasks have tight dependencies requiring constant coordination
- The overhead of context switching exceeds the benefit

## Core command

For short prompts:
```bash
agent-cli dev new <branch-name> --agent --prompt "Fix the login bug"
```

For longer prompts (recommended for multi-line or complex instructions):
```bash
agent-cli dev new <branch-name> --agent --prompt-file path/to/prompt.md
```

This creates:
1. A new git worktree with its own branch
2. Runs project setup (installs dependencies)
3. Opens a new terminal tab with an AI coding agent
4. Passes your prompt to the agent

**Important**: Use `--prompt-file` for prompts longer than a single line. The `--prompt` option passes text through the shell, which can cause issues with special characters (exclamation marks, dollar signs, backticks, quotes) in ZSH and other shells. Using `--prompt-file` avoids all shell quoting issues.

## Writing effective prompts for spawned agents

Spawned agents work in isolation, so prompts must be **self-contained**. Include:

1. **Clear task description**: What to implement/fix/refactor
2. **Relevant context**: File locations, patterns to follow, constraints
3. **Report request**: Ask the agent to write conclusions to `.claude/REPORT.md`

### Using --prompt-file (recommended)

For any prompt longer than a single sentence:

1. Write the prompt to a temporary file (e.g., `.claude/spawn-prompt.md`)
2. Use `--prompt-file` to pass it to the agent
3. The file can be deleted after spawning

Example workflow:
```bash
# 1. Write prompt to file (Claude does this with the Write tool)
# 2. Spawn agent with the file
agent-cli dev new my-feature --agent --prompt-file .claude/spawn-prompt.md
# 3. Optionally clean up
rm .claude/spawn-prompt.md
```

### Prompt template

```
<Task description>

Context:
- <Key file locations>
- <Patterns to follow>
- <Constraints or requirements>

When complete, write a summary to .claude/REPORT.md including:
- What you implemented/changed
- Key decisions you made
- Any questions or concerns for review
```

## Checking spawned agent results

After spawning, you can check progress:

```bash
# List all worktrees and their status
agent-cli dev status

# Read an agent's report
agent-cli dev run <branch-name> cat .claude/REPORT.md

# Open the worktree in your editor
agent-cli dev editor <branch-name>
```

## Example: Multi-feature implementation

If asked to implement auth, payments, and notifications:

```bash
# Spawn three parallel agents
agent-cli dev new auth-feature --agent --prompt "Implement JWT authentication..."
agent-cli dev new payment-integration --agent --prompt "Add Stripe payment processing..."
agent-cli dev new email-notifications --agent --prompt "Implement email notification system..."
```

Each agent works independently in its own branch. Results can be reviewed and merged separately.

## Key options

| Option | Description |
|--------|-------------|
| `--agent` / `-a` | Start AI coding agent after creation |
| `--prompt` / `-p` | Initial prompt for the agent (short prompts only) |
| `--prompt-file` / `-P` | Read prompt from file (recommended for longer prompts) |
| `--from` / `-f` | Base branch (default: origin/main) |
| `--with-agent` | Specific agent: claude, aider, codex, gemini |
| `--agent-args` | Extra arguments for the agent |

@examples.md
