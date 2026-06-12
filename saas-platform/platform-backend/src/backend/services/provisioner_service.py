"""Instance provisioning service shared by provisioner, admin, and user instance routes.

Routes own auth and request parsing; this module owns the provisioning business logic:
secret derivation, Helm args assembly, OpenRouter key lifecycle, Kubernetes secret
management, deployment readiness, and the core instance operations.
"""

import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import partial
from typing import Any
from uuid import UUID

import anyio
from backend.config import (
    INSTANCE_BASE_DOMAIN,
    INSTANCE_CREDENTIALS_ENCRYPTION_SECRET,
    INSTANCE_IMAGE_PULL_SECRET_NAMES,
    INSTANCE_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS,
    INSTANCE_MATRIX_OIDC_CLIENT_ID,
    INSTANCE_MATRIX_OIDC_CLIENT_SECRET,
    INSTANCE_MATRIX_OIDC_ENABLED,
    INSTANCE_MATRIX_OIDC_ISSUER,
    INSTANCE_MINDROOM_IMAGE,
    INSTANCE_MINDROOM_IMAGE_PULL_POLICY,
    INSTANCE_SYNAPSE_IMAGE,
    INSTANCE_SYNAPSE_IMAGE_PULL_POLICY,
    INSTANCE_STORAGE_CLASS_NAME,
    INSTANCE_TRUSTED_UPSTREAM_AUTH_ENABLED,
    INSTANCE_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE,
    INSTANCE_TRUSTED_UPSTREAM_EMAIL_HEADER,
    INSTANCE_TRUSTED_UPSTREAM_JWKS_URL,
    INSTANCE_TRUSTED_UPSTREAM_JWT_AUDIENCE,
    INSTANCE_TRUSTED_UPSTREAM_JWT_EMAIL_CLAIM,
    INSTANCE_TRUSTED_UPSTREAM_JWT_HEADER,
    INSTANCE_TRUSTED_UPSTREAM_JWT_ISSUER,
    INSTANCE_TRUSTED_UPSTREAM_JWT_MATRIX_USER_ID_CLAIM,
    INSTANCE_TRUSTED_UPSTREAM_JWT_USER_ID_CLAIM,
    INSTANCE_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER,
    INSTANCE_TRUSTED_UPSTREAM_REQUIRE_JWT,
    INSTANCE_TRUSTED_UPSTREAM_USER_ID_HEADER,
    OPENROUTER_PROVISIONING_API_KEY,
    PLATFORM_DOMAIN,
    PROVISIONER_API_KEY,
    SANDBOX_PROXY_TOKEN,
    SUPABASE_ANON_KEY,
    SUPABASE_SERVICE_KEY,
    SUPABASE_URL,
    logger,
)
from backend.db_utils import update_instance_status
from backend.deps import ensure_supabase
from backend.k8s import (
    check_deployment_exists,
    instance_deployment_ref,
    run_kubectl,
    tenant_start_deployment_refs,
    tenant_stop_deployment_refs,
    wait_for_deployment_ready,
)
from backend.models import SyncUpdateOut
from backend.openrouter import (
    CreatedOpenRouterKey,
    OpenRouterConfigurationError,
    OpenRouterError,
    OpenRouterKeyPlan,
    create_openrouter_key,
    delete_openrouter_key,
)
from backend.pricing import get_plan_details
from backend.process import run_helm
from fastapi import BackgroundTasks, HTTPException

_MATRIX_LOCALPART_ALLOWED_CHARS = frozenset("_-./=+abcdefghijklmnopqrstuvwxyz0123456789")
_HOSTED_MATRIX_AUTO_JOIN_ROOM_KEYS = (
    "analysis",
    "automation",
    "business",
    "communication",
    "dev",
    "docs",
    "finance",
    "help",
    "home",
    "lobby",
    "news",
    "ops",
    "personal",
    "productivity",
    "research",
    "science",
)

_RESOURCE_PROFILE_HELM_VALUES = {
    "pro": {
        "storage": "25Gi",
        "mindroomResources.requests.memory": "1Gi",
        "mindroomResources.requests.cpu": "500m",
        "mindroomResources.limits.memory": "4Gi",
        "mindroomResources.limits.cpu": "2000m",
        "synapseResources.requests.memory": "1Gi",
        "synapseResources.requests.cpu": "500m",
        "synapseResources.limits.memory": "4Gi",
        "synapseResources.limits.cpu": "2000m",
        "sandboxRunnerResources.requests.memory": "512Mi",
        "sandboxRunnerResources.requests.cpu": "250m",
        "sandboxRunnerResources.limits.memory": "2Gi",
        "sandboxRunnerResources.limits.cpu": "1000m",
    }
}

_INSTANCES_NAMESPACE = "mindroom-instances"


def _env_flag_enabled(value: str) -> bool:
    """Return whether an env-style flag value is explicitly enabled."""
    return value.strip().lower() in {"1", "true", "yes", "on"}


