#!/usr/bin/env python
"""Authenticate Gmail for agno tools with correct scopes."""

import os
from pathlib import Path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Agno's required scopes
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
]

def authenticate():
    """Authenticate and save token for agno Gmail tools."""
    creds = None
    token_file = Path("token.json")
    
    # Check if token already exists
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # Create flow from environment variables
            client_config = {
                "installed": {
                    "client_id": os.getenv("GOOGLE_CLIENT_ID"),
                    "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
                    "project_id": os.getenv("GOOGLE_PROJECT_ID"),
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                    "redirect_uris": ["http://localhost:8080"],
                }
            }
            
            flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
            creds = flow.run_local_server(port=8080)
        
        # Save the credentials for the next run
        token_file.write_text(creds.to_json())
        print(f"✅ Token saved to {token_file}")
    else:
        print(f"✅ Using existing token from {token_file}")
    
    return creds

if __name__ == "__main__":
    print("Gmail Authentication for Agno Tools")
    print("=" * 60)
    print("This will authenticate with the following scopes:")
    for scope in SCOPES:
        print(f"  - {scope}")
    print()
    
    try:
        creds = authenticate()
        print("\n✅ Authentication successful!")
        print("You can now use the Gmail agent.")
    except Exception as e:
        print(f"\n❌ Authentication failed: {e}")
        print("\nMake sure you have set the following environment variables:")
        print("  - GOOGLE_CLIENT_ID")
        print("  - GOOGLE_CLIENT_SECRET")
        print("  - GOOGLE_PROJECT_ID")