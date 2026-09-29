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
import re
import secrets
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any
from uuid import UUID

import anyio
from backend.config import (
    INSTANCE_BASE_DOMAIN,
    INSTANCE_CREDENTIALS_ENCRYPTION_SECRET,
    INSTANCE_IMAGE_PULL_SECRET_NAMES,
    INSTANCE_INGRESS_CONTROLLER_NAMESPACE,
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
    INSTANCE_TRUSTED_UPSTREAM_EMAIL_DOMAIN,
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
    SUPABASE_ANON_KEY,
    SUPABASE_URL,
    logger,
)
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
    OpenRouterKeyNotFoundError,
    OpenRouterKeyPlan,
    create_openrouter_key,
    delete_openrouter_key,
    set_openrouter_key_disabled,
)
from backend.pricing import get_plan_details
from backend.process import run_helm
from backend.services.instances_data import (
    create_instance,
    get_instance,
    list_instances,
    update_instance,
    update_instance_status,
)
from fastapi import BackgroundTasks, HTTPException
from supabase import PostgrestAPIError

_MATRIX_LOCALPART_ALLOWED_CHARS = frozenset("_-./=+abcdefghijklmnopqrstuvwxyz0123456789")
# Rooms created by the seeded instance config (cluster/k8s/instance/default-config.yaml).
_HOSTED_MATRIX_AUTO_JOIN_ROOM_KEYS = ("personal",)

_RESOURCE_PROFILE_HELM_VALUES = {
    "pro": {
        "storage": "25Gi",
        "mindroomResources.requests.memory": "640Mi",
        "mindroomResources.requests.cpu": "200m",
        "mindroomResources.requests.ephemeral-storage": "64Mi",
        "mindroomResources.limits.memory": "4Gi",
        "mindroomResources.limits.cpu": "2000m",
        "mindroomResources.limits.ephemeral-storage": "32Gi",
        "synapseResources.requests.memory": "384Mi",
        "synapseResources.requests.cpu": "100m",
        "synapseResources.requests.ephemeral-storage": "64Mi",
        "synapseResources.limits.memory": "4Gi",
        "synapseResources.limits.cpu": "2000m",
        "synapseResources.limits.ephemeral-storage": "4Gi",
        "sandboxRunnerResources.requests.memory": "256Mi",
        "sandboxRunnerResources.requests.cpu": "50m",
        "sandboxRunnerResources.requests.ephemeral-storage": "64Mi",
        "sandboxRunnerResources.limits.memory": "2Gi",
        "sandboxRunnerResources.limits.cpu": "1000m",
        "sandboxRunnerResources.limits.ephemeral-storage": "8Gi",
    }
}

