#!/usr/bin/env python3
"""Test script for MindRoom Dokku Provisioner."""

import sys
import time
from typing import Any

import requests

# Configuration
BASE_URL = "http://localhost:8002"
API_URL = f"{BASE_URL}/api/v1"

# ANSI color codes
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BLUE = "\033[94m"
RESET = "\033[0m"


def print_colored(message: str, color: str = RESET):
    """Print colored message."""
    print(f"{color}{message}{RESET}")


def test_health():
    """Test health endpoints."""
    print_colored("\n=== Testing Health Endpoints ===", BLUE)

    endpoints = ["/health", "/ready", "/live"]
    for endpoint in endpoints:
        try:
            response = requests.get(f"{BASE_URL}{endpoint}")
            if response.status_code == 200:
                print_colored(f"✅ {endpoint}: {response.json()}", GREEN)
            else:
                print_colored(f"❌ {endpoint}: Status {response.status_code}", RED)
        except Exception as e:
            print_colored(f"❌ {endpoint}: {e}", RED)


def test_provision(tier: str = "starter") -> dict[str, Any]:
    """Test instance provisioning."""
    print_colored(f"\n=== Testing Provisioning ({tier} tier) ===", BLUE)

    subscription_id = f"test_{tier}_{int(time.time())}"

    payload = {
        "subscription_id": subscription_id,
        "account_id": "test_account_001",
        "tier": tier,
        "limits": {
            "memory_mb": 512 if tier == "starter" else 2048,
            "cpu_limit": 0.5 if tier == "starter" else 1.0,
            "storage_gb": 5 if tier == "starter" else 20,
            "agents": 3 if tier == "starter" else 6,
            "messages_per_day": 500 if tier == "starter" else 2000,
        },
        "enable_matrix": False,
    }

    print(f"Provisioning instance for subscription: {subscription_id}")

    try:
        response = requests.post(
            f"{API_URL}/provision",
            json=payload,
            timeout=300,  # 5 minutes timeout
        )

        if response.status_code == 200:
            data = response.json()
            print_colored("✅ Provisioning successful!", GREEN)
            print(f"  App Name: {data['app_name']}")
            print(f"  Frontend URL: {data['frontend_url']}")
            print(f"  Backend URL: {data['backend_url']}")
            print(f"  Admin Password: {data['admin_password']}")
            print(f"  Time: {data['provisioning_time_seconds']:.2f} seconds")
            return data
        print_colored(f"❌ Provisioning failed: {response.text}", RED)
        return {}
    except Exception as e:
        print_colored(f"❌ Provisioning error: {e}", RED)
        return {}


def test_status(subscription_id: str):
    """Test instance status check."""
    print_colored("\n=== Testing Status Check ===", BLUE)

    try:
        response = requests.get(f"{API_URL}/status/{subscription_id}")
        if response.status_code == 200:
            data = response.json()
            print_colored("✅ Status check successful!", GREEN)
            print(f"  Status: {data.get('status')}")
            print(f"  App: {data.get('app_name')}")
            return data
        print_colored(f"❌ Status check failed: {response.text}", RED)
    except Exception as e:
        print_colored(f"❌ Status check error: {e}", RED)


def test_update(subscription_id: str, app_name: str):
    """Test instance update."""
    print_colored("\n=== Testing Instance Update ===", BLUE)

    payload = {
        "subscription_id": subscription_id,
        "app_name": app_name,
        "limits": {
            "memory_mb": 1024,
            "cpu_limit": 0.75,
        },
    }

    try:
        response = requests.put(f"{API_URL}/update", json=payload)
        if response.status_code == 200:
            data = response.json()
            print_colored("✅ Update successful!", GREEN)
            print(f"  Message: {data['message']}")
            print(f"  Restart Required: {data['restart_required']}")
        else:
            print_colored(f"❌ Update failed: {response.text}", RED)
    except Exception as e:
        print_colored(f"❌ Update error: {e}", RED)


