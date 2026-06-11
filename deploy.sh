#!/usr/bin/env bash
# Airgapped deploy: load image(s), then run the stack. Idempotent.
#
# Usage (on the airgapped host, after extracting muhafiz_support.tar):
#   cd muhafiz_support
#   # edit .env as needed
#   # (optional) edit ollama/entrypoint.sh
#   ./deploy.sh                  load ALL images + start the whole stack
#   ./deploy.sh <service>        load ONLY that service's image + (re)start it.
#                                <service> = ollama | yolo | live_feeds | whisper
#
# The single-service form is for updating one service after shipping a fresh
# per-service image tar (built with: ./scripts/build_airgap.sh <service>).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "${HERE}"

PROJECT_NAME="muhafiz_support"
IMAGES_DIR="${HERE}/_bundle/images"

if [ ! -d "${IMAGES_DIR}" ]; then
    echo "ERROR: ${IMAGES_DIR} missing — did you extract muhafiz_support.tar correctly?" >&2
    exit 1
fi

SERVICE="${1:-}"

# Map a compose service name to its saved image tarball.
svc_tar() {
    case "$1" in
        ollama)     echo "ollama.tar" ;;
        yolo)       echo "yolo_inference.tar" ;;
        live_feeds) echo "live_data_feeds.tar" ;;
        whisper)    echo "whisper.tar" ;;
        *)          return 1 ;;
    esac
}

if [ -n "${SERVICE}" ]; then
    # ---- single-service update ----
    TAR_NAME="$(svc_tar "${SERVICE}")" || {
        echo "ERROR: unknown service '${SERVICE}'. Valid: ollama | yolo | live_feeds | whisper (or omit for all)." >&2
        exit 1
    }
    TAR_PATH="${IMAGES_DIR}/${TAR_NAME}"
    [ -f "${TAR_PATH}" ] || {
        echo "ERROR: ${TAR_PATH} not found — copy the per-service image tar there first." >&2
        exit 1
    }

    echo "==> [1/2] Loading image for ${SERVICE} (${TAR_NAME})"
    docker load -i "${TAR_PATH}"

    echo "==> [2/2] (Re)starting ${SERVICE}"
    # --force-recreate so the container always picks up the just-loaded image,
    # even though the tag (muhafiz/${SERVICE}:latest) is unchanged.
    docker compose -p "${PROJECT_NAME}" up -d --force-recreate "${SERVICE}"

    echo ""
    echo "${SERVICE} updated. Tail logs with:"
    echo "  docker compose -p ${PROJECT_NAME} logs -f ${SERVICE}"
    exit 0
fi

# ---- full deploy ----
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
