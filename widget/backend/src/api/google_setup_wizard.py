"""Google Cloud Setup Wizard - Automates OAuth credential creation."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/setup/google", tags=["google-setup"])


class SetupStatus(BaseModel):
    """Setup status response."""

    step: str
    completed: bool
    message: str
    next_action: str | None = None
    credentials: dict | None = None


class SetupRequest(BaseModel):
    """Setup request."""

    project_name: str = "mindroom-integration"
    skip_browser: bool = False


def run_command(cmd: list[str]) -> tuple[bool, str]:
    """Run a shell command and return success status and output."""
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=30)
        return result.returncode == 0, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return False, "Command timed out"
    except Exception as e:
        return False, str(e)


def check_gcloud_installed() -> bool:
    """Check if gcloud CLI is installed."""
    success, _ = run_command(["which", "gcloud"])
    return success


def is_gcloud_authenticated() -> bool:
    """Check if user is authenticated with gcloud."""
    success, output = run_command(["gcloud", "auth", "list", "--format=json"])
    if success:
        try:
            accounts = json.loads(output)
            return len(accounts) > 0
        except Exception:  # noqa: S110
            pass
    return False


@router.get("/check-prerequisites")
async def check_prerequisites() -> dict[str, Any]:
    """Check if all prerequisites are installed."""
    checks = {
        "gcloud_installed": check_gcloud_installed(),
        "gcloud_authenticated": is_gcloud_authenticated() if check_gcloud_installed() else False,
        "can_automate": False,  # Google doesn't allow full automation
    }

    if not checks["gcloud_installed"]:
        return SetupStatus(
            step="prerequisites",
            completed=False,
            message="Google Cloud CLI not installed",
            next_action="Install gcloud from https://cloud.google.com/sdk/docs/install",
        )

    if not checks["gcloud_authenticated"]:
        return SetupStatus(
            step="authentication",
            completed=False,
            message="Not authenticated with Google Cloud",
            next_action="Run: gcloud auth login",
        )

    return SetupStatus(
        step="prerequisites",
        completed=True,
        message="Prerequisites checked",
        next_action="Ready to create project",
    )


@router.post("/create-project")
async def create_project(request: SetupRequest) -> dict[str, Any]:
    """Create a new Google Cloud project."""
    project_id = request.project_name.lower().replace(" ", "-")

    # Create project
    success, output = run_command(
        ["gcloud", "projects", "create", project_id, "--name", request.project_name, "--format=json"],
    )

    if not success:
        if "already exists" in output:
            return SetupStatus(
                step="create_project",
                completed=True,
                message=f"Project {project_id} already exists",
                next_action="Enable APIs",
            )
        return SetupStatus(
            step="create_project",
            completed=False,
            message=f"Failed to create project: {output}",
        )

    # Set as default project
    run_command(["gcloud", "config", "set", "project", project_id])

    return SetupStatus(
        step="create_project",
        completed=True,
        message=f"Project {project_id} created",
        next_action="Enable APIs",
    )


@router.post("/enable-apis")
async def enable_apis(request: SetupRequest) -> SetupStatus:
    """Enable required Google APIs."""
    project_id = request.project_name.lower().replace(" ", "-")

    apis = [
        "gmail.googleapis.com",
        "calendar-json.googleapis.com",
        "drive.googleapis.com",
        "people.googleapis.com",
    ]

    # Set project
    run_command(["gcloud", "config", "set", "project", project_id])

    failed = []
    for api in apis:
        success, output = run_command(["gcloud", "services", "enable", api, "--project", project_id])
        if not success and "already enabled" not in output:
            failed.append(api)

    if failed:
        return SetupStatus(
            step="enable_apis",
            completed=False,
            message=f"Failed to enable APIs: {', '.join(failed)}",
        )

    return SetupStatus(
        step="enable_apis",
        completed=True,
        message="All APIs enabled",
        next_action="Create OAuth credentials",
    )


@router.post("/start-oauth-setup")
async def start_oauth_setup(request: SetupRequest) -> SetupStatus:
    """Generate OAuth setup instructions since it can't be fully automated."""
    project_id = request.project_name.lower().replace(" ", "-")

    # OAuth configuration would go here if needed

    # Create a URL to open the console at the right place
    console_url = f"https://console.cloud.google.com/apis/credentials?project={project_id}"

    instructions = f"""
    # OAuth Setup Instructions

    Since Google requires manual OAuth consent screen configuration, please:

    1. Click here to open Google Cloud Console: {console_url}

    2. Click "Configure Consent Screen" and choose:
       - User Type: External
       - App name: MindRoom
       - Support email: (your email)
       - Skip optional fields

    3. Add scopes:
       - ../auth/gmail.modify
       - ../auth/calendar
       - ../auth/drive.file

    4. Click "Create Credentials" > "OAuth client ID":
       - Application type: Web application
       - Name: MindRoom Web
       - Authorized redirect URIs:
         • http://localhost:8000/api/auth/google/callback
         • http://localhost:8001/api/auth/google/callback

    5. Copy the Client ID and Client Secret shown

    6. Click "Complete Setup" below and paste them
    """

    # Open browser if requested
    if not request.skip_browser:
        subprocess.Popen(["open", console_url])  # macOS  # noqa: ASYNC220

    return SetupStatus(
        step="oauth_setup",
        completed=False,
        message="Manual OAuth setup required",
        next_action=instructions,
    )