_INSTANCES_NAMESPACE = "mindroom-instances"
# Tenant pods run tenant code, so admission must reject privileged and host-reaching pods in their namespace.
_POD_SECURITY_ENFORCE_LABEL = "pod-security.kubernetes.io/enforce=baseline"
# PostgreSQL unique_violation of the constraint that allows one instance per subscription (migration 007).
_UNIQUE_VIOLATION = "23505"
_ONE_INSTANCE_PER_SUBSCRIPTION = "instances_subscription_id_key"
_QUANTITY = re.compile(r"(\d+(?:\.\d+)?)([KMGTPE]i|[kMGTPE])?")
_QUANTITY_FACTORS = {
    **{suffix: 1024**power for power, suffix in enumerate(("", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei"))},
    **{suffix: 1000**power for power, suffix in enumerate(("k", "M", "G", "T", "P", "E"), start=1)},
}


def _env_flag_enabled(value: str) -> bool:
    """Return whether an env-style flag value is explicitly enabled."""
    return value.strip().lower() in {"1", "true", "yes", "on"}


async def _background_mark_running_when_ready(instance_id: str, namespace: str = _INSTANCES_NAMESPACE) -> None:
    """Background task: wait longer and mark instance running when ready, unless the lifecycle held it meanwhile."""
    try:
        ready = await wait_for_deployment_ready(instance_id, namespace=namespace, timeout_seconds=600)
        if ready and not _held_by_lifecycle(ensure_supabase(), instance_id):
            try:
                update_instance(ensure_supabase(), instance_id, {"status": "running"})
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


def instance_platform_sso_secret(instance_id: str) -> str:
    """Derive the per-instance key that signs dashboard login tickets for one tenant runtime."""
    return _stable_instance_secret("platform-sso", instance_id)


def _stable_instance_secret(purpose: str, instance_id: str) -> str:
    """Derive one stable per-instance secret from the platform root secret.

    WARNING: when INSTANCE_CREDENTIALS_ENCRYPTION_SECRET is unset, PROVISIONER_API_KEY doubles
    as the HMAC root secret. Rotating PROVISIONER_API_KEY without first setting
    INSTANCE_CREDENTIALS_ENCRYPTION_SECRET silently invalidates every derived per-instance
    secret (credential encryption keys, Matrix registration shared secrets, dashboard SSO keys) for existing tenants.
    """
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
    """Return the MXID that should be authorized for the tenant owner.

    Only platform OIDC binds the derived localpart to the authenticated account holder.
    Without it the platform cannot prove who holds that MXID, so nothing is pre-authorized.
    """
    if not account_id or not _env_flag_enabled(INSTANCE_MATRIX_OIDC_ENABLED):
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
    if _env_flag_enabled(INSTANCE_MATRIX_OIDC_ENABLED):
        # The chart only enables OIDC for the literal "true", which must agree with the owner authorization gate.
        helm_args += [
            "--set",
            "matrixOidc.enabled=true",
            "--set",
            "roomDefaults.joinPolicy=public",
            "--set",
            "roomDefaults.listed=false",
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


def _instance_pvc_names(instance_id: str | int) -> list[str]:
    """Return the chart-managed PVC names that hold one instance's data."""
    return [f"mindroom-storage-{instance_id}", f"synapse-storage-{instance_id}"]


def _instance_secret_names(instance_id: str | int) -> list[str]:
    """Return every Secret name an instance may own, including ones applied outside Helm."""
    return [
        _instance_secret_name(str(instance_id)),
        f"mindroom-primary-api-key-{instance_id}",
    ]


async def _enforce_pod_security_baseline(namespace: str) -> None:
    """Label the tenant namespace for the Pod Security baseline profile before deploying into it."""
    code, _out, err = await run_kubectl(["label", "namespace", namespace, _POD_SECURITY_ENFORCE_LABEL, "--overwrite"])
    if code != 0:
        logger.error("Failed to enforce Pod Security on namespace %s: %s", namespace, err)
        raise HTTPException(status_code=500, detail="Failed to enforce Pod Security on the instance namespace")


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
    await _remove_stale_instance_secret_keys(secret_name, namespace, secret_data.keys())
    return _instance_secret_hash(secret_data)


async def _remove_stale_instance_secret_keys(secret_name: str, namespace: str, current_keys: Iterable[str]) -> None:
    """Delete Secret keys the provisioner no longer writes.

    `kubectl apply` leaves keys dropped from `stringData` in the live Secret, so a retired
    credential would otherwise stay mounted in tenant pods.
    """
    code, out, err = await run_kubectl(
        ["get", "secret", secret_name, "-o=go-template={{range $key, $value := .data}}{{$key}} {{end}}"],
        namespace=namespace,
    )
    if code != 0:
        msg = f"Failed to inspect instance Secret {secret_name}: {err or out}"
        raise RuntimeError(msg)
    stale_keys = sorted(set(out.split()) - set(current_keys))
    if not stale_keys:
        return
    patch = json.dumps({"data": dict.fromkeys(stale_keys)})
    code, out, err = await run_kubectl(
        ["patch", "secret", secret_name, "--type=merge", "-p", patch], namespace=namespace
    )
    if code != 0:
        msg = f"Failed to remove stale keys from instance Secret {secret_name}: {err or out}"
        raise RuntimeError(msg)


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


@dataclass(frozen=True)
class _ExistingVolumes:
    """Settings of an instance's existing PVCs that a redeploy must keep, because Kubernetes cannot change them."""

    storage_class_name: str | None = None
    storage: str | None = None  # Largest requested size; a bound PVC can grow but never shrink.


def _quantity_bytes(quantity: str) -> float:
    """Return the size a Kubernetes storage quantity such as `25Gi` or `10G` stands for."""
    match = _QUANTITY.fullmatch(quantity)
    if match is None:
        msg = f"Cannot compare the storage size {quantity!r}"
        raise HTTPException(status_code=500, detail=msg)
    return float(match.group(1)) * _QUANTITY_FACTORS[match.group(2) or ""]


async def _existing_instance_volumes(instance_id: str, namespace: str) -> _ExistingVolumes:
    """Return the storage class and requested size of an existing instance's PVCs."""
    code, out, err = await run_kubectl(
        ["get", "pvc", *_instance_pvc_names(instance_id), "--ignore-not-found", "-o", "json"], namespace=namespace
    )
    if code != 0:
        msg = f"Failed to inspect existing PVCs for instance {instance_id}: {err or out}"
        raise HTTPException(status_code=500, detail=msg)
    if not out.strip():
        return _ExistingVolumes()

    specs = [item.get("spec", {}) for item in json.loads(out).get("items", [])]
    storage_classes = {spec.get("storageClassName", "").strip() for spec in specs} - {""}
    if len(storage_classes) > 1:
        msg = f"Instance {instance_id} has PVCs with different storage classes: {', '.join(sorted(storage_classes))}"
        raise HTTPException(status_code=500, detail=msg)
    sizes = {spec.get("resources", {}).get("requests", {}).get("storage", "").strip() for spec in specs} - {""}
    return _ExistingVolumes(
        storage_class_name=next(iter(storage_classes), None),
        storage=max(sizes, key=_quantity_bytes, default=None),
    )


def _append_storage_helm_args(helm_args: list[str], resource_profile: str, existing_storage: str | None) -> None:
    """Keep existing volumes at least their current size, since Kubernetes refuses to shrink a PVC.

    A tier's profile can lower the requested size (for example a downgrade from pro), and Helm would then fail.
    """
    requested = _RESOURCE_PROFILE_HELM_VALUES.get(resource_profile, {}).get("storage")
    if existing_storage and (requested is None or _quantity_bytes(requested) < _quantity_bytes(existing_storage)):
        helm_args += ["--set", f"storage={existing_storage}"]


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


CLEARED_OPENROUTER_KEY_METADATA = {
    "openrouter_key_hash": None,
    "openrouter_key_label": None,
    "openrouter_key_limit_usd": None,
    "openrouter_key_limit_reset": None,
    "openrouter_key_created_at": None,
}


def _included_ai_budget_usd(tier: str) -> int:
    plan = get_plan_details(tier)
    return plan.included_ai_budget_usd if plan else 0


def openrouter_key_matches_plan(instance_row: Mapping[str, Any], tier: str) -> bool:
    """Return whether an instance holds exactly the platform-paid OpenRouter key its tier includes.

    A tier without an included AI budget matches only an instance that stores no key.
    """
    budget = _included_ai_budget_usd(tier)
    if budget <= 0:
        return _stored_openrouter_key_hash(instance_row) is None
    return _matching_openrouter_metadata(instance_row, budget)


def openrouter_key_exceeds_plan(instance_row: Mapping[str, Any], tier: str) -> bool:
    """Return whether an instance holds a platform-paid key with a larger budget than its tier includes."""
    if _stored_openrouter_key_hash(instance_row) is None:
        return False
    limit = instance_row.get("openrouter_key_limit_usd")
    return limit is None or int(limit) > _included_ai_budget_usd(tier)


async def set_instance_openrouter_key_disabled(instance_row: Mapping[str, Any], *, disabled: bool) -> None:
    """Disable or re-enable the platform-paid OpenRouter key of one instance, if it has one.

    A key that no longer exists counts as disabled; re-enabling a missing key raises.
    """
    key_hash = _stored_openrouter_key_hash(instance_row)
    if key_hash is None:
        return
    set_disabled = partial(
        set_openrouter_key_disabled,
        management_api_key=OPENROUTER_PROVISIONING_API_KEY,
        key_hash=key_hash,
        disabled=disabled,
    )
    try:
        await anyio.to_thread.run_sync(set_disabled)
    except OpenRouterKeyNotFoundError:
        if not disabled:
            raise
        logger.info("OpenRouter key %s for instance %s no longer exists", key_hash, instance_row.get("instance_id"))


async def revoke_instance_openrouter_key(sb: Any, instance_id: str | int) -> None:
    """Delete the platform-paid OpenRouter key of one instance and forget its metadata."""
    key_hash = _stored_openrouter_key_hash(get_instance(sb, instance_id, columns="openrouter_key_hash"))
    if key_hash is None:
        return
    delete_key = partial(delete_openrouter_key, management_api_key=OPENROUTER_PROVISIONING_API_KEY, key_hash=key_hash)
    try:
        await anyio.to_thread.run_sync(delete_key)
    except OpenRouterKeyNotFoundError:
        logger.info("OpenRouter key %s for instance %s was already deleted", key_hash, instance_id)
    update_instance(sb, instance_id, CLEARED_OPENROUTER_KEY_METADATA)


async def _delete_resources_outside_release(instance_id: str | int) -> None:
    """Delete instance PVCs and Secrets that `helm uninstall` does not own or may leave behind."""
    for kind, names in (("pvc", _instance_pvc_names(instance_id)), ("secret", _instance_secret_names(instance_id))):
        code, out, err = await run_kubectl(
            ["delete", kind, *names, "--ignore-not-found", "--wait=false"], namespace=_INSTANCES_NAMESPACE
        )
        if code != 0:
            msg = f"Failed to delete {kind} for instance {instance_id}: {err or out}"
            raise RuntimeError(msg)


def _persist_openrouter_key_metadata(sb: Any, instance_id: str, created_key: CreatedOpenRouterKey) -> None:
    """Persist non-secret OpenRouter key metadata for reuse and audit."""
    update_instance(
        sb,
        instance_id,
        {
            "openrouter_key_hash": created_key.hash,
            "openrouter_key_label": created_key.label,
            "openrouter_key_limit_usd": created_key.limit_usd,
            "openrouter_key_limit_reset": created_key.limit_reset,
            "openrouter_key_created_at": datetime.now(UTC).isoformat(),
        },
    )


def _mark_instance_provision_error(sb: Any, instance_id: str, context: str) -> None:
    """Mark a failed provisioning attempt as error without hiding the original failure."""
    try:
        update_instance(sb, instance_id, {"status": "error"})
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
) -> tuple[str, CreatedOpenRouterKey | None]:
    """Return the OpenRouter key value this tenant instance should receive, and the key if it was just created.

    A stored key with a larger budget than the tier includes (for example after a downgrade) is deleted before any
    replacement exists, so a failure can never leave it live once the row stops naming it; a smaller one keeps
    serving until its replacement is published.
    A created key is not recorded yet: call `_commit_openrouter_key` once the Secret holding it is published,
    or `_discard_openrouter_key` if publication fails, so stored metadata always names the published key.
    """
    monthly_limit_usd = _included_ai_budget_usd(tier)
    if monthly_limit_usd > 0 and _matching_openrouter_metadata(existing_instance_row, monthly_limit_usd):
        existing_key = await _existing_instance_secret_value(instance_id, namespace, "openrouter_key")
        if existing_key:
            return existing_key, None
    if existing_instance_row is not None and openrouter_key_exceeds_plan(existing_instance_row, tier):
        await revoke_instance_openrouter_key(sb, instance_id)
    if monthly_limit_usd <= 0:
        return "", None

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
    return created_key.key, created_key


async def _commit_openrouter_key(sb: Any, instance_id: str, created_key: CreatedOpenRouterKey) -> None:
    """Record a newly published key and revoke the smaller key the row still names, if any."""
    superseded_key_hash = _stored_openrouter_key_hash(get_instance(sb, instance_id, columns="openrouter_key_hash"))
    try:
        await anyio.to_thread.run_sync(partial(_persist_openrouter_key_metadata, sb, instance_id, created_key))
    except Exception:
        logger.exception("Failed to persist OpenRouter key metadata for instance %s", instance_id)
        return
    if superseded_key_hash is None or superseded_key_hash == created_key.hash:
        return
    delete_key = partial(
        delete_openrouter_key, management_api_key=OPENROUTER_PROVISIONING_API_KEY, key_hash=superseded_key_hash
    )
    try:
        await anyio.to_thread.run_sync(delete_key)
    except OpenRouterError:
        logger.warning(
            "Failed to revoke superseded OpenRouter key %s for instance %s",
            superseded_key_hash,
            instance_id,
            exc_info=True,
        )


async def _discard_openrouter_key(created_key: CreatedOpenRouterKey, instance_id: str) -> None:
    """Best-effort delete of a key whose Secret was never published, so the next attempt mints a fresh one."""
    delete_key = partial(
        delete_openrouter_key, management_api_key=OPENROUTER_PROVISIONING_API_KEY, key_hash=created_key.hash
    )
    try:
        await anyio.to_thread.run_sync(delete_key)
    except OpenRouterError:
        logger.warning("Failed to delete unpublished OpenRouter key for instance %s", instance_id, exc_info=True)


async def provision_instance(  # noqa: C901, PLR0912, PLR0915
    sb: Any, *, data: dict, background_tasks: BackgroundTasks | None, resume_lifecycle_hold: bool = False
) -> dict[str, Any]:
    """Provision (or re-provision) a tenant instance and return the portal response payload.

    An instance the subscription lifecycle holds is redeployed stopped with its key disabled,
    unless the lifecycle itself is resuming it (`resume_lifecycle_hold`).
    """
    subscription_id = data.get("subscription_id")
    account_id = data.get("account_id")
    tier = data.get("tier", "free")
    existing_instance_id = data.get("instance_id")  # For re-provisioning

    # If re-provisioning, update existing instance; otherwise insert new
    if existing_instance_id:
        customer_id = str(existing_instance_id)
        try:
            updated_rows = update_instance(sb, customer_id, {"status": "provisioning"})
            if not updated_rows:
                msg = f"Instance {customer_id} not found"
                raise HTTPException(status_code=404, detail=msg)  # noqa: TRY301
            existing_instance_row = updated_rows[0]
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
            created_row = create_instance(
                sb,
                {
                    "subscription_id": subscription_id,
                    "account_id": account_id,
                    "status": "provisioning",
                    "tier": tier,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            if not created_row:
                msg = "Failed to insert instance"
                raise HTTPException(status_code=500, detail=msg)  # noqa: TRY301
            customer_id = created_row["instance_id"]
            existing_instance_row = created_row
        except HTTPException:
            raise
        except Exception as e:
            if (
                isinstance(e, PostgrestAPIError)
                and e.code == _UNIQUE_VIOLATION
                and _ONE_INSTANCE_PER_SUBSCRIPTION in (e.message or "")
            ):
                # A concurrent request inserted this subscription's instance first; the database allows only one.
                raise HTTPException(status_code=409, detail="This subscription already has an instance") from e
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
        update_instance(
            sb,
            customer_id,
            {
                "instance_url": frontend_url,
                "frontend_url": frontend_url,
                "backend_url": api_url,
                "api_url": api_url,
                "matrix_url": matrix_url,
                "matrix_server_url": matrix_url,
            },
        )
    except Exception:
        logger.warning("Failed to update URLs for instance %s", customer_id)

    # Keep this non-empty so shell/file/python proxying doesn't fail at runtime.
    # Always per instance: a shared token would let one tenant authenticate to every tenant's runner.
    sandbox_proxy_token = secrets.token_hex(32)
    try:
        await _enforce_pod_security_baseline(namespace)
        # Existing instances may have plaintext credential files; preserve their current encryption state.
        credentials_encryption_key = await _provision_credentials_encryption_key(
            customer_id=customer_id, existing_instance_id=existing_instance_id, data=data, namespace=namespace
        )
        existing_volumes = (
            await _existing_instance_volumes(customer_id, namespace) if existing_instance_id else _ExistingVolumes()
        )
        storage_class_name = existing_volumes.storage_class_name or INSTANCE_STORAGE_CLASS_NAME
        openrouter_key, created_openrouter_key = await _provision_openrouter_key(
            sb=sb,
            account_id=account_id,
            instance_id=customer_id,
            tier=tier,
            existing_instance_row=existing_instance_row,
            namespace=namespace,
        )
        # User BYOK credentials live in tenant storage; hosted budgets use only a scoped OpenRouter key.
        # Tenant workloads are untrusted, so every value here must be scoped to this instance.
        instance_secret_data = {
            "openai_key": "",
            "anthropic_key": "",
            "openrouter_key": openrouter_key,
            "google_key": "",
            "deepseek_key": "",
            "sandbox_proxy_token": sandbox_proxy_token,
            "credentials_encryption_key": credentials_encryption_key,
            "matrix_oidc_client_secret": INSTANCE_MATRIX_OIDC_CLIENT_SECRET or "",
            "matrix_registration_shared_secret": _instance_matrix_registration_shared_secret(customer_id),
            "platform_sso_secret": instance_platform_sso_secret(customer_id),
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
            f"platformDomain={PLATFORM_DOMAIN}",
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
        if INSTANCE_INGRESS_CONTROLLER_NAMESPACE:
            helm_args += ["--set", f"ingressControllerNamespace={INSTANCE_INGRESS_CONTROLLER_NAMESPACE}"]
        plan = get_plan_details(tier)
        resource_profile = plan.resource_profile if plan else ""
        _append_resource_profile_helm_args(helm_args, resource_profile)
        _append_storage_helm_args(helm_args, resource_profile, existing_volumes.storage)
        if INSTANCE_MINDROOM_IMAGE:
            helm_args += ["--set", f"mindroom_image={INSTANCE_MINDROOM_IMAGE}"]
        if INSTANCE_MINDROOM_IMAGE_PULL_POLICY:
            helm_args += ["--set", f"mindroom_image_pull_policy={INSTANCE_MINDROOM_IMAGE_PULL_POLICY}"]
        if owner_matrix_user_id:
            helm_args += [
                "--set-string",
                f"administrators[0]={owner_matrix_user_id}",
                "--set-string",
                f"roomDefaults.inviteUsers[0]={owner_matrix_user_id}",
                "--set-string",
                f"roomDefaults.admins[0]={owner_matrix_user_id}",
            ]
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
        if INSTANCE_TRUSTED_UPSTREAM_EMAIL_DOMAIN:
            helm_args += ["--set", f"trustedUpstreamAuth.emailDomain={INSTANCE_TRUSTED_UPSTREAM_EMAIL_DOMAIN}"]
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
        # Apply before Helm so pods restarted by the new secret hash read the new values;
        # Synapse reads its OIDC client secret only at startup.
        try:
            await _apply_instance_secret(customer_id, namespace, instance_secret_data)
        except Exception:
            if created_openrouter_key is not None:
                await _discard_openrouter_key(created_openrouter_key, customer_id)
            raise
        if created_openrouter_key is not None:
            await _commit_openrouter_key(sb, customer_id, created_openrouter_key)
        code, stdout, stderr = await run_helm(helm_args)
        if code != 0:
            msg = f"Helm install failed: {stderr}"
            raise HTTPException(status_code=500, detail=msg)  # noqa: TRY301
        logger.info("Helm install output: %s", stdout)
        # Older releases managed this Secret in Helm. Apply it again after Helm
        # because Helm's resource pruning deletes the externally managed Secret.
        await _apply_instance_secret(customer_id, namespace, instance_secret_data)
    except HTTPException:
        _mark_instance_provision_error(sb, customer_id, "deployment HTTP exception")
        raise
    except Exception as e:
        logger.exception("Failed to deploy instance")
        _mark_instance_provision_error(sb, customer_id, "deploy exception")
        raise HTTPException(status_code=500, detail=f"Failed to deploy instance: {e!s}") from e

    held_response = {
        "customer_id": customer_id,
        "frontend_url": frontend_url,
        "api_url": api_url,
        "matrix_url": matrix_url,
        "success": True,
        "message": "Instance redeployed but kept stopped because its subscription or account cannot run it",
    }
    # The lifecycle holds instances without the provisioning request knowing, and Helm just set every replica back
    # to one, so a hold that exists now, or lands during the readiness wait, keeps the instance stopped.
    if not resume_lifecycle_hold and _held_by_lifecycle(sb, customer_id):
        await _keep_held_instance_stopped(sb, customer_id, tier)
        return held_response

    # Optional readiness poll; if ready, mark running. Otherwise remain provisioning.
    ready = await wait_for_deployment_ready(customer_id, namespace=namespace, timeout_seconds=180)
    if not resume_lifecycle_hold and _held_by_lifecycle(sb, customer_id):
        await _keep_held_instance_stopped(sb, customer_id, tier)
        return held_response
    try:
        # The tier is recorded only once deployed; the subscription lifecycle redeploys an instance whose tier differs.
        update_instance(sb, customer_id, {"status": "running" if ready else "provisioning", "tier": tier})
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


def account_row_pending_deletion(account: Mapping[str, Any] | None) -> bool:
    """Return whether an `accounts` row is pending deletion; the one check every pending-deletion decision uses."""
    return account is not None and account.get("deleted_at") is not None


def account_pending_deletion(sb: Any, account_id: str) -> bool:
    """Return whether the account is pending deletion, when it may not run instances or change billing."""
    rows = sb.table("accounts").select("deleted_at").eq("id", account_id).limit(1).execute().data
    return account_row_pending_deletion(rows[0] if rows else None)


def refuse_pending_deletion(sb: Any, account_id: str, detail: str) -> None:
    """Refuse the request with `detail` while the account is pending deletion, whatever its subscription says."""
    if account_pending_deletion(sb, account_id):
        raise HTTPException(status_code=409, detail=detail)


def _held_by_lifecycle(sb: Any, instance_id: str | int) -> bool:
    """Return whether the lifecycle holds the instance right now, or will because its account is pending deletion."""
    row = get_instance(sb, instance_id, columns="lifecycle_stopped_at,account_id") or {}
    if row.get("lifecycle_stopped_at") is not None:
        return True
    return row.get("account_id") is not None and account_pending_deletion(sb, row["account_id"])


async def _keep_held_instance_stopped(sb: Any, instance_id: str, tier: str) -> None:
    """Scale a freshly deployed instance the lifecycle holds back to zero with its platform key disabled."""
    await _scale_tenant_deployments(
        tenant_stop_deployment_refs(instance_id), replicas=0, namespace=_INSTANCES_NAMESPACE
    )
    held_row = get_instance(sb, instance_id, columns="instance_id,openrouter_key_hash") or {}
    await set_instance_openrouter_key_disabled(held_row, disabled=True)
    update_instance(sb, instance_id, {"status": "stopped", "tier": tier})


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
    """Completely deprovision a tenant instance.

    Removes the Helm release, the instance volumes and Secrets, and the platform-paid OpenRouter key,
    then marks the instance deprovisioned. Every step tolerates already-deleted resources, so a failed
    run can simply be repeated.
    """
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

        await _delete_resources_outside_release(instance_id)
        await revoke_instance_openrouter_key(ensure_supabase(), instance_id)

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
        instances = list_instances(sb)

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
