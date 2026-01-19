# Examples: Parallel Agent Workflows

Real-world scenarios for spawning parallel AI coding agents, optimized for Claude 4.5 models.

> **Note on prompts**: The examples below show prompt content inline for readability. In practice, **always use `--prompt-file`** for these multi-line prompts to avoid shell quoting issues:
>
> ```bash
> # Write prompt to file, then spawn
> agent-cli dev new my-feature --agent --prompt-file .claude/spawn-prompt.md
> ```

## Prompt structure guidelines

Each prompt for a spawned agent should follow this structure:

1. **Explicit task description** - Be specific about what to implement
2. **Workflow directive** - Read files in parallel, commit incrementally, verify before completing
3. **Code exploration** - Read and understand existing code before writing
4. **Context with motivation** - Explain why patterns matter
5. **Focused scope** - Keep solutions minimal, implement only what's requested
6. **Structured report** - Write conclusions to `.claude/REPORT.md`

## Scenario 1: Multi-feature implementation

**User request**: "Implement user auth, payment processing, and email notifications"

**Strategy**: Three independent features → spawn three agents.

```bash
agent-cli dev new auth-feature --agent --prompt "Implement JWT-based user authentication.

<workflow>
- Read multiple files in parallel when exploring the codebase
- Make incremental git commits as you complete each component
- Run tests and linting before writing your final report
</workflow>

<code_exploration>
Start by reading these files (in parallel if independent):
- src/api/routes/ to understand existing endpoint patterns
- src/models/ to see how models are structured
- Any existing auth-related code to avoid duplication

Think carefully about the existing patterns before designing your implementation.
</code_exploration>

<context>
Backend is FastAPI in src/api/. This authentication system protects all user-facing endpoints, so reliability and security are critical. Follow the exact patterns in existing endpoints to maintain codebase consistency.
</context>

<requirements>
Implement these endpoints following existing route patterns:
- POST /auth/register - create new user with password hashing
- POST /auth/login - validate credentials and return JWT token
- GET /auth/me - return current user (requires valid JWT)
- Create an auth dependency for protecting other routes
- Store JWT_SECRET in environment variable
</requirements>

<scope>
Keep the implementation simple and focused. Implement only what is requested. A working, minimal implementation is better than an over-designed one. Reuse existing abstractions where possible.
</scope>

<report>
After verifying tests pass and linting is clean, write to .claude/REPORT.md:

## Summary
[2-3 sentences on what was implemented]

## Files Changed
- path/to/file.py - description of change

## Key Decisions
- Decision 1: rationale

## Testing
How to verify the implementation works

## Questions/Concerns
Any items needing review
</report>"

agent-cli dev new payment-integration --agent --prompt "Integrate Stripe payment processing.

<workflow>
- Read multiple files in parallel when exploring the codebase
- Make incremental git commits as you complete each component
- Run tests and linting before writing your final report
</workflow>

<code_exploration>
Read these files to understand the codebase (parallelize independent reads):
- src/api/routes/ for endpoint patterns and error handling
- src/models/ for existing model patterns
- Any existing payment or billing code

Only make claims about code you have actually read.
</code_exploration>

<context>
This payment integration handles real money transactions and must be implemented correctly. Stripe webhooks are essential for tracking payment status - the system cannot rely solely on client-side confirmation. Use the stripe Python package and store STRIPE_SECRET_KEY in environment.
</context>

<requirements>
- POST /payments/create-intent - create Stripe PaymentIntent, return client_secret
- POST /payments/webhook - handle Stripe webhook events (payment_intent.succeeded, payment_intent.failed)
- Add Payment model to track transaction status
- Include proper webhook signature verification for security
</requirements>

<scope>
Implement only what is specified. Focus on a working, secure implementation. Skip subscription handling, multiple payment methods, or other features unless explicitly requested.
</scope>

<report>
After verifying tests pass, write to .claude/REPORT.md:

## Summary
[What was implemented]

## Files Changed
[List with descriptions]

## Security Considerations
[How webhook verification works, secret handling]

## Testing
[How to test with Stripe test mode]

## Questions/Concerns
[Any items for review]
</report>"

agent-cli dev new email-notifications --agent --prompt "Implement email notification system.

<workflow>
- Read multiple files in parallel when exploring the codebase
- Make incremental git commits as you complete each component
- Run tests and linting before writing your final report
</workflow>

<code_exploration>
Start by reading the codebase to understand patterns:
- Examine src/api/ for how background tasks are handled
- Check existing configuration patterns for external services
- Look for template handling patterns

Understand the existing architecture before implementing.
</code_exploration>

<context>
Email notifications are user-facing and must be reliable. Background processing prevents blocking API responses. Template-based emails allow content changes without code changes.
</context>

<requirements>
- Use an appropriate email library for the stack (e.g., fastapi-mail or aiosmtplib)
- Implement as background tasks to avoid blocking API responses
- Create templates for: welcome, password_reset, order_confirmation
- POST /notifications/send-test - endpoint for testing email delivery
- Store SMTP settings (host, port, user, password) in environment
</requirements>

<scope>
Implement the minimum required for reliable email delivery. Skip notification preferences, SMS, or push notifications unless requested.
</scope>

<report>
After verifying tests pass, write to .claude/REPORT.md with summary, files changed, library choice rationale, testing instructions, and any concerns.
</report>"
```

