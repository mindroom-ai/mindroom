#!/usr/bin/env python
"""Manually complete OAuth callback when redirect fails."""

import sys

print("Manual OAuth Callback Completion")
print("=" * 60)
print()
print("When you see 'localhost refused to connect' after authorizing:")
print()
print("1. Copy the ENTIRE URL from your browser's address bar")
print("2. Paste it here and press Enter:")
print()

url = input("Paste URL: ").strip()

if "code=" not in url:
    print("Error: No authorization code found in URL")
    sys.exit(1)

# Extract the code
import urllib.parse
parsed = urllib.parse.urlparse(url)
params = urllib.parse.parse_qs(parsed.query)
code = params.get('code', [None])[0]

if not code:
    print("Error: Could not extract authorization code")
    sys.exit(1)

print(f"\nAuthorization code extracted: {code[:20]}...")

# Make the callback request locally
import requests

try:
    response = requests.get(
        f"http://localhost:8765/api/gmail/callback",
        params={"code": code}
    )
    
    if response.status_code == 200:
        print("\n✅ OAuth callback completed successfully!")
        print("You can now use Gmail integration.")
    else:
        print(f"\n❌ Callback failed: {response.status_code}")
        print(f"Response: {response.text}")
except Exception as e:
    print(f"\n❌ Error: {e}")
    print("\nMake sure the widget backend is running on port 8765")