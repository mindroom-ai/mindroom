#!/usr/bin/env bash
# Deploy a MindRoom release to the hosted SaaS cluster.
#
# Usage: cluster/scripts/deploy-release.sh <tag> [--instances running|all|none|<id,id,...>] [--dry-run]
#
# Steps:
#   1. Pre-pull the release images on the k3s node.
#   2. Upgrade the platform Helm release with the chart and images from <tag>.
#   3. Re-provision the selected instances through POST /system/provision.
#   4. Health-check the platform API and every re-provisioned running instance.
#
# Run it from a repo checkout that has <tag> (git fetch --tags), with kubectl and helm pointed at the cluster.
# Secrets are read from the cluster and never printed.
# --dry-run performs every read, renders the Helm upgrade, and prints the plan without changing anything.
#
# Environment:
#   NAMESPACE   platform namespace (default: mindroom-production)
#   RELEASE     platform Helm release (default: platform)
#   NODE_SSH    ssh target of the k3s node for image pre-pulls (default: run crictl locally)
#   BACKUP_DIR  where the previous Helm values are saved (default: ~/saas-deploy)

set -euo pipefail

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

TAG=""
SELECT="running"
DRY_RUN=false
while [ $# -gt 0 ]; do
  case "$1" in
    --instances) SELECT="${2:?--instances needs a value}"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h | --help) usage ;;
    -*) echo "Unknown option: $1" >&2; usage ;;
    *) TAG="$1"; shift ;;
  esac
done
[ -n "$TAG" ] || usage
[[ "$TAG" =~ ^v[0-9]{4}\.[0-9]+\.[0-9]+$ ]] || { echo "Tag must look like vYYYY.M.N, got $TAG" >&2; exit 2; }

NAMESPACE="${NAMESPACE:-mindroom-production}"
RELEASE="${RELEASE:-platform}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/saas-deploy}"
REPO_ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)

for cmd in kubectl helm curl jq git tar; do
  command -v "$cmd" >/dev/null || { echo "Missing required command: $cmd" >&2; exit 1; }
done
git -C "$REPO_ROOT" rev-parse -q --verify "refs/tags/$TAG" >/dev/null ||
  { echo "Tag $TAG not found locally; run git fetch --tags first" >&2; exit 1; }

log() { echo "[$(date +%H:%M:%S)] $*"; }
run() {
  if $DRY_RUN; then echo "  [dry-run] $*"; else "$@"; fi
}

# --- Read configuration and secrets from the cluster ---
VALUES_JSON=$(helm get values "$RELEASE" -n "$NAMESPACE" -o json)
value() { jq -r "$1" <<<"$VALUES_JSON"; }
DOMAIN=$(value '.domain // empty')
INSTANCE_DOMAIN=$(value '.provisioner.instanceBaseDomain // .domain // empty')
SUPABASE_URL=$(value '.supabase.url // empty')
REGISTRY=$(value '.registry // "ghcr.io/mindroom-ai"')
SECRET_NAME=$(value '.platformSecrets.name // "platform-secrets"')
[ -n "$DOMAIN" ] && [ -n "$SUPABASE_URL" ] || { echo "Helm values lack domain or supabase.url" >&2; exit 1; }

secret() {
  kubectl get secret "$SECRET_NAME" -n "$NAMESPACE" -o "jsonpath={.data.$1}" | base64 -d
}
PROVISIONER_KEY=$(secret provisioner_api_key)
SUPABASE_KEY=$(secret supabase_service_key)
[ -n "$PROVISIONER_KEY" ] && [ -n "$SUPABASE_KEY" ] || { echo "Secret $SECRET_NAME lacks provisioner_api_key or supabase_service_key" >&2; exit 1; }

API="https://api.$DOMAIN"
BACKEND_IMAGE="$REGISTRY/platform-backend:$TAG"
FRONTEND_IMAGE="$REGISTRY/platform-frontend:$TAG"
MINDROOM_IMAGE="$REGISTRY/mindroom:$TAG"

# Headers go through stdin so secrets never appear in the process list or output.
supabase_get() {
  printf 'apikey: %s\nAuthorization: Bearer %s\n' "$SUPABASE_KEY" "$SUPABASE_KEY" |
    curl -fsS -H @- "$SUPABASE_URL/rest/v1/$1"
}
# Prints the response body and then the HTTP status code on its own line.
provisioner_post() {
  local path="$1" body="${2:-}"
  [ -n "$body" ] || body='{}'
  printf 'Authorization: Bearer %s\n' "$PROVISIONER_KEY" |
    curl -sS --max-time 600 -H @- -H 'Content-Type: application/json' \
      -X POST -d "$body" -w '\n%{http_code}' "$API$path"
}

# --- Select instances ---
ROWS=$(supabase_get 'instances?select=instance_id,subscription_id,account_id,tier,status,lifecycle_stopped_at&order=instance_id')
case "$SELECT" in
  none) SELECTED='[]' ;;
  running) SELECTED=$(jq -c '[.[] | select(.status == "running")]' <<<"$ROWS") ;;
  all) SELECTED=$(jq -c '[.[] | select(.status != "deprovisioned")]' <<<"$ROWS") ;;
  *)
    [[ "$SELECT" =~ ^[0-9]+(,[0-9]+)*$ ]] || { echo "--instances must be running, all, none, or comma-separated ids" >&2; exit 2; }
    SELECTED=$(jq -c --arg ids "$SELECT" '($ids | split(",") | map(tonumber)) as $want | [.[] | select(.instance_id as $id | $want | any(. == $id))]' <<<"$ROWS")
    missing=$(jq -r --arg ids "$SELECT" --argjson sel "$SELECTED" '$ids | split(",") | map(tonumber) - ($sel | map(.instance_id)) | join(",")' <<<"null")
    [ -z "$missing" ] || { echo "Unknown instance ids: $missing" >&2; exit 1; }
    deprovisioned=$(jq -r '[.[] | select(.status == "deprovisioned") | .instance_id] | join(",")' <<<"$SELECTED")
    [ -z "$deprovisioned" ] || { echo "Refusing to re-provision deprovisioned instances: $deprovisioned" >&2; exit 1; }
    ;;
