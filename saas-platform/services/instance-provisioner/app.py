"""Simple Kubernetes/Helm provisioner for MindRoom instances."""

import json
import logging
import os
import secrets
import string
import subprocess

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="MindRoom Instance Provisioner",
    description="Provisions MindRoom instances on Kubernetes using Helm",
    version="2.0.0",
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security
security = HTTPBearer()

# Get the API key from environment (required)
PROVISIONER_API_KEY = os.getenv("PROVISIONER_API_KEY")
if not PROVISIONER_API_KEY:
    logger.warning("PROVISIONER_API_KEY not set - using a temporary key for development")
    PROVISIONER_API_KEY = "development_only_key_not_for_production"


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> bool:  # noqa: B008
    """Verify the bearer token."""
    if credentials.credentials != PROVISIONER_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return True


# Models
class ProvisionRequest(BaseModel):
    """Request model for provisioning an instance."""

    subscription_id: str
    account_id: str
    tier: str = "starter"
    custom_domain: str | None = None


class ProvisionResponse(BaseModel):
    """Response model for provisioning an instance."""

    success: bool
    customer_id: str
    frontend_url: str
    api_url: str
    matrix_url: str
    message: str
    auth_token: str | None = None  # Simple auth token for accessing the instance


class DeprovisionRequest(BaseModel):
    """Request model for deprovisioning an instance."""

    subscription_id: str
    customer_id: str


class DeprovisionResponse(BaseModel):
    """Response model for deprovisioning an instance."""

    success: bool
    message: str


# Helper functions
def generate_customer_id(subscription_id: str) -> str:
    """Generate a clean customer ID from subscription ID."""
    # Take first 8 chars and clean
    clean_id = subscription_id[:8].lower()
    clean_id = "".join(c if c.isalnum() else "" for c in clean_id)
    if not clean_id:
        # Fallback to random ID
        clean_id = "".join(secrets.choice(string.ascii_lowercase) for _ in range(8))
    return clean_id


def generate_password() -> str:
    """Generate secure password."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(20))


def run_command(cmd: list) -> tuple[bool, str]:
    """Run a shell command and return success status and output."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        logger.exception(f"Command failed: {' '.join(cmd)}\nError: {e.stderr}")
        return False, e.stderr
    else:
        logger.info(f"Command succeeded: {' '.join(cmd)}")
        return True, result.stdout


def get_storage_by_tier(tier: str) -> str:
    """Get storage size based on tier."""
    storage_map = {
        "free": "1Gi",
        "starter": "5Gi",
        "professional": "20Gi",
        "enterprise": "100Gi",
    }
    return storage_map.get(tier, "5Gi")


# Routes
@app.get("/")
async def root() -> dict:
    """Root endpoint."""
    return {
        "service": "MindRoom Instance Provisioner",
        "status": "operational",
        "version": "2.0.0",
        "mode": "kubernetes/helm",
    }


@app.get("/health")
async def health() -> dict:
    """Health check."""
    # Check if kubectl is available
    success, _ = run_command(["kubectl", "version", "--client"])

    # Check if helm is available
    helm_success, _ = run_command(["helm", "version"])

    if success and helm_success:
        return {"status": "healthy", "kubectl": "available", "helm": "available"}
    raise HTTPException(503, "kubectl or helm not available")


