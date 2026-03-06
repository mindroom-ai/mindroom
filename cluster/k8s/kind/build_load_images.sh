#!/usr/bin/env bash
set -euo pipefail

# Build local images for platform-frontend and platform-backend and load them into kind

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
CLUSTER_NAME="mindroom"

# Image coordinates used by the Helm chart defaults
REGISTRY="ghcr.io/mindroom-ai"
BACKEND_IMAGE="${REGISTRY}/platform-backend:latest"
FRONTEND_IMAGE="${REGISTRY}/platform-frontend:latest"

echo "[images] Building platform images tagged to chart defaults:" \
     "${BACKEND_IMAGE} and ${FRONTEND_IMAGE}"

pushd "${ROOT_DIR}" >/dev/null

# Build frontend
docker build \
  -t "${FRONTEND_IMAGE}" \
  -f saas-platform/Dockerfile.platform-frontend .

# Build backend
docker build \
  -t "${BACKEND_IMAGE}" \
  -f saas-platform/Dockerfile.platform-backend .

echo "[images] Loading images into kind cluster '${CLUSTER_NAME}'..."
kind load docker-image "${FRONTEND_IMAGE}" --name "${CLUSTER_NAME}"
kind load docker-image "${BACKEND_IMAGE}" --name "${CLUSTER_NAME}"

echo "[images] Done. Helm will use these images with imagePullPolicy=IfNotPresent."

popd >/dev/null
