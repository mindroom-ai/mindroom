# Token Tracking Implementation Plan for MindRoom

## Overview

This document outlines the complete implementation plan for adding token usage tracking to MindRoom. The system will track token usage for all AI agent interactions, supporting both self-hosted and SaaS deployment modes.

## Architecture Overview

Token tracking will integrate at these key points:
1. **Collection**: After agent responses in `ai.py` (lines 237, 247, 388)
2. **Storage**: Dual-mode (JSON for self-hosted, Supabase for SaaS)
3. **API**: Expose usage via MindRoom API (`src/mindroom/api/`)
4. **Frontend**: Display in MindRoom widget (`frontend/`)
5. **SaaS Platform**: Additional APIs in `saas-platform/platform-backend/`

## Phase 1: Core Token Tracking Module

### 1.1 Create Token Tracking Module
**File**: `src/mindroom/token_tracking.py` (NEW FILE)

```python
"""Token usage tracking for MindRoom agents."""

from dataclasses import dataclass, asdict
from datetime import datetime, UTC
import json
import os
from pathlib import Path
from typing import Optional

@dataclass
class TokenUsage:
    """Token usage record."""
    timestamp: datetime
    agent_name: str
    room_id: str
    thread_id: Optional[str]
    session_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    model_provider: str
    model_id: str
    # SaaS-only fields
    instance_id: Optional[str] = None
    account_id: Optional[str] = None

    def to_dict(self):
        d = asdict(self)
        d['timestamp'] = d['timestamp'].isoformat()
        return {k: v for k, v in d.items() if v is not None}


def get_storage_path() -> Path:
    """Get the path for token usage storage."""
    storage_path = Path(os.getenv("STORAGE_PATH", "./mindroom_data"))
    return storage_path / "token_usage.jsonl"


async def store_token_usage(usage: TokenUsage) -> None:
    """Store token usage - routes to appropriate backend."""
    # Check if running in SaaS mode
    if os.getenv("SUPABASE_URL") and os.getenv("ACCOUNT_ID"):
        # SaaS mode - store to Supabase
        await _store_to_supabase(usage)
    else:
        # Self-hosted mode - store locally
        _store_to_file(usage)


def _store_to_file(usage: TokenUsage) -> None:
    """Store to local JSON lines file."""
    storage_path = get_storage_path()
    storage_path.parent.mkdir(parents=True, exist_ok=True)

    with storage_path.open("a") as f:
        f.write(json.dumps(usage.to_dict()) + "\n")


async def _store_to_supabase(usage: TokenUsage) -> None:
    """Store to Supabase for SaaS mode."""
    from supabase import create_client

    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_KEY_FILE")
    if key and key.endswith("_key"):
        # Read from file if it's a file path
        with open(key) as f:
            key = f.read().strip()

    client = create_client(url, key)
    await client.table('token_usage').insert(usage.to_dict()).execute()


def get_token_usage(
    agent_name: Optional[str] = None,
    since: Optional[datetime] = None,
    room_id: Optional[str] = None,
) -> dict:
    """Query token usage from appropriate backend."""
    if os.getenv("SUPABASE_URL") and os.getenv("ACCOUNT_ID"):
        return _query_from_supabase(agent_name, since, room_id)
    else:
        return _query_from_file(agent_name, since, room_id)


def _query_from_file(
    agent_name: Optional[str] = None,
    since: Optional[datetime] = None,
    room_id: Optional[str] = None,
) -> dict:
    """Query from local file."""
    storage_path = get_storage_path()
    totals = {"input": 0, "output": 0, "total": 0, "count": 0}

    if not storage_path.exists():
        return totals

    with storage_path.open() as f:
        for line in f:
            record = json.loads(line)

            # Apply filters
            if agent_name and record.get('agent_name') != agent_name:
                continue
            if room_id and record.get('room_id') != room_id:
                continue
            if since and datetime.fromisoformat(record['timestamp']) < since:
                continue

            totals["input"] += record.get('input_tokens', 0)
            totals["output"] += record.get('output_tokens', 0)
            totals["total"] += record.get('total_tokens', 0)
            totals["count"] += 1

    return totals


def _query_from_supabase(
    agent_name: Optional[str] = None,
    since: Optional[datetime] = None,
    room_id: Optional[str] = None,
) -> dict:
    """Query from Supabase."""
    from supabase import create_client

    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_KEY_FILE")
    if key and key.endswith("_key"):
        with open(key) as f:
            key = f.read().strip()

    client = create_client(url, key)

    query = client.table('token_usage').select('*')

    if os.getenv("ACCOUNT_ID"):
        query = query.eq("account_id", os.getenv("ACCOUNT_ID"))
    if os.getenv("CUSTOMER_ID"):
        query = query.eq("instance_id", os.getenv("CUSTOMER_ID"))
    if agent_name:
        query = query.eq("agent_name", agent_name)
    if room_id:
        query = query.eq("room_id", room_id)
    if since:
        query = query.gte("timestamp", since.isoformat())

    result = query.execute()

    totals = {"input": 0, "output": 0, "total": 0, "count": 0}
    for record in result.data:
        totals["input"] += record.get('input_tokens', 0)
        totals["output"] += record.get('output_tokens', 0)
        totals["total"] += record.get('total_tokens', 0)
        totals["count"] += 1

    return totals
```

