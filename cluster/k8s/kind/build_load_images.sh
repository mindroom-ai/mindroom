#!/usr/bin/env bash
set -euo pipefail

# Build local images for the platform and MindRoom runtime, then load them into kind.

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
CLUSTER_NAME="mindroom"

# Image coordinates used by the Helm chart defaults
REGISTRY="ghcr.io/mindroom-ai"
PLATFORM_BACKEND_IMAGE="${REGISTRY}/platform-backend:latest"
PLATFORM_FRONTEND_IMAGE="${REGISTRY}/platform-frontend:latest"
MINDROOM_IMAGE="${REGISTRY}/mindroom:latest"
MINDROOM_MINIMAL_IMAGE="${REGISTRY}/mindroom-minimal:latest"

echo "[images] Building images tagged to chart/runtime defaults:"
echo "  - ${PLATFORM_BACKEND_IMAGE}"
echo "  - ${PLATFORM_FRONTEND_IMAGE}"
echo "  - ${MINDROOM_IMAGE}"
echo "  - ${MINDROOM_MINIMAL_IMAGE}"

pushd "${ROOT_DIR}" >/dev/null

# Build platform frontend
docker build \
  -t "${PLATFORM_FRONTEND_IMAGE}" \
  -f saas-platform/Dockerfile.platform-frontend .

# Build platform backend
docker build \
  -t "${PLATFORM_BACKEND_IMAGE}" \
  -f saas-platform/Dockerfile.platform-backend .

# Build MindRoom runtime images
docker build \
  -t "${MINDROOM_IMAGE}" \
  -f local/instances/deploy/Dockerfile.backend .

docker build \
  -t "${MINDROOM_MINIMAL_IMAGE}" \
  -f local/instances/deploy/Dockerfile.backend-minimal .

echo "[images] Loading images into kind cluster '${CLUSTER_NAME}'..."
kind load docker-image "${PLATFORM_FRONTEND_IMAGE}" --name "${CLUSTER_NAME}"
kind load docker-image "${PLATFORM_BACKEND_IMAGE}" --name "${CLUSTER_NAME}"
kind load docker-image "${MINDROOM_IMAGE}" --name "${CLUSTER_NAME}"
kind load docker-image "${MINDROOM_MINIMAL_IMAGE}" --name "${CLUSTER_NAME}"

echo "[images] Done. Helm will use these images with imagePullPolicy=IfNotPresent."

popd >/dev/null
