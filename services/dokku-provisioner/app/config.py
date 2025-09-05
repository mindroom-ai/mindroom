"""Configuration settings for the Dokku Provisioner service."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings."""

    # Server Configuration
    port: int = 8002
    host: str = "0.0.0.0"  # noqa: S104
    log_level: str = "INFO"

    # Dokku SSH Configuration
    dokku_host: str
    dokku_user: str = "dokku"
    dokku_ssh_key_path: str = "/app/ssh/dokku_key"
    dokku_port: int = 22

    # Domain Configuration
    base_domain: str = "mindroom.chat"

    # Docker Images
    mindroom_backend_image: str = "mindroom/backend:latest"
    mindroom_frontend_image: str = "mindroom/frontend:latest"

    # Supabase Configuration
    supabase_url: str
    supabase_service_key: str

    # Resource Limits (defaults per tier)
    default_memory_limit: str = "512m"
    default_cpu_limit: str = "0.5"

    # Storage Configuration
    instance_data_base: str = "/var/lib/dokku/data/storage"

    # Matrix Server Images
    tuwunel_image: str = "ghcr.io/tulir/tuwunel:latest"
    synapse_image: str = "matrixdotorg/synapse:latest"

    # Redis Configuration for caching
    redis_image: str = "redis:7-alpine"

    # PostgreSQL Configuration
    postgres_image: str = "postgres:15-alpine"

    class Config:
        """Pydantic config."""

        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


# Create a singleton instance
settings = Settings()
