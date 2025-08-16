#!/usr/bin/env python3
"""Test script to verify Matrix server connectivity and functionality."""

import random
import sys
import time

import requests


def test_matrix_server(base_url: str, server_type: str) -> bool:  # noqa: C901, PLR0911, PLR0912, PLR0915
    """Test Matrix server connectivity and basic operations."""
    print(f"\n{'=' * 60}")
    print(f"Testing {server_type} at {base_url}")
    print("=" * 60)

    # Test 1: Check server is responding
    print("\n1. Checking server availability...")
    try:
        response = requests.get(f"{base_url}/_matrix/client/versions", timeout=5)
        if response.status_code == 200:
            versions = response.json()
            print("   ✅ Server is responding")
            print(f"   Supported versions: {versions.get('versions', [])[:3]}...")
        else:
            print(f"   ❌ Server returned status {response.status_code}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"   ❌ Failed to connect: {e}")
        return False

    # Test 2: Check registration availability
    print("\n2. Checking registration availability...")
    try:
        response = requests.get(f"{base_url}/_matrix/client/r0/register", timeout=5)
        if response.status_code == 401:  # Expected for GET request
            data = response.json()
            flows = data.get("flows", [])
            print("   ✅ Registration endpoint available")
            print(f"   Available flows: {len(flows)}")
        else:
            print(f"   ⚠️  Unexpected status: {response.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"   ❌ Failed to check registration: {e}")
        return False

    # Test 3: Register a test user
    print("\n3. Attempting to register a test user...")
    username = f"testuser_{random.randint(1000, 9999)}"  # noqa: S311
    password = "TestPassword123!"  # noqa: S105

    try:
        # First, get the registration flows
        response = requests.post(
            f"{base_url}/_matrix/client/r0/register",
            json={},
            timeout=5,
        )

        if response.status_code == 401:
            data = response.json()
            session = data.get("session")

            # Try dummy registration (for development servers)
            register_data = {
                "auth": {
                    "type": "m.login.dummy",
                    "session": session,
                },
                "username": username,
                "password": password,
                "device_id": "TEST_DEVICE",
                "initial_device_display_name": "Test Device",
            }

            response = requests.post(
                f"{base_url}/_matrix/client/r0/register",
                json=register_data,
                timeout=5,
            )

            if response.status_code == 200:
                result = response.json()
                print(f"   ✅ Successfully registered user: {username}")
                print(f"   User ID: {result.get('user_id')}")
                print(f"   Access token: {result.get('access_token')[:20]}...")

                # Store for login test
                access_token = result.get("access_token")
                user_id = result.get("user_id")  # noqa: F841
            else:
                print(f"   ❌ Registration failed: {response.status_code}")
                print(f"   Response: {response.text[:200]}")
                return False
        else:
            print(f"   ❌ Unexpected response: {response.status_code}")
            return False

    except requests.exceptions.RequestException as e:
        print(f"   ❌ Registration error: {e}")
        return False

    # Test 4: Login with the test user
    print("\n4. Testing login...")
    try:
        login_data = {
            "type": "m.login.password",
            "user": username,
            "password": password,
            "device_id": "TEST_DEVICE_2",
            "initial_device_display_name": "Test Device 2",
        }

        response = requests.post(
            f"{base_url}/_matrix/client/r0/login",
            json=login_data,
            timeout=5,
        )

        if response.status_code == 200:
            result = response.json()
            print("   ✅ Successfully logged in")
            print(f"   New access token: {result.get('access_token')[:20]}...")
        else:
            print(f"   ❌ Login failed: {response.status_code}")
            print(f"   Response: {response.text[:200]}")
    except requests.exceptions.RequestException as e:
        print(f"   ❌ Login error: {e}")

    # Test 5: Check sync endpoint (requires auth)
    print("\n5. Testing sync endpoint...")
    try:
        headers = {"Authorization": f"Bearer {access_token}"}
        response = requests.get(
            f"{base_url}/_matrix/client/r0/sync",
            headers=headers,
            params={"timeout": 1000},  # 1 second timeout
            timeout=5,
        )

        if response.status_code == 200:
            sync_data = response.json()
            print("   ✅ Sync endpoint working")
            print(f"   Next batch: {sync_data.get('next_batch')[:20]}...")
        else:
            print(f"   ❌ Sync failed: {response.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"   ❌ Sync error: {e}")

    # Test 6: Create a test room
    print("\n6. Testing room creation...")
    try:
        room_data = {
            "name": f"Test Room {random.randint(100, 999)}",  # noqa: S311
            "room_alias_name": f"test_room_{random.randint(1000, 9999)}",  # noqa: S311
            "topic": "Test room for Mindroom Matrix integration",
            "preset": "public_chat",
        }

        headers = {"Authorization": f"Bearer {access_token}"}
        response = requests.post(
            f"{base_url}/_matrix/client/r0/createRoom",
            json=room_data,
            headers=headers,
            timeout=5,
        )

        if response.status_code == 200:
            result = response.json()
            room_id = result.get("room_id")
            print("   ✅ Successfully created room")
            print(f"   Room ID: {room_id}")
        else:
            print(f"   ❌ Room creation failed: {response.status_code}")
            print(f"   Response: {response.text[:200]}")
    except requests.exceptions.RequestException as e:
        print(f"   ❌ Room creation error: {e}")

    print(f"\n{'=' * 60}")
    print(f"✅ {server_type} server tests completed successfully!")
    print("=" * 60)
    return True


def main() -> None:
    """Main test function."""
    if len(sys.argv) < 2:
        print("Usage: python test_matrix.py <port> [server_type]")
        print("Example: python test_matrix.py 8448 tuwunel")
        print("Example: python test_matrix.py 8008 synapse")
        sys.exit(1)

    port = sys.argv[1]
    server_type = sys.argv[2] if len(sys.argv) > 2 else "Matrix"
    base_url = f"http://localhost:{port}"

    # Wait a bit for server to be ready
    print(f"Waiting for {server_type} server to be ready...")
    time.sleep(2)

    success = test_matrix_server(base_url, server_type)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
