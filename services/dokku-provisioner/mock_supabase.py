#!/usr/bin/env python3
"""Mock Supabase server for testing the provisioner without real Supabase."""

from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Mock Supabase for Testing")

# In-memory storage
instances_db: dict[str, dict[str, Any]] = {}
events_db: list[dict[str, Any]] = []
backups_db: list[dict[str, Any]] = []


class InstanceUpdate(BaseModel):
    subscription_id: str
    status: str
    app_name: str | None = None
    urls: dict[str, str] | None = None
    error: str | None = None
    metadata: dict[str, Any] | None = None
    updated_at: str


@app.get("/")
def root():
    return {"service": "Mock Supabase", "status": "running"}


@app.post("/rest/v1/instances")
def create_or_update_instance(data: dict[str, Any]):
    """Mock Supabase upsert endpoint."""
    subscription_id = data.get("subscription_id")
    if subscription_id:
        instances_db[subscription_id] = data
        return {"data": [data]}
    return {"data": []}


@app.get("/rest/v1/instances")
def get_instances(subscription_id: str | None = None):
    """Mock Supabase select endpoint."""
    if subscription_id:
        instance = instances_db.get(subscription_id)
        if instance:
            return {"data": instance}
        raise HTTPException(404, "Not found")
    return {"data": list(instances_db.values())}


@app.post("/rest/v1/events")
def create_event(data: dict[str, Any]):
    """Mock Supabase events insert."""
    events_db.append(data)
    return {"data": [data]}


@app.get("/rest/v1/events")
def get_events(subscription_id: str | None = None):
    """Get events."""
    if subscription_id:
        filtered = [e for e in events_db if e.get("subscription_id") == subscription_id]
        return {"data": filtered}
    return {"data": events_db}


@app.post("/rest/v1/backups")
def create_backup(data: dict[str, Any]):
    """Mock Supabase backups insert."""
    backups_db.append(data)
    return {"data": [data]}


@app.get("/health")
def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "instances_count": len(instances_db),
        "events_count": len(events_db),
        "backups_count": len(backups_db),
    }


@app.get("/stats")
def get_stats():
    """Get mock database statistics."""
    return {
        "instances": instances_db,
        "events": events_db,
        "backups": backups_db,
        "counts": {
            "instances": len(instances_db),
            "events": len(events_db),
            "backups": len(backups_db),
        },
    }


if __name__ == "__main__":
    print("Starting Mock Supabase on port 8003...")
    print("Configure provisioner with SUPABASE_URL=http://localhost:8003")
    uvicorn.run(app, host="0.0.0.0", port=8003)
