"""Dokku SSH client for executing commands on the Dokku server."""

import logging
from pathlib import Path

import paramiko

from ..config import settings

logger = logging.getLogger(__name__)


class DokkuClient:
    """SSH client for executing Dokku commands."""

    def __init__(self):
        """Initialize the Dokku client."""
        self.ssh: paramiko.SSHClient | None = None
        self.connect()

    def connect(self) -> None:
        """Establish SSH connection to Dokku server."""
        try:
            self.ssh = paramiko.SSHClient()
            self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            # Check if SSH key file exists
            key_path = Path(settings.dokku_ssh_key_path)
            if not key_path.exists():
                logger.error(f"SSH key not found at {settings.dokku_ssh_key_path}")
                self.ssh = None
                return

            # Load SSH key
            try:
                key = paramiko.RSAKey.from_private_key_file(str(key_path))
            except Exception:
                # Try as Ed25519 key if RSA fails
                try:
                    key = paramiko.Ed25519Key.from_private_key_file(str(key_path))
                except Exception as e:
                    logger.error(f"Failed to load SSH key: {e}")
                    self.ssh = None
                    return

            # Connect to Dokku server
            self.ssh.connect(
                hostname=settings.dokku_host,
                port=settings.dokku_port,
                username=settings.dokku_user,
                pkey=key,
                timeout=30,
                look_for_keys=False,
            )
            logger.info(f"Connected to Dokku at {settings.dokku_host}")

        except Exception as e:
            logger.error(f"Failed to connect to Dokku: {e}")
            self.ssh = None

    def disconnect(self) -> None:
        """Close SSH connection."""
        if self.ssh:
            self.ssh.close()
            self.ssh = None
            logger.info("Disconnected from Dokku")

    def execute(self, command: str, timeout: int = 30) -> tuple[int, str, str]:
        """Execute a Dokku command via SSH.

        Args:
            command: Command to execute (will be prefixed with 'dokku' if needed)
            timeout: Command timeout in seconds

        Returns:
            Tuple of (exit_status, stdout, stderr)

        """
        if not self.ssh:
            logger.error("Not connected to Dokku")
            return 1, "", "Not connected to Dokku server"

        # Ensure command starts with 'dokku' for safety
        if not command.startswith("dokku"):
            command = f"dokku {command}"

        logger.debug(f"Executing: {command}")

        try:
            stdin, stdout, stderr = self.ssh.exec_command(command, timeout=timeout)

            exit_status = stdout.channel.recv_exit_status()
            output = stdout.read().decode("utf-8")
            error = stderr.read().decode("utf-8")

            if exit_status != 0:
                logger.error(f"Command failed: {command}\nError: {error}")
            else:
                logger.debug(f"Command succeeded: {command}")

            return exit_status, output, error

        except Exception as e:
            logger.error(f"Failed to execute command: {e}")
            return 1, "", str(e)

    def app_exists(self, app_name: str) -> bool:
        """Check if a Dokku app exists."""
        status, output, _ = self.execute(f"apps:exists {app_name}")
        return status == 0

    def create_app(self, app_name: str) -> bool:
        """Create a new Dokku app."""
        if self.app_exists(app_name):
            logger.warning(f"App {app_name} already exists")
            return True

        status, _, _ = self.execute(f"apps:create {app_name}")
        return status == 0

    def destroy_app(self, app_name: str, force: bool = True) -> bool:
        """Destroy a Dokku app."""
        if not self.app_exists(app_name):
            logger.warning(f"App {app_name} does not exist")
            return True

        force_flag = "--force" if force else ""
        status, _, _ = self.execute(f"apps:destroy {app_name} {force_flag}")
        return status == 0

    def set_config(self, app_name: str, config: dict[str, str]) -> bool:
        """Set environment variables for an app."""
        if not config:
            return True

        # Escape values properly
        config_str = " ".join([f'{k}="{v}"' for k, v in config.items()])
        status, _, _ = self.execute(f"config:set {app_name} {config_str}")
        return status == 0

    def unset_config(self, app_name: str, keys: list[str]) -> bool:
        """Unset environment variables for an app."""
        if not keys:
            return True

        keys_str = " ".join(keys)
        status, _, _ = self.execute(f"config:unset {app_name} {keys_str}")
        return status == 0

    def get_config(self, app_name: str) -> dict[str, str]:
        """Get environment variables for an app."""
        status, output, _ = self.execute(f"config:export {app_name}")
        if status != 0:
            return {}

        config = {}
        for line in output.strip().split("\n"):
            if "=" in line and line.startswith("export "):
                key_value = line.replace("export ", "").strip()
                key, value = key_value.split("=", 1)
                # Remove quotes if present
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                config[key] = value

        return config

    def create_postgres(self, service_name: str) -> bool:
        """Create a PostgreSQL database."""
        status, _, _ = self.execute(f"postgres:create {service_name}")
        return status == 0

    def destroy_postgres(self, service_name: str, force: bool = True) -> bool:
        """Destroy a PostgreSQL database."""
        force_flag = "--force" if force else ""
        status, _, _ = self.execute(f"postgres:destroy {service_name} {force_flag}")
        return status == 0

    def link_postgres(self, service_name: str, app_name: str) -> bool:
        """Link PostgreSQL to an app."""
        status, _, _ = self.execute(f"postgres:link {service_name} {app_name}")
        return status == 0

    def unlink_postgres(self, service_name: str, app_name: str) -> bool:
        """Unlink PostgreSQL from an app."""
        status, _, _ = self.execute(f"postgres:unlink {service_name} {app_name}")
        return status == 0

    def create_redis(self, service_name: str) -> bool:
        """Create a Redis instance."""
        status, _, _ = self.execute(f"redis:create {service_name}")
        return status == 0

    def destroy_redis(self, service_name: str, force: bool = True) -> bool:
        """Destroy a Redis instance."""
        force_flag = "--force" if force else ""
        status, _, _ = self.execute(f"redis:destroy {service_name} {force_flag}")
        return status == 0

    def link_redis(self, service_name: str, app_name: str) -> bool:
        """Link Redis to an app."""
        status, _, _ = self.execute(f"redis:link {service_name} {app_name}")
        return status == 0

    def unlink_redis(self, service_name: str, app_name: str) -> bool:
        """Unlink Redis from an app."""
        status, _, _ = self.execute(f"redis:unlink {service_name} {app_name}")
        return status == 0

    def set_domains(self, app_name: str, domains: list[str]) -> bool:
        """Set domains for an app."""
        success = True
        for domain in domains:
            status, _, _ = self.execute(f"domains:add {app_name} {domain}")
            if status != 0:
                success = False
        return success

    def remove_domains(self, app_name: str, domains: list[str]) -> bool:
        """Remove domains from an app."""
        success = True
        for domain in domains:
            status, _, _ = self.execute(f"domains:remove {app_name} {domain}")
            if status != 0:
                success = False
        return success

    def enable_letsencrypt(self, app_name: str, email: str | None = None) -> bool:
        """Enable Let's Encrypt SSL for an app."""
        email_flag = f"--email {email}" if email else ""
        status, _, _ = self.execute(f"letsencrypt:enable {app_name} {email_flag}")
        return status == 0

    def disable_letsencrypt(self, app_name: str) -> bool:
        """Disable Let's Encrypt SSL for an app."""
        status, _, _ = self.execute(f"letsencrypt:disable {app_name}")
        return status == 0

    def set_resource_limits(self, app_name: str, memory: str, cpu: str) -> bool:
        """Set resource limits for an app (requires resource plugin)."""
        status1, _, _ = self.execute(f"resource:limit {app_name} --memory {memory}")
        status2, _, _ = self.execute(f"resource:limit {app_name} --cpu {cpu}")
        return status1 == 0 and status2 == 0

    def deploy_image(self, app_name: str, image: str) -> bool:
        """Deploy a Docker image to an app."""
        status, _, _ = self.execute(f"git:from-image {app_name} {image}")
        return status == 0

    def create_storage(self, app_name: str, host_path: str, mount_path: str) -> bool:
        """Create persistent storage for an app."""
        status, _, _ = self.execute(
            f"storage:mount {app_name} {host_path}:{mount_path}",
        )
        return status == 0

    def remove_storage(self, app_name: str, host_path: str, mount_path: str) -> bool:
        """Remove persistent storage from an app."""
        status, _, _ = self.execute(
            f"storage:unmount {app_name} {host_path}:{mount_path}",
        )
        return status == 0

    def scale_app(self, app_name: str, process_type: str, count: int) -> bool:
        """Scale app processes."""
        status, _, _ = self.execute(f"ps:scale {app_name} {process_type}={count}")
        return status == 0

    def restart_app(self, app_name: str) -> bool:
        """Restart an app."""
        status, _, _ = self.execute(f"ps:restart {app_name}")
        return status == 0

    def stop_app(self, app_name: str) -> bool:
        """Stop an app."""
        status, _, _ = self.execute(f"ps:stop {app_name}")
        return status == 0

    def start_app(self, app_name: str) -> bool:
        """Start an app."""
        status, _, _ = self.execute(f"ps:start {app_name}")
        return status == 0

    def get_app_info(self, app_name: str) -> dict:
        """Get detailed information about an app."""
        status, output, _ = self.execute(f"apps:report {app_name}")
        if status != 0:
            return {}

        info = {}
        for line in output.strip().split("\n"):
            if ":" in line:
                parts = line.split(":", 1)
                if len(parts) == 2:
                    key = parts[0].strip()
                    value = parts[1].strip()
                    info[key] = value

        return info

    def logs(self, app_name: str, lines: int = 100) -> str:
        """Get app logs."""
        status, output, _ = self.execute(f"logs {app_name} -n {lines}")
        return output if status == 0 else ""


# Singleton instance
dokku_client = DokkuClient()


def test_connection() -> bool:
    """Test Dokku connection."""
    try:
        if not dokku_client.ssh:
            return False

        status, output, _ = dokku_client.execute("version")
        if status == 0:
            logger.info(f"Dokku version: {output.strip()}")
            return True
        return False
    except Exception as e:
        logger.error(f"Dokku connection test failed: {e}")
        return False
