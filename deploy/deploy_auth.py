#!/usr/bin/env python3
"""Authentication configuration helper for MindRoom deployment.

This script helps generate bcrypt password hashes for the authentication system.
"""

import getpass
import sys

import bcrypt


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def main() -> None:
    """Main function to generate password hash."""
    print("MindRoom Authentication Setup")
    print("=" * 40)
    print()

    username = input("Enter username (default: admin): ").strip() or "admin"

    # Get password securely
    while True:
        password = getpass.getpass("Enter password: ")
        confirm = getpass.getpass("Confirm password: ")

        if password != confirm:
            print("Passwords don't match. Please try again.")
            continue

        if len(password) < 8:
            print("Password must be at least 8 characters long.")
            continue

        break

    # Generate hash
    password_hash = hash_password(password)

    print()
    print("Authentication Configuration")
    print("-" * 40)
    print()
    print("Add these environment variables to your .env file:")
    print()
    print("MINDROOM_AUTH_ENABLED=true")
    print(f"MINDROOM_AUTH_USERNAME={username}")
    print(f"MINDROOM_AUTH_PASSWORD_HASH={password_hash}")
    print("MINDROOM_SESSION_DURATION=86400  # 24 hours in seconds")
    print()
    print("Or export them before starting the application:")
    print()
    print("export MINDROOM_AUTH_ENABLED=true")
    print(f"export MINDROOM_AUTH_USERNAME='{username}'")
    print(f"export MINDROOM_AUTH_PASSWORD_HASH='{password_hash}'")
    print("export MINDROOM_SESSION_DURATION=86400")
    print()
    print("To disable authentication, set:")
    print("MINDROOM_AUTH_ENABLED=false")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