@router.post("/complete-setup")
async def complete_setup(credentials: dict) -> SetupStatus:
    """Save the OAuth credentials to environment."""
    client_id = credentials.get("client_id")
    client_secret = credentials.get("client_secret")
    project_id = credentials.get("project_id", "mindroom-integration")

    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail="Missing credentials")

    # Save to .env file
    env_path = Path(__file__).parent.parent.parent.parent.parent / ".env"

    env_lines = []
    if env_path.exists():
        with env_path.open() as f:
            env_lines = f.readlines()

    # Update or add credentials
    env_vars = {
        "GOOGLE_CLIENT_ID": client_id,
        "GOOGLE_CLIENT_SECRET": client_secret,
        "GOOGLE_PROJECT_ID": project_id,
        "GOOGLE_REDIRECT_URI": "http://localhost:8000/api/auth/google/callback",
    }

    for key, value in env_vars.items():
        found = False
        for i, line in enumerate(env_lines):
            if line.startswith(f"{key}="):
                env_lines[i] = f"{key}={value}\n"
                found = True
                break
        if not found:
            env_lines.append(f"{key}={value}\n")

    with env_path.open("w") as f:
        f.writelines(env_lines)

    # Also set in current environment
    for key, value in env_vars.items():
        os.environ[key] = value

    return SetupStatus(
        step="complete",
        completed=True,
        message="Setup complete! Gmail integration is ready.",
        credentials=env_vars,
    )


@router.get("/quick-setup-script")
async def get_quick_setup_script() -> dict[str, str]:
    """Generate a bash script that does everything possible automatically."""
    script = """#!/bin/bash
# MindRoom Google Setup Script
# This automates as much as possible of the Google Cloud setup

set -e

echo "🚀 MindRoom Google Cloud Setup"
echo "=============================="

# Check if gcloud is installed
if ! command -v gcloud &> /dev/null; then
    echo "❌ gcloud CLI not found. Please install it first:"
    echo "   https://cloud.google.com/sdk/docs/install"
    exit 1
fi

# Authenticate if needed
if ! gcloud auth list --format="value(account)" | grep -q .; then
    echo "📝 Authenticating with Google Cloud..."
    gcloud auth login
fi

# Create project
PROJECT_ID="mindroom-$(date +%s)"
echo "📦 Creating project: $PROJECT_ID"
gcloud projects create $PROJECT_ID --name="MindRoom Integration" || true

# Set as default
gcloud config set project $PROJECT_ID

# Enable billing (required for APIs)
echo "💳 Note: You may need to enable billing for this project"
echo "   Visit: https://console.cloud.google.com/billing/linkedaccount?project=$PROJECT_ID"
read -p "Press Enter once billing is enabled..."

# Enable APIs
echo "🔌 Enabling APIs..."
gcloud services enable gmail.googleapis.com
gcloud services enable calendar-json.googleapis.com
gcloud services enable drive.googleapis.com
gcloud services enable people.googleapis.com

echo ""
echo "✅ Automated setup complete!"
echo ""
echo "⚠️  Now you need to manually create OAuth credentials:"
echo "1. Open: https://console.cloud.google.com/apis/credentials?project=$PROJECT_ID"
echo "2. Configure OAuth consent screen (External, App name: MindRoom)"
echo "3. Create OAuth 2.0 Client ID (Web application)"
echo "4. Add redirect URI: http://localhost:8000/api/auth/google/callback"
echo "5. Copy the Client ID and Secret"
echo ""
echo "Then add to your .env file:"
echo "GOOGLE_CLIENT_ID=your_client_id_here"
echo "GOOGLE_CLIENT_SECRET=your_secret_here"
echo "GOOGLE_PROJECT_ID=$PROJECT_ID"
"""

    return {"script": script, "filename": "setup_google.sh"}
