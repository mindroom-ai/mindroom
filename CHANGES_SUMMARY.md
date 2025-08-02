# Cross-Room Agent Invites Feature - Detailed Change Summary

## Overview
This feature branch implements cross-room agent invitations with periodic cleanup, allowing temporary agent participation in rooms and threads they're not natively assigned to. The implementation went through several iterations to achieve the simplest, most maintainable solution.

## Key Features Implemented

### 1. Agent Invitation System
- **Thread Invitations**: `/invite <agent>` - Invite agents to specific threads
- **Room Invitations**: `/invite <agent> to room` - Invite agents to entire rooms
- **Duration Support**: Optional `for <hours>` parameter for time-limited invites
- **Uninvite Command**: `/uninvite <agent>` - Remove agents from threads
- **List Command**: `/list_invites` - Show all active invitations

### 2. Periodic Cleanup System
- **Automatic Background Task**: Runs every 60 seconds to manage invitations
- **Inactivity Detection**: Removes room invites after 24 hours of inactivity
- **Thread Activity Awareness**: Updates room activity when agents are active in threads
- **Expired Invite Cleanup**: Removes time-limited invitations after expiration

### 3. Response Rule Simplification
- **Unified Behavior**: Invited agents now follow the same rules as native agents
- **No Special Cases**: Removed complex special handling for invited agents
- **Clear Rules**: Documented 5 simple rules that govern all agent behavior

## Major Code Changes

### New Files Created

#### `src/mindroom/invites_base.py`
Base classes for invitation management with common functionality:
```python
- BaseInvitation: Abstract base for all invitation types
- BaseInviteManager: Abstract base for invitation managers
- Provides timestamp handling, expiration logic, and thread safety
```

#### `src/mindroom/thread_invites.py`
Thread-specific invitation management:
```python
- ThreadInvitation: Dataclass for thread invitations
- ThreadInviteManager: Singleton manager for thread invites
- Supports time-limited invitations and cross-room thread access
```

#### `src/mindroom/room_invites.py`
Room-level invitation management:
```python
- RoomInvitation: Dataclass with inactivity timeout support
- RoomInviteManager: Singleton manager with activity tracking
- Integrates with Matrix room kick functionality
```

#### `src/mindroom/commands.py`
Command parsing for invitation commands:
```python
- CommandType enum with INVITE, UNINVITE, LIST_INVITES, HELP
- Command dataclass for parsed commands
- CommandParser with regex-based parsing
- Support for various command formats and parameters
```

#### `src/mindroom/utils.py`
Common utility functions extracted for DRY principle:
```python
- extract_domain_from_user_id()
- extract_username_from_user_id()
- extract_server_name_from_homeserver()
- construct_agent_user_id()
- extract_thread_info()
- check_agent_mentioned()
- create_session_id()
- has_room_access()
- should_agent_respond()
- should_route_to_agent()
```

#### Test Files
- `tests/test_thread_invites.py` - Comprehensive thread invitation tests
- `tests/test_room_invites.py` - Room invitation and activity tracking tests
- `tests/test_commands.py` - Command parsing tests
- `tests/test_periodic_cleanup.py` - Background cleanup task tests
- `tests/test_bot_helpers.py` - Tests for extracted helper functions
- `tests/test_agent_response_logic.py` - Comprehensive response rule tests

### Modified Files

#### `src/mindroom/bot.py`
Major refactoring for clarity and maintainability:

**Extracted Helper Functions**:
```python
async def _handle_invite_command(...) -> str
async def _handle_list_invites_command(...) -> str
def _is_sender_other_agent(...) -> bool
def _should_process_message(...) -> bool
```

**Refactored Methods**:
- `_on_message()` - Broken down into focused helper methods:
  - `_should_process_message()` - Validates sender
  - `_has_room_access()` - Checks permissions
  - `_try_handle_command()` - Command processing
  - `_extract_message_context()` - Context extraction
  - `_should_respond_to_message()` - Response decision
  - `_process_and_respond()` - Response generation
  - `_send_response()` - Response sending

**Added Features**:
- Command handling in `_handle_command()`
- Periodic cleanup task `_periodic_cleanup()`
- Integration with invitation managers

**Simplified Logic**:
- Removed special cases for invited agents
- Cleaner response decision flow
- Better separation of concerns

#### `src/mindroom/matrix/` modules
- Updated to use utility functions from `utils.py`
- Removed duplicate helper functions
- Better code organization

## Response Rules (Simplified)

### The 5 Core Rules
1. **Mentioned agents always respond** - `@agent` guarantees response
2. **Single agent continues conversation** - Natural flow in 1-on-1 threads
3. **Multiple agents need direction** - Explicit mentions required with 2+ agents
4. **Smart routing for new threads** - Automatic agent selection
5. **Invited agents act like natives** - Same rules apply to all agents

### Key Simplification
Previously, invited agents had special behavior (only respond when mentioned). This was removed - invited agents now participate exactly like native agents, making the system more predictable and easier to understand.

## Architecture Decisions

### 1. Singleton Pattern for Managers
- Thread and room invite managers use singleton pattern
- Ensures consistent state across the application
- Thread-safe with asyncio locks

### 2. Background Task Architecture
- Single periodic task runs every 60 seconds
- Handles both thread and room cleanup
- Checks for thread activity before kicking from rooms
- Resilient to errors (continues running)

### 3. DRY Principle Application
- Extracted 10+ common functions to `utils.py`
- Created base classes for invitations
- Reduced code duplication significantly

### 4. Functional Programming Style
- Extracted methods into standalone functions where possible
- Made internal-only functions private with underscore prefix
- Improved testability and code organization

## Testing Coverage

### Test Statistics
- Added 70+ new test cases
- Coverage improvements:
  - `bot.py`: 60% → 70%
  - `utils.py`: 69% → 76%
  - New modules: 90%+ coverage

### Test Categories
1. **Unit Tests**: Individual functions and methods
2. **Integration Tests**: Manager interactions
3. **Concurrent Tests**: Thread safety verification
4. **Edge Case Tests**: Boundary conditions
5. **Response Logic Tests**: All rule combinations

## Documentation Updates

### README.md
Added "Agent Response Rules" section explaining:
- The 5 core rules
- Benefits of the rule system
- Examples of agent behavior

### Code Documentation
- Comprehensive docstrings for all new functions
- Clear comments explaining complex logic
- Test documentation explaining coverage

## Diff Statistics
- Total lines changed: ~3,500
- Files added: 10
- Files modified: 15
- Net code addition: ~2,000 lines (including tests)

## Migration Path
No migration needed - this is a new feature. The invitation system is backward compatible with existing room configurations.

## Future Improvements (TODOs)
1. Add explicit router agent that users can @mention
2. Consider persistent storage for invitations across restarts
3. Add metrics/monitoring for invitation usage
4. Consider per-agent invitation limits

## Review Checklist
- [ ] All tests pass (`pytest`)
- [ ] Pre-commit hooks pass
- [ ] Documentation is clear
- [ ] Code follows DRY principles
- [ ] No backward compatibility concerns (per project philosophy)
- [ ] Simplified implementation without over-engineering
