"""FastAPI application for MindRoom Dokku Provisioner."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .dokku.client import test_connection
from .routers import health, provision

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Startup
    logger.info("Starting MindRoom Dokku Provisioner...")

    # Test Dokku connection
    if not test_connection():
        logger.error("Cannot connect to Dokku server - service will have limited functionality")
    else:
        logger.info("Successfully connected to Dokku server")

    yield

    # Shutdown
    logger.info("Shutting down MindRoom Dokku Provisioner...")


app = FastAPI(
    title="MindRoom Dokku Provisioner",
    description="Service for provisioning MindRoom instances on Dokku",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(health.router, tags=["health"])
app.include_router(provision.router, prefix="/api/v1", tags=["provisioning"])


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    """Handle HTTP exceptions."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
    )


@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    """Handle general exceptions."""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error"},
    )


@app.get("/", tags=["root"])
async def root():
    """Root endpoint."""
    return {
        "service": "MindRoom Dokku Provisioner",
        "status": "operational",
        "version": "1.0.0",
    }
