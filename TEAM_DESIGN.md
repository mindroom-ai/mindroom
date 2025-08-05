# Team Formation Logic Design

## Overview
Design for integrating Agno Teams into mindroom to enable multi-agent collaboration when multiple agents are present in threads or explicitly tagged.

## Team Formation Rules

### 1. Explicit Team Formation (Multiple agents tagged)
When a user tags multiple agents in a message:
```
@research @analyst What are the market trends?
```
- Form a team with the tagged agents
- Use "coordinate" mode for sequential collaboration

### 2. Implicit Team Formation (Multiple agents in thread)
When multiple agents are already participating in a thread and no specific agent is mentioned:
```
Thread has: @code, @security
User: "How should we handle authentication?"
```
- Form a team with all agents present in thread
- Use "collaborate" mode for parallel perspectives

### 3. Router-Initiated Team Formation
When router detects a complex query requiring multiple expertise:
```
User: "Build a secure API with documentation"
```
- Router analyzes query and selects appropriate agents
- Forms team with selected agents
- Uses "coordinate" mode for structured approach

## Implementation Architecture

### 1. Team Manager Component
```python
class TeamManager:
    """Manages team formation and coordination."""

    def should_form_team(
        self,
        tagged_agents: list[str],
        agents_in_thread: list[str],
        message: str,
    ) -> tuple[bool, list[str], str]:
        """Determine if team should form and with which mode."""

    async def create_team(
        self,
        agent_names: list[str],
        mode: str,
    ) -> Team:
        """Create an Agno Team instance."""

    async def execute_team_response(
        self,
        team: Team,
        message: str,
        context: dict,
    ) -> str:
        """Execute team response and return synthesized result."""
```

### 2. Integration Points

#### A. In `bot.py`
- Detect team formation triggers in `_on_message`
- Route to team manager when conditions are met
- Handle team responses alongside individual responses

#### B. In `thread_utils.py`
- Update `should_agent_respond` to check team membership
- Add `is_part_of_team` helper function
- Prevent individual responses when team is active

#### C. In `orchestrator.py`
- Add TeamManager instance
- Provide access to all agent instances for team formation
- Coordinate team lifecycle

### 3. Team Modes Implementation

#### Coordinate Mode
- Agents work sequentially
- Each builds on previous agent's output
- Order determined by task requirements
- Example: Research → Analysis → Writing

#### Collaborate Mode
- Agents work in parallel
- All receive same input
- Outputs synthesized into unified response
- Example: Code + Security perspectives combined

#### Route Mode (Future)
- Lead agent delegates subtasks
- Agents work on assigned portions
- Results assembled by lead agent
- Example: Complex multi-step projects

### 4. Response Synthesis

For team responses, we need to:
1. Collect individual agent outputs
2. Synthesize into coherent team response
3. Maintain individual agent voices where appropriate
4. Add team-level summary/recommendations

Example synthesis:
```
Team Response:

**Research Agent**: Found 3 key trends...
**Analyst Agent**: Based on the data...

**Team Recommendation**: Combining our analysis...
```

## API Design

### Team Response Flow
```python
# In bot.py
if team_manager.should_form_team(tagged_agents, agents_in_thread, message):
    team = await team_manager.create_team(agent_names, mode)
    response = await team_manager.execute_team_response(team, message, context)
    await self._send_response(response, event)
```

### Preventing Double Responses
```python
# In thread_utils.py
def should_agent_respond(...):
    # Check if agent is part of active team
    if is_part_of_team_response(agent_name, thread_id):
        return False  # Team will handle response
    # ... existing logic
```

## Configuration

### Team Settings in config.yaml
```yaml
team_settings:
  enabled: true
  default_mode: collaborate
  synthesis_style: structured  # or narrative
  max_team_size: 5

# Predefined teams (optional)
teams:
  backend_team:
    members: [code, security, database]
    mode: coordinate
  analysis_team:
    members: [research, analyst, writer]
    mode: coordinate
```

## Error Handling

1. **Agent Unavailable**: Continue with available agents
2. **Timeout**: Set reasonable timeouts for team operations
3. **Conflicting Responses**: Use synthesis to acknowledge differences
4. **Context Overflow**: Summarize when approaching limits

## Next Steps

1. Implement TeamManager class
2. Update bot.py to detect team scenarios
3. Modify thread_utils.py for team awareness
4. Add team response synthesis
5. Test with various scenarios
