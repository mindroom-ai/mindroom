#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
INSTANCE_ID="${INSTANCE_ID:-1}"
INSTANCE_NAMESPACE="${INSTANCE_NAMESPACE:-mindroom-instances}"
PLATFORM_NAMESPACE="${PLATFORM_NAMESPACE:-mindroom-staging}"
BASE_DOMAIN="${BASE_DOMAIN:-local}"
ACCOUNT_ID="${ACCOUNT_ID:-acct-kindtest}"
MINDROOM_IMAGE="${MINDROOM_IMAGE:-ghcr.io/mindroom-ai/mindroom:latest}"
MINDROOM_IMAGE_PULL_POLICY="${MINDROOM_IMAGE_PULL_POLICY:-IfNotPresent}"
PLATFORM_BACKEND_LOCAL_PORT="${PLATFORM_BACKEND_LOCAL_PORT:-18000}"
PLATFORM_FRONTEND_LOCAL_PORT="${PLATFORM_FRONTEND_LOCAL_PORT:-13000}"
MINDROOM_LOCAL_PORT="${MINDROOM_LOCAL_PORT:-18765}"
SYNAPSE_LOCAL_PORT="${SYNAPSE_LOCAL_PORT:-18008}"
PLATFORM_HEALTH_URL="http://127.0.0.1:${PLATFORM_BACKEND_LOCAL_PORT}/health"
PLATFORM_UI_URL="http://127.0.0.1:${PLATFORM_FRONTEND_LOCAL_PORT}/"
MINDROOM_HEALTH_URL="http://127.0.0.1:${MINDROOM_LOCAL_PORT}/api/health"
MINDROOM_UI_URL="http://127.0.0.1:${MINDROOM_LOCAL_PORT}/"
SYNAPSE_URL="http://127.0.0.1:${SYNAPSE_LOCAL_PORT}/_matrix/client/versions"

TMP_DIR="$(mktemp -d)"
PF_PLATFORM_BACKEND_PID=""
PF_PLATFORM_FRONTEND_PID=""
PF_MINDROOM_PID=""
PF_SYNAPSE_PID=""

cleanup() {
  for pid in "$PF_PLATFORM_BACKEND_PID" "$PF_PLATFORM_FRONTEND_PID" "$PF_MINDROOM_PID" "$PF_SYNAPSE_PID"; do
    if [ -n "${pid}" ]; then
      kill "${pid}" >/dev/null 2>&1 || true
      wait "${pid}" 2>/dev/null || true
    fi
  done
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

wait_for_url() {
  local url="$1"
  local expected="$2"
  local label="$3"

  for _ in $(seq 1 30); do
    if curl -fsS "${url}" | grep -q "${expected}"; then
      echo "[smoke] ${label} ready"
      return 0
    fi
    sleep 2
  done

  echo "[error] Timed out waiting for ${label} (${url})" >&2
  return 1
}

wait_for_port_forward() {
  local local_port="$1"
  local pid="$2"
  local log_file="$3"
  local label="$4"

  for _ in $(seq 1 30); do
    if ! kill -0 "${pid}" >/dev/null 2>&1; then
      echo "[error] ${label} port-forward exited early" >&2
      cat "${log_file}" >&2 || true
      return 1
    fi

    if bash -c "exec 3<>/dev/tcp/127.0.0.1/${local_port}" >/dev/null 2>&1; then
      echo "[smoke] ${label} port-forward ready"
      return 0
    fi

    sleep 1
  done

  echo "[error] Timed out waiting for ${label} port-forward on 127.0.0.1:${local_port}" >&2
  cat "${log_file}" >&2 || true
  return 1
}

start_port_forward() {
  local namespace="$1"
  local resource="$2"
  local local_port="$3"
  local remote_port="$4"
  local log_file="$5"
  local label="$6"

  kubectl port-forward --address 127.0.0.1 -n "${namespace}" "${resource}" "${local_port}:${remote_port}" >"${log_file}" 2>&1 &
  local pid=$!
  wait_for_port_forward "${local_port}" "${pid}" "${log_file}" "${label}"
  echo "${pid}"
}

kubectl get namespace "${INSTANCE_NAMESPACE}" >/dev/null 2>&1 || kubectl create namespace "${INSTANCE_NAMESPACE}"

echo "[helm] Deploying instance ${INSTANCE_ID}..."
helm upgrade --install "instance-${INSTANCE_ID}" "${ROOT_DIR}/cluster/k8s/instance" \
  --namespace "${INSTANCE_NAMESPACE}" \
  --create-namespace \
  --set "customer=${INSTANCE_ID}" \
  --set "baseDomain=${BASE_DOMAIN}" \
  --set "accountId=${ACCOUNT_ID}" \
  --set "storageClassName=standard" \
  --set "mindroom_image=${MINDROOM_IMAGE}" \
  --set "mindroom_image_pull_policy=${MINDROOM_IMAGE_PULL_POLICY}" \
  --set "openai_key=test-openai" \
  --set "anthropic_key=test-anthropic" \
  --set "google_key=test-google" \
  --set "openrouter_key=test-openrouter" \
  --set "deepseek_key=test-deepseek" \
  --set "sandbox_proxy_token=test-sandbox-token"

kubectl rollout status "deployment/mindroom-${INSTANCE_ID}" -n "${INSTANCE_NAMESPACE}" --timeout=300s
kubectl rollout status "deployment/synapse-${INSTANCE_ID}" -n "${INSTANCE_NAMESPACE}" --timeout=300s

PF_PLATFORM_BACKEND_PID="$(start_port_forward "${PLATFORM_NAMESPACE}" svc/platform-backend "${PLATFORM_BACKEND_LOCAL_PORT}" 8000 "${TMP_DIR}/pf-platform-backend.log" "platform backend")"
PF_PLATFORM_FRONTEND_PID="$(start_port_forward "${PLATFORM_NAMESPACE}" svc/platform-frontend "${PLATFORM_FRONTEND_LOCAL_PORT}" 3000 "${TMP_DIR}/pf-platform-frontend.log" "platform frontend")"
PF_MINDROOM_PID="$(start_port_forward "${INSTANCE_NAMESPACE}" "svc/mindroom-${INSTANCE_ID}" "${MINDROOM_LOCAL_PORT}" 8765 "${TMP_DIR}/pf-mindroom.log" "MindRoom")"
PF_SYNAPSE_PID="$(start_port_forward "${INSTANCE_NAMESPACE}" "svc/synapse-${INSTANCE_ID}" "${SYNAPSE_LOCAL_PORT}" 8008 "${TMP_DIR}/pf-synapse.log" "Synapse")"

wait_for_url "${PLATFORM_HEALTH_URL}" "\"status\"" "platform backend health"
wait_for_url "${PLATFORM_UI_URL}" "MindRoom" "platform frontend"
wait_for_url "${MINDROOM_HEALTH_URL}" "\"healthy\"" "MindRoom health"
wait_for_url "${MINDROOM_UI_URL}" "MindRoom" "MindRoom dashboard"
wait_for_url "${SYNAPSE_URL}" "\"versions\"" "instance Synapse"

echo "[smoke] kind platform + instance checks passed"
