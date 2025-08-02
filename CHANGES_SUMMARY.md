# Cross-Room Agent Invites Feature - Detailed Change Summary

## Overview
This feature branch implements thread-specific agent invitations with periodic cleanup, allowing temporary agent participation in threads they're not natively assigned to. The implementation was significantly simplified after realizing agents should ONLY respond in threads, never in main room messages.

## Key Simplification
**Agents ONLY respond in threads** - This fundamental rule eliminated the need for complex room invitation logic. While Matrix requires agents to be invited to rooms for protocol compliance, conceptually agents only participate in specific threads.

## Key Features Implemented

### 1. Thread Invitation System
- **Thread Invitations**: `/invite <agent> [for <hours>]` - Invite agents to specific threads
- **Duration Support**: Optional time-limited invites
- **Uninvite Command**: `/uninvite <agent>` - Remove agents from threads
- **List Command**: `/list_invites` - Show active thread invitations
- **Matrix Compliance**: Automatically handles room invites when needed for protocol

### 2. Periodic Cleanup System
- **Automatic Background Task**: Runs every 60 seconds
- **Expired Invite Cleanup**: Removes time-limited invitations after expiration
- **Simple and Focused**: Only manages thread invitations

### 3. Response Rule Enforcement
- **Thread-Only Responses**: Agents never respond to main room messages
- **Unified Behavior**: Invited agents follow same rules as native agents
- **No Special Cases**: Removed all complex special handling

## Major Code Changes

### Files Removed
- `src/mindroom/room_invites.py` - Entire room invitation concept removed
- `src/mindroom/invites_base.py` - Over-engineered base classes removed
- `tests/test_room_invites.py` - Related tests removed

### New/Modified Files

#### `src/mindroom/thread_invites.py`
Thread-specific invitation management:
```python
- ThreadInvite: Dataclass for thread invitations
- ThreadInviteManager: Singleton manager for thread invites
- Supports time-limited invitations
```

#### `src/mindroom/commands.py`
Simplified command parsing:
```python
- Removed "to room" syntax
- Commands only work in threads
- Clear error messages when used outside threads
```

#### `src/mindroom/utils.py`
Common utility functions with key change:
```python
def should_agent_respond(...):
    # Agents ONLY respond in threads, never in main rooms
    if not is_thread:
        return ResponseDecision(False, False)
    # ... rest of logic
```

#### `src/mindroom/bot.py`
Major simplifications:
- Removed all room invite logic
- Simplified `_handle_invite_command` to only handle threads
- Cleaned up `_periodic_cleanup` to only manage thread invites
- Removed complex activity tracking

### Test Updates
- `tests/test_periodic_cleanup.py` - Rewritten to only test thread cleanup
- `tests/test_agent_response_logic.py` - Updated to expect no responses outside threads
- `tests/test_bot_helpers.py` - Removed room invite tests

## Response Rules (Enforced in Threads Only)

### The 5 Core Rules
1. **Mentioned agents always respond** - `@agent` guarantees response
2. **Single agent continues conversation** - Natural flow in 1-on-1 threads
3. **Multiple agents need direction** - Explicit mentions required with 2+ agents
4. **Smart routing for new threads** - Automatic agent selection
5. **Invited agents act like natives** - Same rules apply to all agents

## Architecture Decisions

### 1. Thread-Only Design
- Agents conceptually only exist in threads
- Matrix room invites handled transparently for protocol compliance
- Massive simplification of invitation logic

### 2. Simplified State Management
- Only track thread invitations
- No complex room-level activity tracking
- Clear expiration semantics

### 3. Cleaner Code Structure
- Removed ~500 lines of room invitation code
- Eliminated inheritance hierarchy
- More focused and maintainable

## Documentation Updates

### README.md
- Updated to clarify agents only respond in threads
- Simplified command documentation
- Removed room invitation references

### Command Help
- Clear messaging that commands only work in threads
- Simplified examples
- Better error messages

## Diff Statistics (Approximate)
- Files removed: 3
- Files significantly simplified: 5
- Net code reduction: ~500 lines
- Tests updated: 3 test files

## Benefits of Simplification

1. **Conceptual Clarity**: One simple rule - agents only respond in threads
2. **Code Simplicity**: Removed entire subsystem (room invites)
3. **Reduced Bugs**: Less code = less bugs
4. **Better UX**: Clear, predictable behavior
5. **Maintainability**: Easier to understand and modify

## Migration Notes
- No migration needed - simplified version is more restrictive
- Existing thread invitations continue to work
- Room invitations concept completely removed
