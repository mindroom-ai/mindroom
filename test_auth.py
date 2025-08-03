#!/usr/bin/env python
"""Test Matrix authentication."""

import asyncio

import nio


async def test_auth():
    # Test server connectivity
    client = nio.AsyncClient("http://localhost:8008")

    # Try to register a new test user
    print("Testing Matrix server connectivity...")
    response = await client.register(username="test_user_123", password="test_password_123", device_name="test_device")

    if isinstance(response, nio.RegisterResponse):
        print("✅ Successfully registered test user")
        print(f"   User ID: {response.user_id}")
    elif isinstance(response, nio.ErrorResponse):
        print(f"❌ Registration failed: {response.status_code} - {response.message}")

    # Try to login with mindroom_user credentials from state file
    print("\nTesting mindroom_user login...")
    response = await client.login(
        password="mindroom_password_8a423c46f8e19de9f876167196816fe3", device_name="test_device"
    )

    if isinstance(response, nio.LoginResponse):
        print("✅ Successfully logged in as mindroom_user")
    else:
        print(f"❌ Login failed: {response}")

    await client.close()


if __name__ == "__main__":
    asyncio.run(test_auth())