## Phase 2: Integration Points

### 2.1 Integrate in ai.py
**File**: `src/mindroom/ai.py`
**Location**: After line 247 (in `_cached_agent_run`) and line 388 (in `stream_agent_response`)

**Add import at top** (after line 15):
```python
from .token_tracking import TokenUsage, store_token_usage
```

**Add tracking function** (after line 253):
```python
async def _track_token_usage(
    response: RunResponse,
    agent_name: str,
    session_id: str,
    room_id: Optional[str] = None,
) -> None:
    """Track token usage from agent response."""
    if not response.metrics:
        return

    # Extract metrics from Agno's response
    usage = TokenUsage(
        timestamp=datetime.now(UTC),
        agent_name=agent_name,
        room_id=room_id or "unknown",
        thread_id=None,  # Will be set from bot.py if available
        session_id=session_id,
        input_tokens=sum(response.metrics.get("input_tokens", [0])),
        output_tokens=sum(response.metrics.get("output_tokens", [0])),
        total_tokens=sum(response.metrics.get("total_tokens", [0])),
        model_provider=response.model_provider or "unknown",
        model_id=response.model or "unknown",
        instance_id=os.getenv("CUSTOMER_ID"),  # From K8s deployment
        account_id=os.getenv("ACCOUNT_ID"),    # From K8s deployment
    )

    await store_token_usage(usage)
```

**Modify `_cached_agent_run`** (line 247-252):
```python
response = await agent.arun(full_prompt, session_id=session_id)

# Track token usage
await _track_token_usage(response, agent_name, session_id)

cache.set(cache_key, response)
```

### 2.2 Integrate in bot.py for Team Tracking
**File**: `src/mindroom/bot.py`
**Location**: In `_generate_response` method after line 1414

**Add import** (after line 50):
```python
from .token_tracking import TokenUsage, store_token_usage
```

**Add tracking for team responses** (in `_generate_team_response_helper`, after line 1038):
```python
# Track token usage for team responses
if hasattr(response, 'metrics') and response.metrics:
    usage = TokenUsage(
        timestamp=datetime.now(UTC),
        agent_name=f"team_{team_name}",  # Prefix with team_
        room_id=room_id,
        thread_id=thread_id,
        session_id=session_id,
        input_tokens=sum(response.metrics.get("input_tokens", [0])),
        output_tokens=sum(response.metrics.get("output_tokens", [0])),
        total_tokens=sum(response.metrics.get("total_tokens", [0])),
        model_provider=response.model_provider or "unknown",
        model_id=response.model or "unknown",
        instance_id=os.getenv("CUSTOMER_ID"),
        account_id=os.getenv("ACCOUNT_ID"),
    )
    await store_token_usage(usage)
```

## Phase 3: API Endpoints

### 3.1 Add Usage API to MindRoom Backend
**File**: `src/mindroom/api/usage.py` (NEW FILE)