## Scenario 2: Test-driven development

**User request**: "Add a caching layer with comprehensive tests"

**Strategy**: One agent writes tests first, another implements.

```bash
agent-cli dev new cache-tests --agent --prompt "Write comprehensive tests for a caching layer.

<task>
Create a complete test suite that drives the implementation of a caching system. The tests define the interface - write them as if the implementation already exists.
</task>

<workflow>
- Read test files in parallel to understand existing patterns
- Commit tests incrementally as you complete each test category
- Verify tests are syntactically valid before finishing
</workflow>

<code_exploration>
First, explore the codebase (parallelize these reads):
- tests/ for existing test patterns and fixtures
- conftest.py for shared fixtures
- Project testing conventions

Follow the exact testing patterns you find.
</code_exploration>

<interface_spec>
The cache system should support:
- get(key: str) -> Any | None
- set(key: str, value: Any, ttl_seconds: int | None = None) -> None
- delete(key: str) -> bool
- clear() -> None
- Support for Redis backend and in-memory fallback
</interface_spec>

<test_requirements>
Write tests in tests/test_cache.py using pytest:
- Basic get/set/delete operations
- TTL expiration (use time mocking)
- Cache miss returns None
- Backend switching/fallback behavior
- Concurrent access patterns
- Edge cases: empty keys, None values, large values
</test_requirements>

<scope>
Write only tests, not the implementation. The tests should fail initially and pass once implementation is complete. Write tests that verify behavior, not implementation details.
</scope>

<report>
When complete, write to .claude/REPORT.md:

## Test Cases
| Test Name | What It Verifies |
|-----------|------------------|
| test_xxx  | description      |

## Interface Decisions
- Why the interface is designed this way

## Edge Cases Covered
- List of edge cases and why they matter

## Implementation Suggestions
- Hints for the implementer
</report>"
```

After reviewing the tests:

```bash
agent-cli dev new cache-impl --from cache-tests --agent --prompt "Implement the caching layer to pass existing tests.

<workflow>
- Read all test files first to understand the complete interface
- Run tests frequently as you implement: pytest tests/test_cache.py -v
- Make incremental git commits after each passing test group
- Verify all tests pass before writing your report
</workflow>

<code_exploration>
CRITICAL: Read the tests completely before writing any implementation.
- Read tests/test_cache.py to understand expected behavior
- Note the exact interface the tests expect
- Identify edge cases the tests check for

The tests define the contract - implement to match them exactly.
</code_exploration>

<requirements>
Implement in src/cache.py:
- CacheBackend abstract base class
- RedisBackend implementation (use redis-py)
- MemoryBackend implementation (dict-based with TTL support)
- Cache facade that selects backend based on configuration
</requirements>

<scope>
Implement exactly what the tests require. Skip features the tests don't verify. Skip distributed caching, cache warming, or advanced features unless tests require them.
</scope>

<report>
After ALL tests pass, write to .claude/REPORT.md:

## Implementation Approach
[How the cache system works]

## Test Results
[Output of pytest run showing all tests pass]

## Deviations
[Any places where tests seemed incorrect or ambiguous]

## Performance Notes
[Any performance considerations]
</report>"
```

## Scenario 3: Large refactoring by module

**User request**: "Refactor the API to use consistent error handling"

**Strategy**: Split by module, each agent handles one area.

