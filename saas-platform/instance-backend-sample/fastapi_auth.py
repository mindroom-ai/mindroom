"""Minimal Supabase JWT auth for an instance FastAPI backend.

Copy this file into your instance backend and import `verify_user`
to protect any endpoint with the same auth used by the portal.
"""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from supabase import create_client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
ACCOUNT_ID = os.getenv("ACCOUNT_ID")  # The owner of this instance

if not SUPABASE_URL or not SUPABASE_ANON_KEY:
    _msg = "SUPABASE_URL and SUPABASE_ANON_KEY must be set"
    raise RuntimeError(_msg)

supabase_auth = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


async def verify_user(authorization: Annotated[str | None, Header()] = None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization.removeprefix("Bearer ").strip()

    try:
        user = supabase_auth.auth.get_user(token)
    except Exception as err:
        raise HTTPException(status_code=401, detail="Invalid token") from err

    if not user or not user.user:
        raise HTTPException(status_code=401, detail="Invalid token")

    if ACCOUNT_ID and user.user.id != ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Forbidden")

    return {"user_id": user.user.id, "email": user.user.email}


# Example usage (remove if integrating into an existing app):
app = FastAPI()


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/api/secure")
async def secure(user: Annotated[dict, Depends(verify_user)]) -> dict:
    return {"ok": True, "secure": True, "user_id": user["user_id"]}
