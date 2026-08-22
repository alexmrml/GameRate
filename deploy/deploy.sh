#!/usr/bin/env bash
set -Eeuo pipefail

readonly IMAGE_REF="${1:-}"
readonly DEPLOY_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly COMPOSE_FILE="${DEPLOY_DIR}/compose.production.yml"
readonly APP_ENV_FILE="${DEPLOY_DIR}/.env"
readonly IMAGE_ENV_FILE="${DEPLOY_DIR}/.image.env"

if [[ ! "${IMAGE_REF}" =~ ^ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+@sha256:[a-f0-9]{64}$ ]]; then
  echo "usage: $0 ghcr.io/owner/image@sha256:<64 hex characters>" >&2
  exit 2
fi

if [[ ! -f "${APP_ENV_FILE}" ]]; then
  echo "missing ${APP_ENV_FILE}; create it from .env.production.example" >&2
  exit 2
fi

compose() {
  APP_IMAGE="${1}" docker compose \
    --project-directory "${DEPLOY_DIR}" \
    --env-file "${APP_ENV_FILE}" \
    -f "${COMPOSE_FILE}" "${@:2}"
}

wait_for_health() {
  local response
  for _ in {1..45}; do
    if response="$(curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8000/health 2>/dev/null)" \
      && grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' <<<"${response}" \
      && grep -Eq '"active_workers"[[:space:]]*:[[:space:]]*[1-9][0-9]*' <<<"${response}"; then
      return 0
    fi
    sleep 2
  done
  return 1
}

old_image=""
if [[ -f "${IMAGE_ENV_FILE}" ]]; then
  old_image="$(sed -n 's/^APP_IMAGE=//p' "${IMAGE_ENV_FILE}" | head -n 1)"
fi

echo "Pulling ${IMAGE_REF}"
compose "${IMAGE_REF}" pull web worker

echo "Starting GameRate"
if ! compose "${IMAGE_REF}" up -d --remove-orphans --wait --wait-timeout 180 \
  || ! wait_for_health; then
  echo "Deployment failed health checks" >&2
  if [[ -n "${old_image}" && "${old_image}" != "${IMAGE_REF}" ]]; then
    echo "Restoring ${old_image}" >&2
    compose "${old_image}" up -d --remove-orphans --wait --wait-timeout 180 || true
  fi
  exit 1
fi

umask 077
printf 'APP_IMAGE=%s\n' "${IMAGE_REF}" >"${IMAGE_ENV_FILE}.tmp"
mv -f "${IMAGE_ENV_FILE}.tmp" "${IMAGE_ENV_FILE}"

echo "Deployment healthy: ${IMAGE_REF}"
compose "${IMAGE_REF}" ps