async def _background_mark_running_when_ready(instance_id: str, namespace: str = _INSTANCES_NAMESPACE) -> None:
    """Background task: wait longer and mark instance running when ready."""
    try:
        ready = await wait_for_deployment_ready(instance_id, namespace=namespace, timeout_seconds=600)
        if ready:
            try:
                sb = ensure_supabase()
                sb.table("instances").update({"status": "running", "updated_at": datetime.now(UTC).isoformat()}).eq(
                    "instance_id", instance_id
                ).execute()
            except Exception:
                logger.warning("Background update: failed to mark instance %s as running", instance_id)
    except Exception:
        logger.exception("Background readiness wait failed for instance %s", instance_id)


async def _run_kubectl_for_deployments(
    kubectl_args_prefix: list[str], deployment_refs: tuple[str, ...], *, namespace: str
) -> str:
    """Run one kubectl command per deployment and fail on the first error."""
    last_output = ""
    for deployment_ref in deployment_refs:
        code, out, err = await run_kubectl([*kubectl_args_prefix, deployment_ref], namespace=namespace)
        if code != 0:
            msg = f"kubectl command failed for {deployment_ref}: {err or out}"
            raise RuntimeError(msg)
        last_output = out
    return last_output


async def _scale_tenant_deployments(deployment_refs: tuple[str, ...], replicas: int, *, namespace: str) -> str:
    """Scale each tenant deployment to the requested replica count."""
    last_output = ""
    for deployment_ref in deployment_refs:
        code, out, err = await run_kubectl(["scale", deployment_ref, f"--replicas={replicas}"], namespace=namespace)
        if code != 0:
            msg = f"kubectl command failed for {deployment_ref}: {err or out}"
            raise RuntimeError(msg)
        last_output = out
    return last_output


def _instance_credentials_encryption_key(instance_id: str) -> str:
    """Derive a stable per-instance credential encryption key."""
    return _stable_instance_secret("instance-credentials", instance_id)


def _instance_matrix_registration_shared_secret(instance_id: str) -> str:
    """Derive a stable per-instance Synapse registration shared secret."""
    return _stable_instance_secret("matrix-registration", instance_id)


def _stable_instance_secret(purpose: str, instance_id: str) -> str:
    """Derive one stable per-instance secret from the platform root secret."""
    root_secret = (INSTANCE_CREDENTIALS_ENCRYPTION_SECRET or PROVISIONER_API_KEY).strip()
    if not root_secret:
        msg = "INSTANCE_CREDENTIALS_ENCRYPTION_SECRET or PROVISIONER_API_KEY must be configured"
        raise HTTPException(status_code=500, detail=msg)
    digest = hmac.digest(
        root_secret.encode("utf-8"), f"mindroom.{purpose}.v1:{instance_id}".encode("utf-8"), hashlib.sha256
    )
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _matrix_localpart_from_email(email: str) -> str:
    """Return the Matrix localpart Synapse derives from our OIDC email template."""
    email_localpart = email.strip().lower().rsplit("@", maxsplit=1)[0]
    if not email_localpart:
        msg = "Account email is required to configure Matrix owner access"
        raise HTTPException(status_code=500, detail=msg)

    mapped = "".join(
        chr(byte) if chr(byte) in _MATRIX_LOCALPART_ALLOWED_CHARS and chr(byte) != "=" else f"={byte:02x}"
        for byte in email_localpart.encode("utf-8")
    )
    return f"=5f{mapped[1:]}" if mapped.startswith("_") else mapped


def _owner_matrix_user_id_from_email(email: str, *, instance_id: str, base_domain: str) -> str:
    """Build the hosted tenant owner MXID from a platform account email."""
    return f"@{_matrix_localpart_from_email(email)}:{instance_id}.{base_domain}"


def _owner_matrix_user_id_for_account(sb: Any, *, account_id: Any, instance_id: str, base_domain: str) -> str | None:
    """Return the MXID that should be authorized for the tenant owner."""
    if not account_id:
        return None
    try:
        normalized_account_id = str(UUID(str(account_id)))
    except ValueError:
        logger.warning("Skipping tenant owner Matrix user for non-UUID account_id %s", account_id)
        return None

    result = sb.table("accounts").select("email").eq("id", normalized_account_id).limit(1).execute()
    row = result.data[0] if result.data else None
    email = row.get("email") if isinstance(row, Mapping) else None
    if not isinstance(email, str) or not email.strip():
        msg = f"Account {normalized_account_id} needs an email before provisioning Matrix owner access"
        raise HTTPException(status_code=500, detail=msg)
    return _owner_matrix_user_id_from_email(email, instance_id=instance_id, base_domain=base_domain)


