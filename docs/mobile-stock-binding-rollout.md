# Mobile Stock Binding Rollout

## Objetivo

Migrar Metrik Stock desde login anclado a un correo persistido por tablet hacia:

- vinculación administrativa por código temporal
- login diario únicamente por PIN personal
- compatibilidad temporal con el flujo legado por correo

## Componentes que deben desplegarse juntos

1. `kensar_backend`
2. `kensar_frontend`
3. `kensar_mobile`

No se recomienda publicar solo la app móvil sin el backend nuevo.

## Cambios de backend

- `POST /stock/devices/setup-code`
- `POST /auth/mobile-stock-bind`
- `POST /auth/mobile-stock-login`
  - ahora acepta `stock_device_id`
  - mantiene compatibilidad con `email` legado

## Migración de datos

No se borra configuración local de tablets existentes.

La migración agrega columnas nuevas en `stock_devices`:

- `setup_code_hash`
- `setup_code_expires_at`

Las migraciones del backend deben ejecutarse antes de habilitar la APK nueva.

## Flujo de rollout recomendado

1. Desplegar backend nuevo.
2. Verificar que la tabla `stock_devices` tenga las columnas nuevas.
3. Desplegar frontend nuevo.
4. Confirmar que en `Dashboard > Configuración > Dispositivos Metrik Stock` aparezca:
   - `Preparar nueva tablet`
   - acción `Código`
5. Publicar APK nueva.

## Migración de tablets existentes

### Caso 1: tablet ya operativa y no se quiere tocar de inmediato

- puede seguir entrando por correo legado + PIN
- no pierde `stationId`, `stationLabel`, `stockDeviceId` ni configuración local

### Caso 2: tablet existente que se quiere pasar al flujo nuevo

1. Admin genera un código desde Metrik.
2. En la tablet, abrir configuración.
3. Ingresar el código en `Re-vincular con código`.
4. Confirmar que después el acceso diario quede solo por PIN.

### Caso 3: tablet nueva

1. Configurar `ID local` y `Nombre`.
2. Vincular con código.
3. Operar normalmente con PIN.

## Validaciones mínimas post deploy

1. Crear código para una tablet nueva.
2. Vincular la tablet sin correo.
3. Iniciar sesión con PIN de dos usuarios distintos en la misma empresa.
4. Confirmar que trazabilidad quede con usuarios distintos.
5. Confirmar que una tablet vieja aún pueda usar el flujo legado si no ha sido migrada.

## Rollback

Si la APK nueva presenta problemas:

- backend y frontend nuevos pueden quedarse desplegados
- las tablets no migradas aún pueden usar el flujo legado
- basta con detener la distribución de la APK nueva mientras se corrige

El cambio no requiere limpiar datos locales en tablets.
