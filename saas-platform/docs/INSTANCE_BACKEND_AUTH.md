# Instance Backend Auth (Supabase JWT)

This document provides a minimal, secure way to protect the instance backend APIs using the same Supabase authentication as the customer portal.

The platform provisions each instance with environment variables so the instance backend can verify Supabase JWTs:

- `SUPABASE_URL`: Supabase project URL
- `SUPABASE_ANON_KEY`: Supabase anon key (verify JWTs)
- `ACCOUNT_ID`: The owner account id (auth.users.id) that owns this instance

Nginx forwards the `Authorization` header to the instance backend, so the backend can read the JWT from `Authorization: Bearer <token>`.

## FastAPI Example

```py
from fastapi import FastAPI, Header, HTTPException, Depends
from supabase import create_client
import os

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
ACCOUNT_ID = os.getenv("ACCOUNT_ID")
supabase_auth = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

async def verify_user(authorization: str | None = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization.removeprefix("Bearer ").strip()
    try:
        user = supabase_auth.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    if not user or not user.user:
        raise HTTPException(status_code=401, detail="Invalid token")

    if ACCOUNT_ID and user.user.id != ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Forbidden")

    return {"user_id": user.user.id, "email": user.user.email}

app = FastAPI()

@app.get("/api/secure-endpoint")
async def secure_endpoint(_user=Depends(verify_user)):
    return {"ok": True}
```

## Node/Express Example

```js
const express = require('express')
const { createClient } = require('@supabase/supabase-js')

const SUPABASE_URL = process.env.SUPABASE_URL
const SUPABASE_ANON_KEY = process.env.SUPABASE_ANON_KEY
const ACCOUNT_ID = process.env.ACCOUNT_ID
const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY)

async function verifyUser(req, res, next) {
  const auth = req.headers['authorization'] || ''
  const token = auth.startsWith('Bearer ') ? auth.slice(7) : ''
  if (!token) return res.status(401).json({ error: 'Missing token' })

  const { data: { user }, error } = await supabase.auth.getUser(token)
  if (error || !user) return res.status(401).json({ error: 'Invalid token' })

  if (ACCOUNT_ID && user.id !== ACCOUNT_ID) return res.status(403).json({ error: 'Forbidden' })

  req.user = user
  next()
}

const app = express()
app.get('/api/secure-endpoint', verifyUser, (req, res) => res.json({ ok: true }))
```

## Notes

- The instance backend only needs to validate JWTs against Supabase and enforce ownership (ACCOUNT_ID) for single-owner instances.
- If you later support multi-user access per instance, replace the simple `ACCOUNT_ID` check with a database lookup for allowed users/roles.
- No per-instance token or extra cookies are needed; use standard `Authorization: Bearer <jwt>` everywhere.