def _append_matrix_oidc_helm_args(helm_args: list[str]) -> None:
    """Forward hosted Matrix OIDC settings to the instance chart."""
    if INSTANCE_MATRIX_OIDC_ENABLED:
        helm_args += ["--set", f"matrixOidc.enabled={INSTANCE_MATRIX_OIDC_ENABLED}"]
    if _env_flag_enabled(INSTANCE_MATRIX_OIDC_ENABLED):
        helm_args += [
            "--set",
            "matrixRoomAccess.mode=multi_user",
            "--set",
            "matrixRoomAccess.multiUserJoinRule=public",
            "--set",
            "matrixRoomAccess.publishToRoomDirectory=false",
            "--set",
            "matrixRoomAccess.reconcileExistingRooms=true",
        ]
        for index, room_key in enumerate(_HOSTED_MATRIX_AUTO_JOIN_ROOM_KEYS):
            helm_args += ["--set-string", f"matrixAutoJoinRoomKeys[{index}]={room_key}"]
    if INSTANCE_MATRIX_OIDC_ISSUER:
        helm_args += ["--set", f"matrixOidc.issuer={INSTANCE_MATRIX_OIDC_ISSUER}"]
    if INSTANCE_MATRIX_OIDC_CLIENT_ID:
        helm_args += ["--set", f"matrixOidc.clientId={INSTANCE_MATRIX_OIDC_CLIENT_ID}"]


def _append_image_pull_secret_helm_args(helm_args: list[str], secret_names: str) -> None:
    """Forward configured imagePullSecrets to the instance chart."""
    names = [name.strip() for name in secret_names.split(",") if name.strip()]
    for index, name in enumerate(names):
        helm_args += ["--set-string", f"imagePullSecrets[{index}].name={name}"]


def _append_resource_profile_helm_args(helm_args: list[str], resource_profile: str) -> None:
    """Forward configured resource profile overrides to the instance chart."""
    for key, value in _RESOURCE_PROFILE_HELM_VALUES.get(resource_profile, {}).items():
        helm_args += ["--set", f"{key}={value}"]


def _instance_secret_name(instance_id: str) -> str:
    """Return the externally managed Secret name for an instance."""
    return f"mindroom-api-keys-{instance_id}"