```python
"""Token usage API endpoints."""

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..token_tracking import get_token_usage
from .main import verify_user

router = APIRouter(prefix="/usage", tags=["usage"])


class UsageResponse(BaseModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int
    count: int
    period: str


@router.get("/", response_model=UsageResponse)
async def get_usage(
    agent: Optional[str] = None,
    room: Optional[str] = None,
    period: str = "day",
    _user: dict = Depends(verify_user),
) -> UsageResponse:
    """Get token usage statistics."""

    # Calculate time range
    now = datetime.now()
    since_map = {
        "hour": now - timedelta(hours=1),
        "day": now - timedelta(days=1),
        "week": now - timedelta(weeks=1),
        "month": now - timedelta(days=30),
    }
    since = since_map.get(period, now - timedelta(days=1))

    # Get usage data
    usage = get_token_usage(
        agent_name=agent,
        since=since,
        room_id=room,
    )

    return UsageResponse(
        input_tokens=usage["input"],
        output_tokens=usage["output"],
        total_tokens=usage["total"],
        count=usage["count"],
        period=period,
    )


@router.get("/agents")
async def get_agent_breakdown(
    period: str = "day",
    _user: dict = Depends(verify_user),
) -> dict:
    """Get per-agent token usage breakdown."""
    # Implementation to return usage grouped by agent
    # This would query all agents and group the results
    pass
```

### 3.2 Register Usage Router
**File**: `src/mindroom/api/main.py`
**Location**: After line 21 (with other router imports)

```python
from mindroom.api.usage import router as usage_router
```

**Location**: After line 120 (with other router registrations)
```python
app.include_router(usage_router)
```

## Phase 4: Frontend Integration

### 4.1 Add Usage Component
**File**: `frontend/src/components/TokenUsage/TokenUsage.tsx` (NEW FILE)

```tsx
import React, { useEffect, useState } from 'react';
import { api } from '@/lib/api';
import { Card } from '@/components/ui/card';

interface UsageData {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  count: number;
  period: string;
}

export function TokenUsage() {
  const [usage, setUsage] = useState<UsageData | null>(null);
  const [period, setPeriod] = useState('day');

  useEffect(() => {
    const fetchUsage = async () => {
      try {
        const data = await api.get(`/usage?period=${period}`);
        setUsage(data);
      } catch (error) {
        console.error('Failed to fetch usage:', error);
      }
    };

    fetchUsage();
    const interval = setInterval(fetchUsage, 60000); // Refresh every minute
    return () => clearInterval(interval);
  }, [period]);

  if (!usage) return null;

  return (
    <Card className="p-4">
      <h3 className="text-lg font-semibold mb-2">Token Usage</h3>
      <div className="space-y-1 text-sm">
        <div>Input: {usage.input_tokens.toLocaleString()}</div>
        <div>Output: {usage.output_tokens.toLocaleString()}</div>
        <div>Total: {usage.total_tokens.toLocaleString()}</div>
        <div>Requests: {usage.count}</div>
      </div>
      <select
        value={period}
        onChange={(e) => setPeriod(e.target.value)}
        className="mt-2 text-sm"
      >
        <option value="hour">Last Hour</option>
        <option value="day">Last 24 Hours</option>
        <option value="week">Last Week</option>
        <option value="month">Last Month</option>
      </select>
    </Card>
  );
}
```

### 4.2 Add to Dashboard
**File**: `frontend/src/components/Dashboard/Dashboard.tsx`
**Location**: After line 10 (imports)

```tsx
import { TokenUsage } from '../TokenUsage/TokenUsage';
```

**Location**: In the render method (around line 50-60, within the dashboard layout)
```tsx
<TokenUsage />
```

## Phase 5: SaaS Platform Integration

### 5.1 Database Schema
**File**: `saas-platform/supabase/migrations/002_add_token_usage.sql` (NEW FILE)

```sql
CREATE TABLE IF NOT EXISTS token_usage (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp TIMESTAMPTZ NOT NULL,
    instance_id TEXT NOT NULL,
    account_id UUID REFERENCES accounts(id),
    agent_name TEXT NOT NULL,
    room_id TEXT NOT NULL,
    thread_id TEXT,
    session_id TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    model_provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for common queries
CREATE INDEX idx_instance_timestamp ON token_usage(instance_id, timestamp);
CREATE INDEX idx_agent_usage ON token_usage(instance_id, agent_name, timestamp);
CREATE INDEX idx_account_usage ON token_usage(account_id, timestamp);

-- Row Level Security
ALTER TABLE token_usage ENABLE ROW LEVEL SECURITY;

-- Account owners can view their own usage
CREATE POLICY "Account owners can view usage" ON token_usage
    FOR SELECT USING (account_id = auth.uid());
```

