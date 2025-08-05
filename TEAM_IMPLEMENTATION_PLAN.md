# Team Collaboration Implementation Plan

## Overview
Implement team-based agent collaboration using Agno Teams framework for mindroom, allowing multiple agents to coordinate responses when present in threads.

## Requirements
1. When multiple agents are tagged in a message, they form an explicit team
2. When multiple agents are already in a thread and no specific agent is tagged, they form an automatic team
3. Teams should provide coordinated, synthesized responses
4. Single agent behavior remains unchanged
5. Router can form teams for complex queries

## Implementation Strategy

### Phase 1: Core Team Infrastructure
1. Create Team abstraction using Agno Teams
2. Implement team formation logic
3. Add team modes (coordinate, collaborate, route)

### Phase 2: Integration with Existing Logic
1. Update `should_agent_respond` logic to support teams
2. Modify bot message handling to detect team scenarios
3. Implement team response coordination

### Phase 3: Testing & Validation
1. Unit tests for team formation
2. Integration tests for team responses
3. End-to-end tests for full workflow
4. Regression tests to ensure single agent behavior unchanged

## Technical Design

### Team Formation Rules
```python
def should_form_team(tagged_agents, agents_in_thread, message):
    # Case 1: Multiple agents explicitly tagged
    if len(tagged_agents) > 1:
        return True, tagged_agents, "coordinate"

    # Case 2: No agents tagged but multiple in thread
    if len(tagged_agents) == 0 and len(agents_in_thread) > 1:
        return True, agents_in_thread, "collaborate"

    # Case 3: Router decides complex query needs team
    if len(tagged_agents) == 0 and needs_multiple_expertise(message):
        return True, select_team_members(message), "coordinate"

    return False, [], ""
```

### Team Response Flow
1. Detect team formation trigger
2. Create Team instance with selected agents
3. Execute team mode (coordinate/collaborate/route)
4. Synthesize responses
5. Send unified team response

## Files to Modify
- `src/mindroom/bot.py` - Add team detection and handling
- `src/mindroom/thread_utils.py` - Update response logic
- `src/mindroom/orchestrator.py` - Add team management
- `src/mindroom/teams.py` (new) - Team implementation
- Tests for all modified components

## Success Criteria
- Multiple tagged agents collaborate successfully
- Multiple agents in thread auto-collaborate
- Single agent threads work unchanged
- All existing tests pass
- New team-specific tests pass
- End-to-end test demonstrates working teams
