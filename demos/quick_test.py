#!/usr/bin/env python3
"""Quick test script for AI to verify router agent functionality."""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_router_imports():
    """Test if router agent modules can be imported."""
    try:
        from src.mindroom.router_agent import RouterAgent  # noqa: F401
        from src.mindroom.thread_utils import get_agents_in_thread  # noqa: F401

        print("✅ Router agent imports successful")
        return True
    except ImportError as e:
        print(f"❌ Import error: {e}")
        return False


def test_router_functionality():
    """Test basic router functionality."""
    try:
        from src.mindroom.router_agent import should_router_handle
        from src.mindroom.thread_utils import extract_agent_name

        # Test agent name extraction
        test_cases = [
            ("@mindroom_calculator:localhost", "calculator"),
            ("@mindroom_user_123:localhost", None),
            ("@regular_user:localhost", None),
        ]

        all_passed = True
        for input_val, expected in test_cases:
            result = extract_agent_name(input_val)
            if result != expected:
                print(f"❌ Agent extraction failed: {input_val} -> {result} (expected {expected})")
                all_passed = False

        if all_passed:
            print("✅ Agent name extraction tests passed")

        # Test router decision logic
        tests = [
            (["calculator"], ["calculator", "general"], True, False),  # Agent mentioned
            ([], ["calculator"], True, False),  # Single agent in thread
            ([], ["calculator", "general"], True, True),  # Multiple agents, none mentioned
            ([], [], False, False),  # Not in thread
        ]

        for mentioned, in_thread, is_thread, expected in tests:
            result = should_router_handle(mentioned, in_thread, is_thread)
            if result != expected:
                print(f"❌ Router logic failed: {mentioned}, {in_thread}, {is_thread} -> {result}")
                all_passed = False

        if all_passed:
            print("✅ Router decision logic tests passed")

        return all_passed

    except Exception as e:
        print(f"❌ Error in functionality test: {e}")
        return False


def check_configuration():
    """Check if router is configured in agents.yaml."""
    try:
        import yaml

        agents_path = Path(__file__).parent.parent / "agents.yaml"
        if not agents_path.exists():
            print("❌ agents.yaml not found")
            return False

        with open(agents_path) as f:
            config = yaml.safe_load(f)

        if "agents" in config and "router" in config["agents"]:
            print("✅ Router agent configured in agents.yaml")
            return True
        else:
            print("❌ Router agent not found in agents.yaml")
            return False

    except Exception as e:
        print(f"❌ Error checking configuration: {e}")
        return False


def main():
    """Run all tests."""
    print("🤖 Router Agent Quick Test")
    print("=" * 40)

    imports_ok = test_router_imports()
    config_ok = check_configuration()
    functionality_ok = test_router_functionality() if imports_ok else False

    print("\n" + "=" * 40)

    if imports_ok and config_ok and functionality_ok:
        print("✅ All tests passed! Router agent is ready.")
        return 0
    else:
        print("❌ Some tests failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
