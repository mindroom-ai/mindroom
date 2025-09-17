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

from backend.utils.logger import logger

# Load environment variables from saas-platform/.env
# Use absolute path relative to this file's location
config_dir = Path(__file__).parent
saas_platform_env = config_dir / "../../../.env"
load_dotenv(saas_platform_env)

# Configure logging once
logging.basicConfig(level=logging.INFO)


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
    logger.warning("Supabase not configured: missing SUPABASE_URL or SUPABASE_SERVICE_KEY")
    supabase = None  # type: ignore[assignment]
    auth_client = None  # type: ignore[assignment]

# Platform configuration
PLATFORM_DOMAIN = os.getenv("PLATFORM_DOMAIN", "mindroom.chat")
ENVIRONMENT = os.getenv("ENVIRONMENT", "production")
ENABLE_CLEANUP_SCHEDULER = os.getenv("ENABLE_CLEANUP_SCHEDULER", "false").lower() in {
    "1",
    "true",
    "yes",
}

# Stripe configuration
stripe.api_key = _get_secret("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = _get_secret("STRIPE_WEBHOOK_SECRET", "")

# Provisioner API key for internal provisioning actions
PROVISIONER_API_KEY = _get_secret("PROVISIONER_API_KEY", "")

# Gitea registry credentials (for pulling instance images)
GITEA_USER = os.getenv("GITEA_USER", "")

# API keys for MindRoom instances (shared across customers for now)
OPENAI_API_KEY = _get_secret("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = _get_secret("ANTHROPIC_API_KEY", "")
GOOGLE_API_KEY = _get_secret("GOOGLE_API_KEY", "")
OPENROUTER_API_KEY = _get_secret("OPENROUTER_API_KEY", "")
DEEPSEEK_API_KEY = _get_secret("DEEPSEEK_API_KEY", "")

# Gitea registry token
GITEA_TOKEN = _get_secret("GITEA_TOKEN", "")


def _build_allowed_origins(domain: str, environment: str) -> list[str]:
    """Compute allowed CORS origins from superdomain and environment.

    Always allow the platform app origin. Include localhost origins in
    non-production environments to ease development.
    Additional origins can be supplied via comma-separated ALLOWED_ORIGINS env.
    """
    origins = [f"https://app.{domain}"]

    if environment != "production":
        origins += [
            "http://localhost:3000",
            "http://localhost:3001",
        ]

    extra = os.getenv("ALLOWED_ORIGINS", "").strip()
    if extra:
        origins += [o.strip() for o in extra.split(",") if o.strip()]

    return origins


# CORS allowed origins
ALLOWED_ORIGINS = _build_allowed_origins(PLATFORM_DOMAIN, ENVIRONMENT)

__all__ = [
    "ALLOWED_ORIGINS",
    "ENABLE_CLEANUP_SCHEDULER",
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