### 5.2 Platform Backend Usage Routes
**File**: `saas-platform/platform-backend/src/backend/routes/usage.py`
**Location**: Add to existing file (after line 30)

```python
@router.get("/instances/{instance_id}/token-usage/detailed")
async def get_detailed_token_usage(
    instance_id: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    current_user: dict = Depends(get_current_user),
    supabase = Depends(get_supabase),
) -> dict:
    """Get detailed token usage with cost estimates."""

    # Verify instance ownership
    instance = await verify_instance_owner(instance_id, current_user["id"], supabase)

    # Query token usage
    query = supabase.table("token_usage").select("*")
    query = query.eq("instance_id", instance_id)

    if start_date:
        query = query.gte("timestamp", start_date.isoformat())
    if end_date:
        query = query.lte("timestamp", end_date.isoformat())

    result = await query.execute()
    usage_records = result.data

    # Calculate costs (simplified)
    MODEL_COSTS = {
        "gpt-4o": {"input": 0.005, "output": 0.015},
        "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
        "claude-3-5-sonnet": {"input": 0.003, "output": 0.015},
    }

    total_cost = 0
    for record in usage_records:
        model_id = record["model_id"]
        if model_id in MODEL_COSTS:
            costs = MODEL_COSTS[model_id]
            total_cost += (
                record["input_tokens"] / 1000 * costs["input"] +
                record["output_tokens"] / 1000 * costs["output"]
            )

    return {
        "usage": usage_records,
        "estimated_cost": total_cost,
        "period": {
            "start": start_date.isoformat() if start_date else None,
            "end": end_date.isoformat() if end_date else None,
        }
    }
```

## Summary of Changes

### New Files Created:
1. `src/mindroom/token_tracking.py` - Core tracking module
2. `src/mindroom/api/usage.py` - Usage API endpoints
3. `frontend/src/components/TokenUsage/TokenUsage.tsx` - Frontend component
4. `saas-platform/supabase/migrations/002_add_token_usage.sql` - Database schema

### Files Modified:
1. **`src/mindroom/ai.py`** - Lines 247, 388 - Add tracking after agent.arun()
2. **`src/mindroom/bot.py`** - Line 1038 - Track team responses
3. **`src/mindroom/api/main.py`** - Lines 21, 120 - Register usage router
4. **`frontend/src/components/Dashboard/Dashboard.tsx`** - Add TokenUsage component
5. **`saas-platform/platform-backend/src/backend/routes/usage.py`** - Add detailed usage endpoint

## Key Design Decisions

1. **Dual-Mode Storage**: Automatically detects SaaS vs self-hosted based on environment variables
2. **Minimal Dependencies**: Uses only standard library for self-hosted mode
3. **DRY Code**: Single tracking function reused everywhere
4. **Agno Integration**: Leverages existing metrics from RunResponse
5. **Frontend Display**: Simple component that works for both modes
6. **Cost Tracking**: Only in SaaS mode, calculated on-demand

## Implementation Notes

- **Simple**: ~300 lines of code total
- **Functional**: No unnecessary classes or abstractions
- **Clean**: Clear separation between self-hosted and SaaS
- **Testable**: Each component can be tested independently
- **Incremental**: Can be implemented in phases

## Testing Strategy

1. **Unit Tests**: Test token tracking module functions
2. **Integration Tests**: Test API endpoints
3. **E2E Tests**: Test full flow from agent response to frontend display
4. **Manual Testing**: Verify both self-hosted and SaaS modes work correctly

## Rollout Plan

1. **Phase 1**: Implement core tracking module and test locally
2. **Phase 2**: Add API endpoints and test with Postman/curl
3. **Phase 3**: Add frontend component and verify display
4. **Phase 4**: Deploy to staging and test SaaS mode
5. **Phase 5**: Production deployment with monitoring

## Optional Future Enhancements

1. **Rate Limiting**: Implement token-based rate limiting
2. **Alerts**: Send notifications when usage exceeds thresholds
3. **Export**: Allow CSV/JSON export of usage data
4. **Visualization**: Add charts and graphs for usage trends
5. **Cost Optimization**: Suggest cheaper models based on usage patterns
