#!/usr/bin/env bash
set -euo pipefail

STACK_DIR="${1:-}"
PROJECT_NAME="${PROJECT_NAME:-mindroom-stack-smoke}"
STACK_SYNAPSE_PORT="${STACK_SYNAPSE_PORT:-18008}"
STACK_MINDROOM_PORT="${STACK_MINDROOM_PORT:-18765}"
STACK_ELEMENT_PORT="${STACK_ELEMENT_PORT:-18080}"

if [ -z "${STACK_DIR}" ]; then
  echo "Usage: $0 /path/to/mindroom-stack" >&2
  exit 1
fi

if [ ! -f "${STACK_DIR}/compose.yaml" ]; then
  echo "[error] compose.yaml not found in ${STACK_DIR}" >&2
  exit 1
fi

TMP_ENV="$(mktemp)"
TMP_COMPOSE="$(mktemp "${STACK_DIR}/.smoke-compose.XXXXXX.yaml")"

cleanup() {
  docker compose --project-directory "${STACK_DIR}" --project-name "${PROJECT_NAME}" --env-file "${TMP_ENV}" -f "${TMP_COMPOSE}" down -v >/dev/null 2>&1 || true
  rm -f "${TMP_ENV}"
  rm -f "${TMP_COMPOSE}"
}
trap cleanup EXIT

cat >"${TMP_ENV}" <<'EOF'
POSTGRES_PASSWORD=synapse_password
MATRIX_SERVER_NAME=matrix.localhost
OPENAI_API_KEY=test-openai
ANTHROPIC_API_KEY=test-anthropic
GOOGLE_API_KEY=
OPENROUTER_API_KEY=
OLLAMA_HOST=http://localhost:11434
EOF

cat >>"${TMP_ENV}" <<EOF
ELEMENT_HOMESERVER_URL=http://localhost:${STACK_SYNAPSE_PORT}
EOF

sed \
  -e "s/\"8008:8008\"/\"127.0.0.1:${STACK_SYNAPSE_PORT}:8008\"/" \
  -e "s/\"8765:8765\"/\"127.0.0.1:${STACK_MINDROOM_PORT}:8765\"/" \
  -e "s/\"8080:8080\"/\"127.0.0.1:${STACK_ELEMENT_PORT}:8080\"/" \
  "${STACK_DIR}/compose.yaml" >"${TMP_COMPOSE}"

wait_for_status() {
  local url="$1"
  local expected="$2"
  local label="$3"

  for _ in $(seq 1 40); do
    if curl -fsS "${url}" | grep -q "${expected}"; then
      echo "[smoke] ${label} ready"
      return 0
    fi
    sleep 3
  done

  echo "[error] Timed out waiting for ${label} (${url})" >&2
  return 1
}

echo "[smoke] Starting mindroom-stack from ${STACK_DIR}"
docker compose --project-directory "${STACK_DIR}" --project-name "${PROJECT_NAME}" --env-file "${TMP_ENV}" -f "${TMP_COMPOSE}" up -d

wait_for_status "http://127.0.0.1:${STACK_MINDROOM_PORT}/api/health" "\"healthy\"" "MindRoom health"
wait_for_status "http://127.0.0.1:${STACK_MINDROOM_PORT}/" "MindRoom" "MindRoom dashboard"
wait_for_status "http://127.0.0.1:${STACK_SYNAPSE_PORT}/_matrix/client/versions" "\"versions\"" "Synapse"

for _ in $(seq 1 20); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${STACK_ELEMENT_PORT}/" || true)"
  if [ "${code}" = "200" ]; then
    echo "[smoke] Element ready"
    echo "[smoke] mindroom-stack checks passed"
    exit 0
  fi
  sleep 3
done

echo "[error] Timed out waiting for Element (http://127.0.0.1:${STACK_ELEMENT_PORT}/)" >&2
exit 1