def test_deprovision(subscription_id: str, app_name: str):
    """Test instance deprovisioning."""
    print_colored("\n=== Testing Deprovisioning ===", BLUE)

    payload = {
        "subscription_id": subscription_id,
        "app_name": app_name,
        "backup_data": False,
    }

    try:
        response = requests.delete(f"{API_URL}/deprovision", json=payload)
        if response.status_code == 200:
            data = response.json()
            print_colored("✅ Deprovisioning successful!", GREEN)
            print(f"  Message: {data['message']}")
        else:
            print_colored(f"❌ Deprovisioning failed: {response.text}", RED)
    except Exception as e:
        print_colored(f"❌ Deprovisioning error: {e}", RED)


def test_instance_access(frontend_url: str, backend_url: str):
    """Test accessing the provisioned instance."""
    print_colored("\n=== Testing Instance Access ===", BLUE)

    # Test frontend
    try:
        response = requests.get(frontend_url, timeout=10, verify=False)
        if response.status_code in [200, 301, 302]:
            print_colored(f"✅ Frontend accessible: {frontend_url}", GREEN)
        else:
            print_colored(f"⚠️ Frontend returned status {response.status_code}", YELLOW)
    except Exception as e:
        print_colored(f"❌ Frontend not accessible: {e}", RED)

    # Test backend API
    try:
        response = requests.get(f"{backend_url}/health", timeout=10, verify=False)
        if response.status_code == 200:
            print_colored(f"✅ Backend API accessible: {backend_url}", GREEN)
        else:
            print_colored(f"⚠️ Backend API returned status {response.status_code}", YELLOW)
    except Exception as e:
        print_colored(f"❌ Backend API not accessible: {e}", RED)


def run_full_test(tier: str = "starter", cleanup: bool = True):
    """Run full provisioning test cycle."""
    print_colored(f"\n{'=' * 50}", BLUE)
    print_colored("MindRoom Dokku Provisioner Test Suite", BLUE)
    print_colored(f"{'=' * 50}", BLUE)

    # Test health
    test_health()

    # Test provisioning
    provision_data = test_provision(tier)
    if not provision_data:
        print_colored("\nProvisioning failed. Stopping tests.", RED)
        return

    subscription_id = provision_data.get("subscription_id", f"test_{tier}_{int(time.time())}")
    app_name = provision_data["app_name"]

    # Wait for instance to be ready
    print_colored("\n⏳ Waiting 10 seconds for instance to stabilize...", YELLOW)
    time.sleep(10)

    # Test status
    test_status(subscription_id)

    # Test instance access
    test_instance_access(
        provision_data["frontend_url"],
        provision_data["backend_url"],
    )

    # Test update
    test_update(subscription_id, app_name)

    # Cleanup if requested
    if cleanup:
        print_colored("\n⏳ Waiting 5 seconds before cleanup...", YELLOW)
        time.sleep(5)
        test_deprovision(subscription_id, app_name)
    else:
        print_colored(f"\n💡 Instance kept running: {app_name}", YELLOW)
        print_colored("   To cleanup manually, run:", YELLOW)
        print_colored(
            f'   curl -X DELETE {API_URL}/deprovision -H \'Content-Type: application/json\' -d \'{{"subscription_id": "{subscription_id}", "app_name": "{app_name}", "backup_data": false}}\'',
            YELLOW,
        )

    print_colored(f"\n{'=' * 50}", BLUE)
    print_colored("Test Suite Complete!", BLUE)
    print_colored(f"{'=' * 50}\n", BLUE)


if __name__ == "__main__":
    # Parse command line arguments
    tier = "starter"
    cleanup = True

    if len(sys.argv) > 1:
        tier = sys.argv[1]

    if len(sys.argv) > 2 and sys.argv[2] == "--no-cleanup":
        cleanup = False

    # Run tests
    try:
        run_full_test(tier, cleanup)
    except KeyboardInterrupt:
        print_colored("\n\nTest interrupted by user.", YELLOW)