esac

log "Release $TAG to $RELEASE in $NAMESPACE ($API)"
log "Instances to re-provision:"
jq -r '.[] | "  \(.instance_id)  status=\(.status)  tier=\(.tier)\(if .lifecycle_stopped_at then "  held by lifecycle" else "" end)"' <<<"$SELECTED"
[ "$(jq length <<<"$SELECTED")" -gt 0 ] || echo "  (none)"

# --- 1. Pre-pull images on the node ---
log "Pre-pulling images"
for image in "$BACKEND_IMAGE" "$FRONTEND_IMAGE" "$MINDROOM_IMAGE"; do
  if [ -n "${NODE_SSH:-}" ]; then
    run ssh "$NODE_SSH" sudo k3s crictl pull "$image"
  else
    run sudo k3s crictl pull "$image"
  fi
done

# --- 2. Upgrade the platform release with the chart from the same tag ---
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
git -C "$REPO_ROOT" archive "$TAG" cluster/k8s/platform | tar -x -C "$WORK"
mkdir -p "$BACKUP_DIR"
PREVIOUS_VALUES="$BACKUP_DIR/platform-values-$(date +%Y%m%d-%H%M%S).yaml"
(umask 077 && helm get values "$RELEASE" -n "$NAMESPACE" -o yaml >"$PREVIOUS_VALUES")
log "Saved previous Helm values to $PREVIOUS_VALUES"

helm_args=(
  upgrade "$RELEASE" "$WORK/cluster/k8s/platform" -n "$NAMESPACE" -f "$PREVIOUS_VALUES"
  --set-string "imageTag=$TAG,backendImageTag=$TAG,frontendImageTag=$TAG"
  --set-string "provisioner.instanceMindroomImage=$MINDROOM_IMAGE"
  --wait --timeout 10m
)
if $DRY_RUN; then
  log "Rendering Helm upgrade (dry run, output hidden because it can contain secrets)"
  helm "${helm_args[@]}" --dry-run >/dev/null
  echo "  [dry-run] helm ${helm_args[*]}"
else
  log "Upgrading Helm release $RELEASE"
  helm "${helm_args[@]}" >/dev/null
fi

# --- 4a. Platform health ---
log "Checking $API/health"
curl -fsS --retry 10 --retry-delay 6 --retry-all-errors "$API/health"
echo

# --- 3. Re-provision instances ---
FAILED=()
HEALTH_URLS=()
count=0
while read -r row; do
  [ -n "$row" ] || continue
  id=$(jq -r .instance_id <<<"$row")
  status=$(jq -r .status <<<"$row")
  held=$(jq -r 'if .lifecycle_stopped_at then "true" else "false" end' <<<"$row")
  body=$(jq -c '{subscription_id, account_id, tier, instance_id}' <<<"$row")
  if $DRY_RUN; then
    echo "  [dry-run] POST $API/system/provision $body"
    continue
  fi
  # /system/provision is rate limited to 5 requests per minute.
  [ "$count" -eq 0 ] || sleep 13
  count=$((count + 1))
  log "Re-provisioning instance $id"
  response=$(provisioner_post /system/provision "$body") || true
  code=$(tail -n1 <<<"$response")
  result=$(sed '$d' <<<"$response")
  echo "  HTTP $code $result"
  if [[ "$code" != 2* ]] || [ "$(jq -r .success <<<"$result" 2>/dev/null)" != "true" ]; then
    FAILED+=("$id")
    continue
  fi
  if $held; then
    log "Instance $id is held by the subscription lifecycle; the provisioner kept it stopped"
    continue
  fi
  if [ "$status" = "stopped" ]; then
    # Re-provisioning starts a manually stopped instance, so stop it again.
    log "Instance $id was stopped before the deploy; stopping it again"
    response=$(provisioner_post "/system/instances/$id/stop") || true
    [[ "$(tail -n1 <<<"$response")" == 2* ]] || FAILED+=("$id")
    continue
  fi
  if kubectl rollout status "deployment/synapse-$id" -n mindroom-instances --timeout=10m &&
    kubectl rollout status "deployment/mindroom-$id" -n mindroom-instances --timeout=10m; then
    HEALTH_URLS+=("https://$id.$INSTANCE_DOMAIN/api/health")
  else
    FAILED+=("$id")
  fi
done < <(jq -c '.[]' <<<"$SELECTED")

# --- 4b. Instance health ---
for url in "${HEALTH_URLS[@]}"; do
  log "Checking $url"
  if curl -fsS --retry 10 --retry-delay 6 --retry-all-errors "$url" >/dev/null; then
    echo "  ok"
  else
    FAILED+=("$url")
  fi
done

if [ ${#FAILED[@]} -gt 0 ]; then
  log "Finished with failures: ${FAILED[*]}"
  exit 1
fi
if $DRY_RUN; then log "Dry run finished; nothing was changed"; else log "Release $TAG deployed"; fi
