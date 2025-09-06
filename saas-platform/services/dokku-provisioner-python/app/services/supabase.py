"""Supabase integration for tracking instance status."""

import logging
from datetime import datetime
from typing import Any

from supabase import Client, create_client

from ..config import settings

logger = logging.getLogger(__name__)


class SupabaseService:
    """Service for interacting with Supabase."""

    def __init__(self):
        """Initialize Supabase client."""
        try:
            self.client: Client = create_client(
                settings.supabase_url,
                settings.supabase_service_key,
            )
            self.connected = True
            logger.info("Connected to Supabase")
        except Exception as e:
            logger.error(f"Failed to connect to Supabase: {e}")
            self.client = None
            self.connected = False

    def update_instance_status(
        self,
        subscription_id: str,
        status: str,
        app_name: str | None = None,
        urls: dict[str, str] | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Update instance status in Supabase.

        Args:
            subscription_id: Subscription identifier
            status: Instance status (provisioning, running, failed, etc.)
            app_name: Dokku app name
            urls: Dictionary of URLs (frontend, backend, matrix)
            error: Error message if failed
            metadata: Additional metadata

        Returns:
            True if successful, False otherwise

        """
        if not self.client:
            logger.error("Supabase client not initialized")
            return False

        try:
            data = {
                "subscription_id": subscription_id,
                "status": status,
                "updated_at": datetime.utcnow().isoformat(),
            }

            if app_name:
                data["app_name"] = app_name

            if urls:
                data["urls"] = urls

            if error:
                data["error"] = error

            if metadata:
                data["metadata"] = metadata

            # Update or insert the instance record
            response = (
                self.client.table("instances")
                .upsert(
                    data,
                    on_conflict="subscription_id",
                )
                .execute()
            )

            if response.data:
                logger.info(f"Updated instance status for {subscription_id}: {status}")
                return True
            logger.error("Failed to update instance status: no data returned")
            return False

        except Exception as e:
            logger.error(f"Failed to update instance status: {e}")
            return False

    def get_instance_info(self, subscription_id: str) -> dict[str, Any] | None:
        """Get instance information from Supabase.

        Args:
            subscription_id: Subscription identifier

        Returns:
            Instance data or None if not found

        """
        if not self.client:
            logger.error("Supabase client not initialized")
            return None

        try:
            response = (
                self.client.table("instances")
                .select("*")
                .eq(
                    "subscription_id",
                    subscription_id,
                )
                .single()
                .execute()
            )

            return response.data

        except Exception as e:
            logger.error(f"Failed to get instance info: {e}")
            return None

    def list_instances(
        self,
        account_id: str | None = None,
        status: str | None = None,
    ) -> list:
        """List instances with optional filters.

        Args:
            account_id: Filter by account ID
            status: Filter by status

        Returns:
            List of instance records

        """
        if not self.client:
            logger.error("Supabase client not initialized")
            return []

        try:
            query = self.client.table("instances").select("*")

            if account_id:
                query = query.eq("account_id", account_id)

            if status:
                query = query.eq("status", status)

            response = query.execute()
            return response.data or []

        except Exception as e:
            logger.error(f"Failed to list instances: {e}")
            return []

    def create_backup_record(
        self,
        subscription_id: str,
        app_name: str,
        backup_url: str,
        size_mb: float,
    ) -> bool:
        """Create a backup record in Supabase.

        Args:
            subscription_id: Subscription identifier
            app_name: Dokku app name
            backup_url: URL to download backup
            size_mb: Backup size in megabytes

        Returns:
            True if successful, False otherwise

        """
        if not self.client:
            logger.error("Supabase client not initialized")
            return False

        try:
            data = {
                "subscription_id": subscription_id,
                "app_name": app_name,
                "backup_url": backup_url,
                "size_mb": size_mb,
                "created_at": datetime.utcnow().isoformat(),
                "expires_at": None,  # Set expiration if needed
            }

            response = self.client.table("backups").insert(data).execute()

            if response.data:
                logger.info(f"Created backup record for {subscription_id}")
                return True
            logger.error("Failed to create backup record: no data returned")
            return False

        except Exception as e:
            logger.error(f"Failed to create backup record: {e}")
            return False

    def log_event(
        self,
        subscription_id: str,
        event_type: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Log an event to Supabase.

        Args:
            subscription_id: Subscription identifier
            event_type: Type of event
            message: Event message
            metadata: Additional metadata

        Returns:
            True if successful, False otherwise

        """
        if not self.client:
            logger.error("Supabase client not initialized")
            return False

        try:
            data = {
                "subscription_id": subscription_id,
                "event_type": event_type,
                "message": message,
                "metadata": metadata or {},
                "created_at": datetime.utcnow().isoformat(),
            }

            response = self.client.table("events").insert(data).execute()

            if response.data:
                logger.debug(f"Logged event for {subscription_id}: {event_type}")
                return True
            logger.error("Failed to log event: no data returned")
            return False

        except Exception as e:
            logger.error(f"Failed to log event: {e}")
            return False


# Singleton instance
supabase_service = SupabaseService()


def test_connection() -> bool:
    """Test Supabase connection."""
    return supabase_service.connected


# Convenience functions
def update_instance_status(*args, **kwargs):
    """Update instance status."""
    return supabase_service.update_instance_status(*args, **kwargs)


def get_instance_info(*args, **kwargs):
    """Get instance information."""
    return supabase_service.get_instance_info(*args, **kwargs)


def log_event(*args, **kwargs):
    """Log an event."""
    return supabase_service.log_event(*args, **kwargs)
