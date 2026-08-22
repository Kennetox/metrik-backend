#!/usr/bin/env bash
set -euo pipefail

port="${PORT:-10000}"
host="${HOST:-0.0.0.0}"
local_api="http://127.0.0.1:${port}"

uvicorn main:app --host "${host}" --port "${port}" &
server_pid=$!
cleanup() {
  kill "${server_pid}" 2>/dev/null || true
}
trap cleanup TERM INT EXIT

ready_to_announce=0
for _ in {1..60}; do
  if curl --silent --show-error --fail "${local_api}/healthz" >/dev/null 2>&1; then
    ready_to_announce=1
    break
  fi
  sleep 1
done

if [[ "${ready_to_announce}" != "1" ]]; then
  echo "La nueva instancia no respondió durante el arranque." >&2
  exit 1
fi

if [[ -n "${DEPLOYMENT_STATUS_TOKEN:-}" ]]; then
  curl --fail-with-body --silent --show-error \
    -X POST "${local_api}/ops/system-status" \
    -H "X-Deployment-Status-Token: ${DEPLOYMENT_STATUS_TOKEN}" \
    -H "Content-Type: application/json" \
    --data '{"state":"healthy","updated_by":"render-start"}'
  echo "Nueva instancia saludable; aviso de mantenimiento cerrado."
fi

wait "${server_pid}"
