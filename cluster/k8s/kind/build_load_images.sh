#!/usr/bin/env bash
set -euo pipefail

# Build local images for the platform and MindRoom runtime, then load them into kind.

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
CLUSTER_NAME="mindroom"
BUILD_PLATFORM_IMAGES="${BUILD_PLATFORM_IMAGES:-1}"
BUILD_MINDROOM_IMAGES="${BUILD_MINDROOM_IMAGES:-1}"

# Image coordinates used by the Helm chart defaults
REGISTRY="ghcr.io/mindroom-ai"
PLATFORM_BACKEND_IMAGE="${REGISTRY}/platform-backend:latest"
PLATFORM_FRONTEND_IMAGE="${REGISTRY}/platform-frontend:latest"
MINDROOM_IMAGE="${REGISTRY}/mindroom:latest"
MINDROOM_MINIMAL_IMAGE="${REGISTRY}/mindroom-minimal:latest"
TUWUNEL_IMAGE="${TUWUNEL_IMAGE:-ghcr.io/mindroom-ai/mindroom-tuwunel:latest}"

echo "[images] Building images tagged to chart/runtime defaults:"
echo "  - ${PLATFORM_BACKEND_IMAGE}"
echo "  - ${PLATFORM_FRONTEND_IMAGE}"
echo "  - ${MINDROOM_IMAGE}"
echo "  - ${MINDROOM_MINIMAL_IMAGE}"
echo "  - ${TUWUNEL_IMAGE}"

pushd "${ROOT_DIR}" >/dev/null

if [ "${BUILD_PLATFORM_IMAGES}" = "1" ]; then
  echo "[images] Building platform images locally..."
  docker build \
    -t "${PLATFORM_FRONTEND_IMAGE}" \
    -f saas-platform/Dockerfile.platform-frontend .

  docker build \
    -t "${PLATFORM_BACKEND_IMAGE}" \
    -f saas-platform/Dockerfile.platform-backend .
else
  echo "[images] Pulling published platform images..."
  docker pull "${PLATFORM_FRONTEND_IMAGE}"
  docker pull "${PLATFORM_BACKEND_IMAGE}"
fi

if [ "${BUILD_MINDROOM_IMAGES}" = "1" ]; then
  echo "[images] Building MindRoom images locally..."
  docker build \
    -t "${MINDROOM_IMAGE}" \
    -f local/instances/deploy/Dockerfile.mindroom .

  docker build \
    -t "${MINDROOM_MINIMAL_IMAGE}" \
    -f local/instances/deploy/Dockerfile.mindroom-minimal .
else
  echo "[images] Pulling published MindRoom images..."
  docker pull "${MINDROOM_IMAGE}"
  docker pull "${MINDROOM_MINIMAL_IMAGE}"
fi

echo "[images] Pulling Tuwunel image..."
docker pull "${TUWUNEL_IMAGE}"

# Docker's containerd image store keeps a pulled image's multi-platform index without the other platforms' layers.
# `kind load docker-image` imports every platform in that index and fails with "content digest ... not found" (kubernetes-sigs/kind#3795).
# Exporting only the Docker host's platform gives kind a complete archive with either image store.
DOCKER_PLATFORM="$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')"
IMAGE_ARCHIVE_DIR="$(mktemp -d)"
trap 'rm -rf "${IMAGE_ARCHIVE_DIR}"' EXIT

load_image() {
  local archive="${IMAGE_ARCHIVE_DIR}/image.tar"
  docker image save --platform "${DOCKER_PLATFORM}" --output "${archive}" "$1"
  kind load image-archive "${archive}" --name "${CLUSTER_NAME}"
  rm -f "${archive}"
}

echo "[images] Loading ${DOCKER_PLATFORM} images into kind cluster '${CLUSTER_NAME}'..."
load_image "${PLATFORM_FRONTEND_IMAGE}"
load_image "${PLATFORM_BACKEND_IMAGE}"
load_image "${MINDROOM_IMAGE}"
load_image "${TUWUNEL_IMAGE}"

echo "[images] Done. Helm will use these images with imagePullPolicy=IfNotPresent."

popd >/dev/null
