# AI-Powered Team Mode Decision

## Overview

MindRoom now features intelligent team collaboration mode selection. When multiple agents form a team, the system uses AI to determine the optimal collaboration mode based on task requirements.

## How It Works

### Automatic Mode Selection

When multiple agents are tagged or participate in a conversation, the system:

1. Analyzes the user's message to understand task requirements
2. Considers the capabilities of the involved agents
3. Determines the optimal collaboration mode:
   - **Coordinate Mode**: Team leader delegates different subtasks to members and synthesizes their outputs (can be sequential OR parallel as appropriate)
   - **Collaborate Mode**: All team members work on the SAME task simultaneously, providing diverse perspectives

### Examples

#### Different Subtasks (Coordinate Mode)
```
User: @email @phone Send me the details via email, then call me to discuss
AI Decision: Coordinate mode - Different tasks: email agent sends email, phone agent makes call
```

```
User: @weather @news Get the weather forecast and latest news
AI Decision: Coordinate mode - Different tasks that the leader can delegate in parallel
```

#### Same Task for All (Collaborate Mode)
```
User: @research @analyst What do you think about this approach?
AI Decision: Collaborate mode - All agents provide their perspective on the same question
```

```
User: @designer @developer @qa Brainstorm solutions for the UI problem
AI Decision: Collaborate mode - All agents work together on the same brainstorming task
```

## Implementation Details

### Key Components

1. **TeamModeDecision Model**: Structured Pydantic model for AI responses
2. **determine_team_mode()**: Async function that prompts AI for mode decision
3. **Enhanced should_form_team()**: Now async, accepts message and config for AI analysis

### Fallback Behavior

The system maintains backward compatibility:
- If AI decision fails, falls back to original hardcoded logic
- Works without message/config parameters (uses hardcoded rules)
- Preserves existing behavior for single-agent scenarios

### Configuration

The feature is enabled by default but can be disabled:

```python
# In teams.py - should_form_team()
result = await should_form_team(
    tagged_agents=agents,
    agents_in_thread=thread_agents,
    all_mentioned_in_thread=mentioned,
    message=user_message,
    config=app_config,
    use_ai_decision=False  # Disable AI mode selection
)
```

## Testing

Comprehensive test coverage includes:
- AI decision logic for various task types
- Fallback behavior on AI failures
- Backward compatibility with existing code
- Real-world scenarios (email→call, parallel research)

Run tests with:
```bash
pytest tests/test_team_mode_decision.py -v
```

## Benefits

1. **Intelligent Coordination**: Agents work in the most efficient mode for each task
2. **Context-Aware**: Decisions based on actual task requirements
3. **Automatic**: No manual configuration needed per task
4. **Reliable**: Graceful fallback ensures system always works
5. **Extensible**: Easy to enhance decision criteria in the future

## Future Enhancements

- Consider urgency and priority in mode selection
- Add resource constraints to decision logic
- Learn from user feedback on mode effectiveness
- Support hybrid modes for complex workflows
