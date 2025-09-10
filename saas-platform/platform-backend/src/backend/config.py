"""Shared configuration and clients for the backend.

Centralizes environment loading, logging configuration, Supabase clients,
and Stripe configuration so other modules can import from a single place.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC

import stripe
from dotenv import load_dotenv

from supabase import create_client

# Load environment variables from repo root and backend dir if present
load_dotenv(".env")
load_dotenv("../.env")

# Configure logging once
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mindroom.backend")

# Initialize Supabase (service client bypasses RLS)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
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

# Stripe configuration
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

# Provisioner API key for internal provisioning actions
PROVISIONER_API_KEY = os.getenv("PROVISIONER_API_KEY", "")

# Gitea registry credentials (for pulling instance images)
GITEA_USER = os.getenv("GITEA_USER", "")

# OpenRouter API key for AI model access
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

# OpenAI API key for embeddings
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GITEA_TOKEN = os.getenv("GITEA_TOKEN", "")

# CORS allowed origins
ALLOWED_ORIGINS = [
    "https://app.staging.mindroom.chat",
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