def _instance_secret_hash(secret_data: dict[str, str]) -> str:
    """Return a deterministic rollout hash for instance secret contents."""
    encoded = json.dumps(secret_data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _apply_instance_secret(instance_id: str, namespace: str, secret_data: dict[str, str]) -> str:
    """Apply instance secrets outside Helm so release values stay non-sensitive."""
    secret_name = _instance_secret_name(instance_id)
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": secret_name, "namespace": namespace},
        "type": "Opaque",
        "stringData": secret_data,
    }
    fd, path = tempfile.mkstemp(prefix=f"{secret_name}-", suffix=".json", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        code, out, err = await run_kubectl(["apply", "-f", path], namespace=namespace)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
    if code != 0:
        msg = f"Failed to apply instance Secret {secret_name}: {err or out}"
        raise RuntimeError(msg)
    return _instance_secret_hash(secret_data)


async def _existing_instance_secret_value(instance_id: str, namespace: str, key: str) -> str | None:
    """Return an existing instance Secret value when present."""
    secret_name = _instance_secret_name(instance_id)
    code, out, err = await run_kubectl(
        ["get", "secret", secret_name, "--ignore-not-found", f"-o=jsonpath={{.data.{key}}}"], namespace=namespace
    )
    if code != 0:
        msg = f"Failed to inspect existing Secret value {key} for instance {instance_id}: {err or out}"
        raise HTTPException(status_code=500, detail=msg)
    encoded_value = out.strip()
    if not encoded_value:
        return None
    try:
        value = base64.b64decode(encoded_value, validate=True).decode("utf-8").strip()
    except (binascii.Error, UnicodeDecodeError) as exc:
        msg = f"Instance {instance_id} has an invalid {key} Secret value"
        raise HTTPException(status_code=500, detail=msg) from exc
    return value or None


async def _existing_instance_credentials_encryption_key(instance_id: str, namespace: str) -> str | None:
    """Return the existing credential encryption key from an instance Secret when present."""
    return await _existing_instance_secret_value(instance_id, namespace, "credentials_encryption_key")


async def _provision_credentials_encryption_key(
    *, customer_id: str, existing_instance_id: Any, data: dict, namespace: str
) -> str:
    """Return the instance chart credential encryption key value for this provision run."""
    existing_key = (
        await _existing_instance_credentials_encryption_key(customer_id, namespace) if existing_instance_id else None
    )
    if existing_key is not None:
        return existing_key
    if not existing_instance_id or data.get("enable_credentials_encryption") is True:
        return _instance_credentials_encryption_key(customer_id)
    return ""


async def _existing_instance_storage_class_name(instance_id: str, namespace: str) -> str | None:
    """Return the bound PVC storage class for an existing instance."""
    pvc_names = [f"mindroom-storage-{instance_id}", f"synapse-storage-{instance_id}"]
    code, out, err = await run_kubectl(
        ["get", "pvc", *pvc_names, "--ignore-not-found", "-o", "json"], namespace=namespace
    )
    if code != 0:
        msg = f"Failed to inspect existing PVC storage class for instance {instance_id}: {err or out}"
        raise HTTPException(status_code=500, detail=msg)
    if not out.strip():
        return None

    payload = json.loads(out)
    storage_classes = {
        item.get("spec", {}).get("storageClassName", "").strip()
        for item in payload.get("items", [])
        if item.get("spec", {}).get("storageClassName", "").strip()
    }
    if not storage_classes:
        return None
    if len(storage_classes) > 1:
        msg = f"Instance {instance_id} has PVCs with different storage classes: {', '.join(sorted(storage_classes))}"
        raise HTTPException(status_code=500, detail=msg)
    return storage_classes.pop()


def _openrouter_key_name(*, tier: str, account_id: Any, instance_id: str) -> str:
    """Return a stable human-readable OpenRouter key name."""
    return f"MindRoom {tier} account {account_id} instance {instance_id}"


def _matching_openrouter_metadata(row: Mapping[str, Any] | None, monthly_limit_usd: int) -> bool:
    """Return whether stored OpenRouter metadata matches the requested budget."""
    if not row:
        return False
    try:
        stored_limit = int(row.get("openrouter_key_limit_usd") or 0)
    except (TypeError, ValueError):
        return False
    return (
        row.get("openrouter_key_hash") is not None
        and row.get("openrouter_key_limit_reset") == "monthly"
        and stored_limit == monthly_limit_usd
    )


def _stored_openrouter_key_hash(row: Mapping[str, Any] | None) -> str | None:
    """Return a stored OpenRouter key hash when it is usable for lifecycle cleanup."""
    if not row:
        return None
    key_hash = row.get("openrouter_key_hash")
    if isinstance(key_hash, str) and key_hash.strip():
        return key_hash.strip()
    return None


def _persist_openrouter_key_metadata(sb: Any, instance_id: str, created_key: CreatedOpenRouterKey) -> None:
    """Persist non-secret OpenRouter key metadata for reuse and audit."""
    sb.table("instances").update(
        {
            "openrouter_key_hash": created_key.hash,
            "openrouter_key_label": created_key.label,
            "openrouter_key_limit_usd": created_key.limit_usd,
            "openrouter_key_limit_reset": created_key.limit_reset,
            "openrouter_key_created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
    ).eq("instance_id", instance_id).execute()


def _mark_instance_provision_error(sb: Any, instance_id: str, context: str) -> None:
    """Mark a failed provisioning attempt as error without hiding the original failure."""
    try:
        sb.table("instances").update({"status": "error", "updated_at": datetime.now(UTC).isoformat()}).eq(
            "instance_id", instance_id
        ).execute()
    except Exception:
        logger.warning("Failed to update instance status to error after %s", context)


async def _provision_openrouter_key(
    *,
    sb: Any,
    account_id: Any,
    instance_id: str,
    tier: str,
    existing_instance_row: Mapping[str, Any] | None,
    namespace: str,
) -> str:
    """Return the OpenRouter key value this tenant instance should receive."""
    plan = get_plan_details(tier)
    monthly_limit_usd = plan.included_ai_budget_usd if plan else 0
    if monthly_limit_usd <= 0:
        return ""

    if _matching_openrouter_metadata(existing_instance_row, monthly_limit_usd):
        existing_key = await _existing_instance_secret_value(instance_id, namespace, "openrouter_key")
        if existing_key:
            return existing_key

    superseded_key_hash = _stored_openrouter_key_hash(existing_instance_row)
    create_key = partial(
        create_openrouter_key,
        management_api_key=OPENROUTER_PROVISIONING_API_KEY,
        plan=OpenRouterKeyPlan(
            name=_openrouter_key_name(tier=tier, account_id=account_id, instance_id=instance_id),
            monthly_limit_usd=monthly_limit_usd,
        ),
    )
    try:
        created_key = await anyio.to_thread.run_sync(create_key)
    except OpenRouterConfigurationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except OpenRouterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    metadata_persisted = False
    try:
        await anyio.to_thread.run_sync(partial(_persist_openrouter_key_metadata, sb, instance_id, created_key))
        metadata_persisted = True
    except Exception:
        logger.exception("Failed to persist OpenRouter key metadata for instance %s", instance_id)
    if metadata_persisted and superseded_key_hash and superseded_key_hash != created_key.hash:
        try:
            await anyio.to_thread.run_sync(
                partial(
                    delete_openrouter_key,
                    management_api_key=OPENROUTER_PROVISIONING_API_KEY,
                    key_hash=superseded_key_hash,
                )
            )
        except OpenRouterError:
            logger.warning(
                "Failed to revoke superseded OpenRouter key %s for instance %s",
                superseded_key_hash,
                instance_id,
                exc_info=True,
            )
    return created_key.key


async def provision_instance(  # noqa: C901, PLR0912, PLR0915
    sb: Any, *, data: dict, background_tasks: BackgroundTasks | None
) -> dict[str, Any]:
    """Provision (or re-provision) a tenant instance and return the portal response payload."""
    subscription_id = data.get("subscription_id")
    account_id = data.get("account_id")
    tier = data.get("tier", "free")
    existing_instance_id = data.get("instance_id")  # For re-provisioning

    # If re-provisioning, update existing instance; otherwise insert new
    if existing_instance_id:
        customer_id = str(existing_instance_id)
        try:
            update_res = (
                sb.table("instances")
                .update({"status": "provisioning", "updated_at": datetime.now(UTC).isoformat()})
                .eq("instance_id", customer_id)
                .execute()
            )
            if not update_res.data:
                msg = f"Instance {customer_id} not found"
                raise HTTPException(status_code=404, detail=msg)  # noqa: TRY301
            existing_instance_row = update_res.data[0]
            logger.info("Re-provisioning existing instance %s", customer_id)
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Failed to update instance for re-provisioning")
            raise HTTPException(status_code=500, detail=f"Failed to update instance: {e!s}") from e
    else:
        # Insert instance first to get a generated numeric instance_id (as text)
        try:
            now = datetime.now(UTC).isoformat()
            insert_res = (
                sb.table("instances")
                .insert(
                    {
                        "subscription_id": subscription_id,
                        "account_id": account_id,
                        "status": "provisioning",
                        "tier": tier,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                .execute()
            )
            if not insert_res.data:
                msg = "Failed to insert instance"
                raise HTTPException(status_code=500, detail=msg)  # noqa: TRY301
            customer_id = insert_res.data[0]["instance_id"]
            existing_instance_row = insert_res.data[0]
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Failed to insert instance")
            raise HTTPException(status_code=500, detail=f"Failed to insert instance: {e!s}") from e

    helm_release_name = f"instance-{customer_id}"
    logger.info("Provisioning instance for subscription %s, new id: %s, tier: %s", subscription_id, customer_id, tier)

    namespace = _INSTANCES_NAMESPACE
    try:
        await run_kubectl(["create", "namespace", namespace])
    except FileNotFoundError:
        error_msg = "Kubectl command not found. Kubernetes provisioning not available in this environment."
        logger.exception(error_msg)
        raise HTTPException(status_code=503, detail=error_msg) from None
    except Exception as e:
        logger.warning("Could not create namespace (may already exist): %s", e)

    logger.info("Deploying instance %s to namespace %s", customer_id, namespace)

    # Compute URLs and persist them (subdomain is set via trigger if null)
    base_domain = INSTANCE_BASE_DOMAIN or PLATFORM_DOMAIN
    frontend_url = f"https://{customer_id}.{base_domain}"
    api_url = f"https://{customer_id}.api.{base_domain}"
    matrix_url = f"https://{customer_id}.matrix.{base_domain}"
    owner_matrix_user_id = _owner_matrix_user_id_for_account(
        sb, account_id=account_id, instance_id=customer_id, base_domain=base_domain
    )
    try:
        sb.table("instances").update(
            {
                "instance_url": frontend_url,
                "frontend_url": frontend_url,
                "backend_url": api_url,
                "api_url": api_url,
                "matrix_url": matrix_url,
                "matrix_server_url": matrix_url,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        ).eq("instance_id", customer_id).execute()
    except Exception:
        logger.warning("Failed to update URLs for instance %s", customer_id)

    # Keep this non-empty so shell/file/python proxying doesn't fail at runtime.
    sandbox_proxy_token = SANDBOX_PROXY_TOKEN or secrets.token_hex(32)
    # Existing instances may have plaintext credential files; preserve their current encryption state.
    credentials_encryption_key = await _provision_credentials_encryption_key(
        customer_id=customer_id, existing_instance_id=existing_instance_id, data=data, namespace=namespace
    )
    storage_class_name = INSTANCE_STORAGE_CLASS_NAME
    if existing_instance_id:
        storage_class_name = (
            await _existing_instance_storage_class_name(customer_id, namespace)
        ) or INSTANCE_STORAGE_CLASS_NAME
    try:
        openrouter_key = await _provision_openrouter_key(
            sb=sb,
            account_id=account_id,
            instance_id=customer_id,
            tier=tier,
            existing_instance_row=existing_instance_row,
            namespace=namespace,
        )
        # User BYOK credentials live in tenant storage; hosted budgets use only a scoped OpenRouter key.
        instance_secret_data = {
            "openai_key": "",
            "anthropic_key": "",
            "openrouter_key": openrouter_key,
            "google_key": "",
            "deepseek_key": "",
            "supabase_service_key": SUPABASE_SERVICE_KEY or "",
            "sandbox_proxy_token": sandbox_proxy_token,
            "credentials_encryption_key": credentials_encryption_key,
            "matrix_oidc_client_secret": INSTANCE_MATRIX_OIDC_CLIENT_SECRET or "",
            "matrix_registration_shared_secret": _instance_matrix_registration_shared_secret(customer_id),
        }
        instance_secret_hash = _instance_secret_hash(instance_secret_data)
        # Use upgrade --install to handle both new and re-provisioning cases
        helm_args = [
            "upgrade",
            "--install",
            helm_release_name,
            "/app/k8s/instance/",
            "--namespace",
            namespace,
            "--create-namespace",
            "--history-max",
            "2",
            "--set",
            f"customer={customer_id}",
            "--set",
            f"baseDomain={base_domain}",
            "--set",
            f"accountId={account_id}",
            "--set",
            f"supabaseUrl={SUPABASE_URL or ''}",
            "--set",
            f"supabaseAnonKey={SUPABASE_ANON_KEY or ''}",
            "--set",
            "instanceSecrets.create=false",
            "--set",
            f"instanceSecrets.name={_instance_secret_name(customer_id)}",
            "--set-string",
            f"instanceSecrets.hash={instance_secret_hash}",
        ]
        if storage_class_name:
            helm_args += ["--set", f"storageClassName={storage_class_name}"]
        plan = get_plan_details(tier)
        if plan:
            _append_resource_profile_helm_args(helm_args, plan.resource_profile)
        if INSTANCE_MINDROOM_IMAGE:
            helm_args += ["--set", f"mindroom_image={INSTANCE_MINDROOM_IMAGE}"]
        if INSTANCE_MINDROOM_IMAGE_PULL_POLICY:
            helm_args += ["--set", f"mindroom_image_pull_policy={INSTANCE_MINDROOM_IMAGE_PULL_POLICY}"]
        if owner_matrix_user_id:
            helm_args += ["--set-string", f"authorizationGlobalUsers[0]={owner_matrix_user_id}"]
        _append_image_pull_secret_helm_args(helm_args, INSTANCE_IMAGE_PULL_SECRET_NAMES)
        if INSTANCE_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS:
            helm_args += [
                "--set",
                (f"matrixHomeserverStartupTimeoutSeconds={INSTANCE_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS}"),
            ]
        if INSTANCE_SYNAPSE_IMAGE:
            helm_args += ["--set", f"synapse_image={INSTANCE_SYNAPSE_IMAGE}"]
        if INSTANCE_SYNAPSE_IMAGE_PULL_POLICY:
            helm_args += ["--set", f"synapse_image_pull_policy={INSTANCE_SYNAPSE_IMAGE_PULL_POLICY}"]
        if INSTANCE_TRUSTED_UPSTREAM_AUTH_ENABLED:
            helm_args += ["--set", f"trustedUpstreamAuth.enabled={INSTANCE_TRUSTED_UPSTREAM_AUTH_ENABLED}"]
        if INSTANCE_TRUSTED_UPSTREAM_USER_ID_HEADER:
            helm_args += ["--set", f"trustedUpstreamAuth.userIdHeader={INSTANCE_TRUSTED_UPSTREAM_USER_ID_HEADER}"]
        if INSTANCE_TRUSTED_UPSTREAM_EMAIL_HEADER:
            helm_args += ["--set", f"trustedUpstreamAuth.emailHeader={INSTANCE_TRUSTED_UPSTREAM_EMAIL_HEADER}"]
        if INSTANCE_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER:
            helm_args += [
                "--set",
                f"trustedUpstreamAuth.matrixUserIdHeader={INSTANCE_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER}",
            ]
        if INSTANCE_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE:
            helm_args += [
                "--set",
                (
                    "trustedUpstreamAuth.emailToMatrixUserIdTemplate="
                    f"{INSTANCE_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE}"
                ),
            ]
        if INSTANCE_TRUSTED_UPSTREAM_REQUIRE_JWT:
            helm_args += ["--set", f"trustedUpstreamAuth.requireJwt={INSTANCE_TRUSTED_UPSTREAM_REQUIRE_JWT}"]
        if INSTANCE_TRUSTED_UPSTREAM_JWT_HEADER:
            helm_args += ["--set", f"trustedUpstreamAuth.jwtHeader={INSTANCE_TRUSTED_UPSTREAM_JWT_HEADER}"]
        if INSTANCE_TRUSTED_UPSTREAM_JWKS_URL:
            helm_args += ["--set", f"trustedUpstreamAuth.jwksUrl={INSTANCE_TRUSTED_UPSTREAM_JWKS_URL}"]
        if INSTANCE_TRUSTED_UPSTREAM_JWT_AUDIENCE:
            helm_args += ["--set", f"trustedUpstreamAuth.jwtAudience={INSTANCE_TRUSTED_UPSTREAM_JWT_AUDIENCE}"]
        if INSTANCE_TRUSTED_UPSTREAM_JWT_ISSUER:
            helm_args += ["--set", f"trustedUpstreamAuth.jwtIssuer={INSTANCE_TRUSTED_UPSTREAM_JWT_ISSUER}"]
        if INSTANCE_TRUSTED_UPSTREAM_JWT_EMAIL_CLAIM:
            helm_args += ["--set", f"trustedUpstreamAuth.jwtEmailClaim={INSTANCE_TRUSTED_UPSTREAM_JWT_EMAIL_CLAIM}"]
        if INSTANCE_TRUSTED_UPSTREAM_JWT_USER_ID_CLAIM:
            helm_args += ["--set", f"trustedUpstreamAuth.jwtUserIdClaim={INSTANCE_TRUSTED_UPSTREAM_JWT_USER_ID_CLAIM}"]
        if INSTANCE_TRUSTED_UPSTREAM_JWT_MATRIX_USER_ID_CLAIM:
            helm_args += [
                "--set",
                f"trustedUpstreamAuth.jwtMatrixUserIdClaim={INSTANCE_TRUSTED_UPSTREAM_JWT_MATRIX_USER_ID_CLAIM}",
            ]

        _append_matrix_oidc_helm_args(helm_args)
        code, stdout, stderr = await run_helm(helm_args)
        if code != 0:
            msg = f"Helm install failed: {stderr}"
            raise HTTPException(status_code=500, detail=msg)  # noqa: TRY301
        logger.info("Helm install output: %s", stdout)
        # Older releases managed this Secret in Helm. Apply it after Helm so
        # Helm's resource pruning cannot delete the externally managed Secret.
        await _apply_instance_secret(customer_id, namespace, instance_secret_data)
    except HTTPException:
        _mark_instance_provision_error(sb, customer_id, "deployment HTTP exception")
        raise
    except Exception as e:
        logger.exception("Failed to deploy instance")
        _mark_instance_provision_error(sb, customer_id, "deploy exception")
        raise HTTPException(status_code=500, detail=f"Failed to deploy instance: {e!s}") from e

    # Optional readiness poll; if ready, mark running. Otherwise remain provisioning.
    ready = await wait_for_deployment_ready(customer_id, namespace=namespace, timeout_seconds=180)
    try:
        sb.table("instances").update(
            {"status": "running" if ready else "provisioning", "updated_at": datetime.now(UTC).isoformat()}
        ).eq("instance_id", customer_id).execute()
    except Exception:
        logger.warning("Failed to update instance status after readiness poll")

    if not ready and background_tasks is not None:
        # Fire-and-forget longer background wait to mark running later
        try:
            background_tasks.add_task(_background_mark_running_when_ready, customer_id, namespace)
        except Exception:
            logger.warning("Failed to schedule background readiness task for instance %s", customer_id)

    return {
        "customer_id": customer_id,
        "frontend_url": frontend_url,
        "api_url": api_url,
        "matrix_url": matrix_url,
        "success": True,
        "message": "Instance provisioned successfully" if ready else "Provisioning started; instance is getting ready",
    }


async def start_instance(instance_id: int) -> dict[str, Any]:
    """Start a tenant instance by scaling its deployments up."""
    logger.info("Starting instance %s", instance_id)

    if not await check_deployment_exists(instance_id):
        error_msg = f"Deployment {instance_deployment_ref(instance_id)} not found"
        logger.warning(error_msg)
        raise HTTPException(status_code=404, detail=error_msg)

    try:
        out = await _scale_tenant_deployments(
            tenant_start_deployment_refs(instance_id), replicas=1, namespace=_INSTANCES_NAMESPACE
        )
        logger.info("Started instance %s: %s", instance_id, out)
        # Reflect desired state in DB immediately
        if not update_instance_status(instance_id, "running"):
            logger.warning("Failed to update DB status to running for instance %s", instance_id)
    except Exception as e:
        logger.exception("Failed to start instance %s", instance_id)
        raise HTTPException(status_code=500, detail=f"Failed to start instance: {e}") from e

    return {"success": True, "message": f"Instance {instance_id} started successfully"}


async def stop_instance(instance_id: int) -> dict[str, Any]:
    """Stop a tenant instance by scaling its deployments down."""
    logger.info("Stopping instance %s", instance_id)

    if not await check_deployment_exists(instance_id):
        error_msg = f"Deployment {instance_deployment_ref(instance_id)} not found"
        logger.warning(error_msg)
        raise HTTPException(status_code=404, detail=error_msg)

    try:
        out = await _scale_tenant_deployments(
            tenant_stop_deployment_refs(instance_id), replicas=0, namespace=_INSTANCES_NAMESPACE
        )
        logger.info("Stopped instance %s: %s", instance_id, out)
        # Reflect desired state in DB immediately
        if not update_instance_status(instance_id, "stopped"):
            logger.warning("Failed to update DB status to stopped for instance %s", instance_id)
    except Exception as e:
        logger.exception("Failed to stop instance %s", instance_id)
        raise HTTPException(status_code=500, detail=f"Failed to stop instance: {e}") from e

    return {"success": True, "message": f"Instance {instance_id} stopped successfully"}


async def restart_instance(instance_id: int) -> dict[str, Any]:
    """Restart a tenant instance via rolling restarts of its deployments."""
    logger.info("Restarting instance %s", instance_id)

    if not await check_deployment_exists(instance_id):
        error_msg = f"Deployment {instance_deployment_ref(instance_id)} not found"
        logger.warning(error_msg)
        raise HTTPException(status_code=404, detail=error_msg)

    try:
        out = await _run_kubectl_for_deployments(
            ["rollout", "restart"], tenant_start_deployment_refs(instance_id), namespace=_INSTANCES_NAMESPACE
        )
        logger.info("Restarted instance %s: %s", instance_id, out)
    except Exception as e:
        logger.exception("Failed to restart instance %s", instance_id)
        raise HTTPException(status_code=500, detail=f"Failed to restart instance: {e}") from e

    return {"success": True, "message": f"Instance {instance_id} restarted successfully"}


async def uninstall_instance(instance_id: int) -> dict[str, Any]:
    """Completely uninstall/deprovision a tenant instance."""
    logger.info("Uninstalling instance %s", instance_id)

    try:
        helm_release_name = f"instance-{instance_id}"
        code, stdout, stderr = await run_helm(["uninstall", helm_release_name, f"--namespace={_INSTANCES_NAMESPACE}"])

        if code != 0:
            error_msg = stderr
            if "not found" in error_msg.lower():
                logger.info("Instance %s was already uninstalled", instance_id)
            else:
                logger.error("Failed to uninstall instance: %s", error_msg)
                msg = f"Failed to uninstall instance: {error_msg}"
                raise HTTPException(status_code=500, detail=msg)  # noqa: TRY301
        else:
            logger.info("Successfully uninstalled instance %s: %s", instance_id, stdout)

        if not update_instance_status(instance_id, "deprovisioned"):
            logger.warning("Failed to update database for instance %s", instance_id)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to uninstall instance %s", instance_id)
        raise HTTPException(status_code=500, detail=f"Failed to uninstall instance: {e}") from e

    return {"success": True, "message": f"Instance {instance_id} uninstalled successfully", "instance_id": instance_id}


async def sync_instances(sb: Any) -> dict[str, Any]:
    """Sync instance states between database and Kubernetes cluster."""
    logger.info("Starting instance sync")

    try:
        result = sb.table("instances").select("*").execute()
        instances = result.data if result.data else []

        sync_results: dict[str, Any] = {"total": len(instances), "synced": 0, "errors": 0, "updates": []}

        for instance in instances:
            instance_id = instance.get("instance_id") or instance.get("subdomain")
            if not instance_id:
                logger.warning("Instance %s has no instance_id or subdomain", instance.get("id"))
                sync_results["errors"] += 1
                continue

            exists = await check_deployment_exists(instance_id)
            current_status = instance.get("status", "unknown")

            if not exists:
                if current_status not in ["error", "deprovisioned"]:
                    logger.info("Instance %s not found in cluster, marking as error", instance_id)
                    now = datetime.now(UTC).isoformat()
                    sb.table("instances").update(
                        {"status": "error", "kubernetes_synced_at": now, "updated_at": now}
                    ).eq("id", instance["id"]).execute()

                    sync_results["updates"].append(
                        SyncUpdateOut(
                            instance_id=instance_id,
                            old_status=current_status,
                            new_status="error",
                            reason="deployment_not_found",
                        ).model_dump()
                    )
                    sync_results["synced"] += 1
            else:
                try:
                    code, out, _ = await run_kubectl(
                        ["get", instance_deployment_ref(instance_id), "-o=jsonpath={.spec.replicas}"],
                        namespace=_INSTANCES_NAMESPACE,
                    )
                    if code == 0:
                        replicas = int(out.strip() or "0")
                        actual_status = "running" if replicas > 0 else "stopped"

                        if current_status != actual_status:
                            logger.info(
                                "Instance %s status mismatch: DB=%s, K8s=%s", instance_id, current_status, actual_status
                            )
                            now = datetime.now(UTC).isoformat()
                            sb.table("instances").update(
                                {"status": actual_status, "kubernetes_synced_at": now, "updated_at": now}
                            ).eq("id", instance["id"]).execute()

                            sync_results["updates"].append(
                                SyncUpdateOut(
                                    instance_id=instance_id,
                                    old_status=current_status,
                                    new_status=actual_status,
                                    reason="status_mismatch",
                                ).model_dump()
                            )
                            sync_results["synced"] += 1
                except Exception:
                    logger.exception("Error checking instance %s state", instance_id)
                    sync_results["errors"] += 1

        logger.info("Instance sync completed: %s", sync_results)
        return sync_results  # noqa: TRY300
    except Exception as e:
        logger.exception("Failed to sync instances")
        raise HTTPException(status_code=500, detail=f"Failed to sync instances: {e}") from e
