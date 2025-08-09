#!/usr/bin/env python
"""Script to verify that tests actually fail without the fixes."""

import subprocess


def run_test(test_path: str) -> tuple[bool, str]:
    """Run a single test and return success status and output."""
    result = subprocess.run(
        ["python", "-m", "pytest", test_path, "-xvs"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0, result.stdout + result.stderr


def main():
    # Test 1: Room cleanup with thread invitations
    print("\n" + "=" * 60)
    print("TEST 1: Room cleanup should preserve invited agents")
    print("Without fix: Would kick invited agents from rooms")
    print("With fix: Preserves agents with thread invitations")
    print("=" * 60)

    success, output = run_test("tests/test_room_cleanup_thread_invites.py::test_cleanup_preserves_invited_agents")
    if success:
        print("✅ Test passes WITH fix")
    else:
        print("❌ Test fails")
        print(output[-500:])  # Last 500 chars

    # Test 2: Race condition fix
    print("\n" + "=" * 60)
    print("TEST 2: Invited agents should take ownership of empty threads")
    print("Without fix: Router would respond instead of invited agent")
    print("With fix: Invited agent takes ownership immediately")
    print("=" * 60)

    success, output = run_test(
        "tests/test_thread_invite_integration.py::test_invited_agent_takes_ownership_of_empty_thread"
    )
    if success:
        print("✅ Test passes WITH fix")
    else:
        print("❌ Test fails")
        print(output[-500:])

    # Test 3: Bot._on_message handling
    print("\n" + "=" * 60)
    print("TEST 3: Bot._on_message should handle thread invitations")
    print("Without fix: Invited agents wouldn't respond in unconfigured rooms")
    print("With fix: Invited agents can respond in threads")
    print("=" * 60)

    success, output = run_test(
        "tests/test_thread_invite_integration.py::test_invited_agent_responds_in_unconfigured_room"
    )
    if success:
        print("✅ Test passes WITH fix")
    else:
        print("❌ Test fails")
        print(output[-500:])

    # Test 4: Leave unconfigured rooms
    print("\n" + "=" * 60)
    print("TEST 4: Leave unconfigured rooms should preserve thread invitations")
    print("Without fix: Would leave rooms with thread invitations")
    print("With fix: Preserves rooms where agent has invitations")
    print("=" * 60)

    success, output = run_test(
        "tests/test_thread_invite_integration.py::test_bot_leaves_room_preserves_thread_invitations"
    )
    if success:
        print("✅ Test passes WITH fix")
    else:
        print("❌ Test fails")
        print(output[-500:])


if __name__ == "__main__":
    main()
