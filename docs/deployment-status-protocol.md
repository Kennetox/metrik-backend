# Protocolo de despliegue de Metrik

El estado de mantenimiento se guarda en la base de datos para que sobreviva al reinicio del backend. Antes de iniciar un despliegue, el responsable debe publicar el aviso; después de comprobar que la nueva versión responde, debe cerrarlo.

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