@app.post("/api/v1/provision")
async def provision_instance(
    request: ProvisionRequest,
    authenticated: bool = Depends(verify_token),  # noqa: ARG001, FAST002
) -> ProvisionResponse:
    """Provision a new MindRoom instance using Helm."""
    customer_id = generate_customer_id(request.subscription_id)
    matrix_password = generate_password()
    instance_auth_token = generate_password()  # Simple auth token for the instance

    # Configuration
    namespace = "mindroom-instances"
    base_domain = os.getenv("BASE_DOMAIN", "staging.mindroom.chat")
    registry = os.getenv("REGISTRY", "git.nijho.lt/basnijholt")
    helm_chart_path = "/app/k8s/instance"  # Will be mounted from host

    logger.info(f"Provisioning instance for customer: {customer_id}")

    try:
        # Create namespace if it doesn't exist
        try:
            run_command(
                [
                    "kubectl",
                    "create",
                    "namespace",
                    namespace,
                ],
            )
            logger.info(f"Created namespace {namespace}")
        except Exception:
            logger.info(f"Namespace {namespace} already exists")

        # Create image pull secret for private registry
        gitea_username = os.getenv("GITEA_USERNAME", "basnijholt")
        gitea_password = os.getenv("GITEA_PASSWORD", "c9433aa79a9805574f9eb0768f2b71a82bc54123")

        logger.info(f"Creating image pull secret in namespace {namespace}")
        # Create the secret (kubectl will error if already exists, we catch that)
        secret_cmd = [
            "kubectl",
            "create",
            "secret",
            "docker-registry",
            "gitea-registry",
            f"--docker-server={registry.split('/')[0]}",  # Extract registry URL
            f"--docker-username={gitea_username}",
            f"--docker-password={gitea_password}",
            f"--namespace={namespace}",
        ]
        try:
            run_command(secret_cmd)
            logger.info("Image pull secret created successfully")
        except Exception as e:
            # Secret might already exist, that's fine
            if "already exists" in str(e):
                logger.info("Image pull secret already exists")
            else:
                logger.warning(f"Could not create image pull secret: {e}")

        # Prepare Helm values
        helm_cmd = [
            "helm",
            "install",
            f"mindroom-{customer_id}",
            helm_chart_path,
            "--namespace",
            namespace,
            "--create-namespace",
            "--set",
            f"customer={customer_id}",
            "--set",
            f"baseDomain={base_domain}",
            "--set",
            f"mindroom_image={registry}/mindroom-frontend:latest",
            "--set",
            "synapse_image=matrixdotorg/synapse:latest",
            "--set",
            f"storage={get_storage_by_tier(request.tier)}",
            "--set",
            f"matrix_admin_password={matrix_password}",
            "--set",
            f"instance_auth_token={instance_auth_token}",
        ]

        # Add API keys if available
        if os.getenv("OPENAI_API_KEY"):
            helm_cmd.extend(["--set", f"openai_key={os.getenv('OPENAI_API_KEY')}"])
        if os.getenv("ANTHROPIC_API_KEY"):
            helm_cmd.extend(["--set", f"anthropic_key={os.getenv('ANTHROPIC_API_KEY')}"])

        # Install the Helm chart
        success, output = run_command(helm_cmd)

        if success:
            logger.info(f"Successfully provisioned instance for {customer_id}")

            # Return URLs and auth token
            return ProvisionResponse(
                success=True,
                customer_id=customer_id,
                frontend_url=f"https://{customer_id}.{base_domain}",
                api_url=f"https://{customer_id}.api.{base_domain}",
                matrix_url=f"https://{customer_id}.matrix.{base_domain}",
                message=f"Instance provisioned successfully for {customer_id}",
                auth_token=instance_auth_token,  # Return the auth token
            )
        raise HTTPException(500, f"Helm install failed: {output}")  # noqa: TRY301

    except Exception as e:
        logger.exception("Provisioning failed")

        # Try to cleanup
        run_command(
            [
                "helm",
                "uninstall",
                f"mindroom-{customer_id}",
                "--namespace",
                namespace,
            ],
        )

        raise HTTPException(500, f"Provisioning failed: {e!s}") from e


@app.delete("/api/v1/deprovision")
async def deprovision_instance(
    request: DeprovisionRequest,
    authenticated: bool = Depends(verify_token),  # noqa: ARG001, FAST002
) -> DeprovisionResponse:
    """Remove a MindRoom instance."""
    customer_id = request.customer_id
    namespace = "mindroom-instances"

    logger.info(f"Deprovisioning instance for customer: {customer_id}")

    try:
        # Uninstall the Helm release
        success, output = run_command(
            [
                "helm",
                "uninstall",
                f"mindroom-{customer_id}",
                "--namespace",
                namespace,
            ],
        )

        if success:
            logger.info(f"Successfully deprovisioned instance for {customer_id}")
            return DeprovisionResponse(
                success=True,
                message=f"Instance deprovisioned successfully for {customer_id}",
            )
        # If it doesn't exist, that's okay
        if "not found" in output.lower():
            return DeprovisionResponse(
                success=True,
                message=f"Instance was already deprovisioned for {customer_id}",
            )
        raise HTTPException(500, f"Helm uninstall failed: {output}")  # noqa: TRY301

    except Exception as e:
        logger.exception("Deprovisioning failed")
        raise HTTPException(500, f"Deprovisioning failed: {e!s}") from e


@app.get("/api/v1/status/{customer_id}")
async def get_instance_status(customer_id: str) -> dict:
    """Check the status of an instance."""
    namespace = "mindroom-instances"

    # Check Helm release status
    success, output = run_command(
        [
            "helm",
            "status",
            f"mindroom-{customer_id}",
            "--namespace",
            namespace,
            "-o",
            "json",
        ],
    )

    if success:
        try:
            status_data = json.loads(output)
            return {
                "exists": True,
                "customer_id": customer_id,
                "status": status_data.get("info", {}).get("status", "unknown"),
                "namespace": namespace,
            }
        except json.JSONDecodeError:
            return {"exists": True, "customer_id": customer_id, "status": "unknown"}
    else:
        return {"exists": False, "customer_id": customer_id}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8002)  # noqa: S104