```bash
agent-cli dev new refactor-users-errors --agent --prompt "Refactor error handling in the users module.

<workflow>
- Read all relevant files in parallel before making any changes
- Make incremental git commits as you refactor each endpoint
- Run tests after each change to catch regressions early
- Run linting before writing your final report
</workflow>

<code_exploration>
Think carefully about the current state before making changes:
- Read ALL files in src/api/routes/users.py and related user logic
- Document the current error handling patterns you find
- Check how errors are handled in other modules for comparison
- Look for any error handling utilities that already exist

Only modify code you have read and understood.
</code_exploration>

<context>
Inconsistent error responses make API clients fragile and debugging difficult. A standard error format allows clients to handle errors programmatically and provides clear information for debugging. Logging errors with context is essential for production troubleshooting.
</context>

<target_pattern>
Use HTTPException with structured detail:
{
  \"error\": \"ERROR_CODE\",
  \"message\": \"Human readable description\",
  \"details\": {}  // optional additional context
}

Error codes for users: USER_NOT_FOUND, USER_ALREADY_EXISTS, INVALID_CREDENTIALS, EMAIL_NOT_VERIFIED, etc.

Before raising, log with context:
logger.warning(f\"User not found: {user_id}\", extra={\"user_id\": user_id})
</target_pattern>

<scope>
ONLY modify files in src/api/routes/users.py and directly related user logic. Other agents are handling other modules.
</scope>

<report>
After tests pass and linting is clean, write to .claude/REPORT.md:

## Changes Made
| File | Change Description |
|------|-------------------|
| path | what changed      |

## Error Codes Introduced
| Code | When Used | HTTP Status |
|------|-----------|-------------|

## Breaking Changes
[Any API response changes that could affect clients]

## Testing
[How to verify the changes work]
</report>"
```

## Scenario 4: Documentation and implementation in parallel

**User request**: "Add a plugin system with documentation"

**Strategy**: One agent implements, another writes docs simultaneously.

```bash
agent-cli dev new plugin-system --agent --prompt "Implement a plugin system.

<workflow>
- Read existing codebase structure in parallel before designing
- Make incremental git commits as you complete each component
- Run tests and linting before writing your final report
</workflow>

<code_exploration>
Think carefully about the architecture before implementing:
- Read the existing codebase structure to understand where plugins fit
- Check for any existing extension points or hooks
- Look at how configuration is handled
- Understand the application lifecycle

Design the plugin system to integrate naturally with existing patterns.
</code_exploration>

<requirements>
- Plugin base class with lifecycle hooks: on_load(), on_unload(), on_event(event_name, data)
- Plugin registry for discovery and management
- Auto-load plugins from plugins/ directory
- Create one example plugin demonstrating the interface
- Plugins should be able to register event handlers
</requirements>

<scope>
Implement the minimal system that allows extending functionality through plugins. Skip: plugin dependencies, versioning, hot-reloading, sandboxing, or a plugin marketplace.
</scope>

<implementation_notes>
- Use importlib for dynamic loading
- Simple dict-based event system is sufficient
- Plugins should fail gracefully without crashing the app
</implementation_notes>

<report>
After tests pass, write to .claude/REPORT.md:

## Architecture
[Diagram or description of how plugins integrate]

## Plugin Interface
\`\`\`python
class Plugin:
    # document the interface
\`\`\`

## Example Plugin
[Show the example plugin code]

## Usage
[How to create and register a plugin]
</report>"

agent-cli dev new plugin-docs --agent --prompt "Write documentation for the plugin system.

<context>
Implementation is happening in parallel in another branch. Write documentation based on a standard plugin system design. The implementation agent will adapt if needed, or you can update docs after reviewing their work.
</context>

<assumptions>
- Plugin base class with on_load, on_unload, on_event hooks
- Plugin registry pattern with auto-discovery
- Plugins loaded from plugins/ directory
- Event-based communication
</assumptions>

<deliverables>
Create these documentation files:
- docs/plugins/overview.md - What plugins are, why use them, architecture diagram
- docs/plugins/creating-plugins.md - Step-by-step tutorial with complete example
- docs/plugins/api-reference.md - Complete API documentation for Plugin class and registry

Use clear examples and explain the \"why\" not just the \"how\".
</deliverables>

<workflow>
- Commit each documentation file as you complete it
- Ensure markdown renders correctly
</workflow>

<report>
When complete, write to .claude/REPORT.md:

## Documentation Structure
[Outline of what was created]

## Assumptions Made
[What you assumed about the implementation]

## Suggestions for Implementation
[Any insights from writing docs that could improve the design]

## Open Questions
[Things that need clarification from the implementation]
</report>"
```

## Reviewing results

After agents complete their work:

```bash
# Check status of all worktrees
agent-cli dev status

# Read reports from each agent
agent-cli dev run auth-feature cat .claude/REPORT.md
agent-cli dev run payment-integration cat .claude/REPORT.md
agent-cli dev run email-notifications cat .claude/REPORT.md

# Open a worktree to review code
agent-cli dev editor auth-feature

# Run tests in a worktree
agent-cli dev run cache-impl pytest tests/test_cache.py -v

# Clean up after merging
agent-cli dev clean --merged
```

## Report format reference

All spawned agents should write to `.claude/REPORT.md` with at minimum:

```markdown
## Summary
[2-3 sentences describing what was done]

## Files Changed
- path/to/file.py - what changed and why

## Key Decisions
- Decision: rationale for the choice made

## Testing
How to verify the implementation works correctly

## Questions/Concerns
Any items that need human review or clarification
```

This consistent format makes it easy to review work from multiple agents.
