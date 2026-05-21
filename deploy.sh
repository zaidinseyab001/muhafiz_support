#!/usr/bin/env bash
# Airgapped deploy: load images, then run the stack. Idempotent.
#
# Usage (on the airgapped host, after extracting muhafiz_support.tar):
#   cd muhafiz_support
#   # edit .env as needed
#   # (optional) edit ollama/entrypoint.sh
#   ./deploy.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "${HERE}"

PROJECT_NAME="muhafiz_support"
IMAGES_DIR="${HERE}/_bundle/images"

if [ ! -d "${IMAGES_DIR}" ]; then
    echo "ERROR: ${IMAGES_DIR} missing — did you extract muhafiz_support.tar correctly?" >&2
    exit 1
fi

echo "==> [1/2] Loading docker images"
for img in "${IMAGES_DIR}"/*.tar; do
    echo "         loading $(basename "${img}")"
    docker load -i "${img}"
done

echo "==> [2/2] Starting the stack"
docker compose -p "${PROJECT_NAME}" up -d

echo ""
echo "Stack is up. Tail logs with:"
echo "  docker compose -p ${PROJECT_NAME} logs -f"
