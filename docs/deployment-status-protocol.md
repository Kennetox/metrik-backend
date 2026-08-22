# Protocolo de despliegue de Metrik

El estado de mantenimiento se guarda en la base de datos para que sobreviva al reinicio del backend. El flujo recomendado automatiza ambos pasos con los comandos `pre-deploy` y `start` de Render: el primero publica el aviso antes de reemplazar la instancia y el segundo lo cierra cuando la nueva instancia ya inició correctamente.

## Configuración automática en Render

En **Settings** del servicio `metrik-api`, configura:

- **Pre-deploy Command:** `bash scripts/render_predeploy.sh`
- **Start Command:** `bash scripts/render_start.sh`

Además de `DEPLOYMENT_STATUS_TOKEN`, agrega `PUBLIC_API_BASE_URL` con `https://api.metrikpos.com`. Render ejecutará el pre-deploy después del build y antes de poner la nueva instancia en servicio; el comando de arranque limpiará el estado al iniciar la versión nueva.

La primera vez conviene hacer esta configuración cuando la versión que contiene estos scripts ya esté desplegada. Después, los pushes y los despliegues manuales seguirán el flujo automáticamente.

El flujo automatizado fue validado en producción con un despliegue controlado.

## Antes del despliegue

Configura `DEPLOYMENT_STATUS_TOKEN` como secreto en Render y localmente en la terminal. No lo guardes en el repositorio.

```bash
curl -sS -X POST https://api.metrikpos.com/ops/system-status \
  -H "X-Deployment-Status-Token: $DEPLOYMENT_STATUS_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"state":"maintenance","message":"Estamos actualizando Metrik. El servicio volverá en unos minutos.","updated_by":"render"}'
```

Espera la respuesta JSON con `"state":"maintenance"` y luego inicia el deploy en Render. El panel web y las versiones nuevas de Metrik POS consultan `/readyz` cada cinco segundos.

## Después del despliegue

Primero comprueba que la versión nueva está lista:

```bash
curl -i https://api.metrikpos.com/readyz
```

Cuando responda `200` con `"ready":true`, limpia el aviso:

```bash
curl -sS -X POST https://api.metrikpos.com/ops/system-status \
  -H "X-Deployment-Status-Token: $DEPLOYMENT_STATUS_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"state":"healthy","updated_by":"render"}'
```

Si el backend se cae sin aviso previo, `/readyz` devuelve un incidente y las interfaces lo muestran como error. El aviso de mantenimiento requiere ejecutar el primer paso antes de reiniciar el servicio.
