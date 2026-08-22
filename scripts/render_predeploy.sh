#!/usr/bin/env bash
set -euo pipefail

api_base="${PUBLIC_API_BASE_URL:-https://api.metrikpos.com}"
api_base="${api_base%/}"

if [[ -z "${DEPLOYMENT_STATUS_TOKEN:-}" ]]; then
  echo "DEPLOYMENT_STATUS_TOKEN no está configurado; no se puede anunciar el mantenimiento." >&2
  exit 1
fi

curl --fail-with-body --silent --show-error --retry 3 --retry-delay 2 \
  -X POST "${api_base}/ops/system-status" \
  -H "X-Deployment-Status-Token: ${DEPLOYMENT_STATUS_TOKEN}" \
  -H "Content-Type: application/json" \
  --data '{"state":"maintenance","message":"Estamos actualizando Metrik. El servicio volverá en unos minutos.","updated_by":"render-predeploy"}'

echo "Aviso de mantenimiento publicado antes del despliegue."
