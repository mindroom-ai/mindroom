# Mindroom Router Agent Demos

This folder contains demonstration scripts to test the router agent functionality in the multi-agent system.

## Quick Start for AI Inspection

1. **Verify system is ready**:
   ```bash
   cd demos
   python quick_test.py
   ```

2. **Run router tests**:
   ```bash
   python test_router.py                    # Unit tests
   python test_router_structured.py        # Structured output tests
   ```

3. **Test with real messages** (requires running mindroom):
   ```bash
   python demo_router_test.py
   # Enter room ID when prompted (get from mindroom logs)
   ```

## What to Expect

The router agent should:
- **Activate** when multiple agents are in a thread and no agent is mentioned
- **Stay silent** when an agent is explicitly mentioned or only one agent is in thread
- **Route intelligently** by analyzing message content and suggesting appropriate agents
- **Use structured output** with confidence scores and reasoning

## Key Files

- `test_router.py` - Unit tests for router components
- `test_router_structured.py` - Tests for structured output functionality
- `demo_router_test.py` - Real Matrix message testing
- `quick_test.py` - System verification script

## Expected Router Behavior

```
🚦 Router: Analyzing message for routing: "What about compound interest..."
🚦 Router: Routing to calculator (confidence: 0.85)
🚦 Router: Sent routing message
🔍 calculator: Responding to router mention...
```

For detailed setup instructions, see the main project README.
