"""Shared configuration and clients for the backend.

Centralizes environment loading, logging configuration, Supabase clients,
and Stripe configuration so other modules can import from a single place.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC
from pathlib import Path

import stripe
from dotenv import load_dotenv
from supabase import create_client

# Load environment variables from repo root and backend dir if present
load_dotenv(".env")
load_dotenv("../.env")

# Configure logging once
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mindroom.backend")


def _get_secret(name: str, default: str = "") -> str:
    """Return secret from env or file .

    If `NAME` not set, but `NAME_FILE` points to a readable file, read its
    contents and return the stripped value. Otherwise return default.
    """
    val = os.getenv(name)
    if val:
        return val
    file_var = f"{name}_FILE"
    file_path = os.getenv(file_var)
    if file_path and Path(file_path).exists():
        try:
            with Path(file_path).open(encoding="utf-8") as fh:
                return fh.read().strip()
        except Exception:
            logger.warning("Failed reading secret file for %s", name)
    return default


# Initialize Supabase (service client bypasses RLS)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = _get_secret("SUPABASE_SERVICE_KEY")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")

if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    auth_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
else:
    logger.warning(
        "Supabase not configured: missing SUPABASE_URL or SUPABASE_SERVICE_KEY"
    )
    supabase = None  # type: ignore[assignment]
    auth_client = None  # type: ignore[assignment]

# Platform configuration
PLATFORM_DOMAIN = os.getenv("PLATFORM_DOMAIN", "mindroom.chat")
ENVIRONMENT = os.getenv("ENVIRONMENT", "production")

# Stripe configuration
stripe.api_key = _get_secret("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = _get_secret("STRIPE_WEBHOOK_SECRET", "")

# Provisioner API key for internal provisioning actions
PROVISIONER_API_KEY = _get_secret("PROVISIONER_API_KEY", "")

# Gitea registry credentials (for pulling instance images)
GITEA_USER = os.getenv("GITEA_USER", "")

# OpenRouter API key for AI model access
OPENROUTER_API_KEY = _get_secret("OPENROUTER_API_KEY", "")

# OpenAI API key for embeddings
OPENAI_API_KEY = _get_secret("OPENAI_API_KEY", "")
GITEA_TOKEN = _get_secret("GITEA_TOKEN", "")

# CORS allowed origins
ALLOWED_ORIGINS = [
    "https://app.staging.mindroom.chat",
    "https://app.test.mindroom.chat",
    "https://app.mindroom.chat",
    "http://localhost:3000",
    "http://localhost:3001",
]

__all__ = [
    "ALLOWED_ORIGINS",
    "ENVIRONMENT",
    "GITEA_TOKEN",
    "GITEA_USER",
    "PLATFORM_DOMAIN",
    "PROVISIONER_API_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "SUPABASE_ANON_KEY",
    "SUPABASE_URL",
    "UTC",
    "auth_client",
    "logger",
    "stripe",
    "supabase",
]
