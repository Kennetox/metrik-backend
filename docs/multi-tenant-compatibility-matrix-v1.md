# Matriz de Compatibilidad Multi-tenant v1

Fecha: 2026-03-08
Alcance: proteger integraciones existentes antes de introducir `tenant_id`.

## Objetivo
Congelar el contrato minimo que NO se puede romper para:
- `kensar_frontend` (web/dashboard/POS web)
- `kensar_pos_desktop`
- `kensar_mobile` (Metrik Stock)
- `kensar_pos_tablet`
- `print-agent-tray`

## Regla de oro de esta fase
No se cambian rutas, ni forma base de respuestas, ni semantica de login/sesion.
La multi-tenencia se introduce de forma interna en backend (resolucion de tenant en servidor).

## App por app

### 1) kensar_frontend
Dependencia alta del backend. Usa de forma extensa:
- `POST /auth/login`, `POST /auth/pos-login`, `POST /auth/logout`, `GET /auth/session-status`
- Modulos `pos/*`, `dashboard/*`, `inventory/*`, `receiving/*`, `hr/*`, `schedule/*`, `reports/*`, `separated-orders/*`, `products/*`, `product-groups/*`, `uploads/*`
- QZ: `GET /pos/qz/cert`, `POST /pos/qz/sign`

Riesgo de ruptura:
- Alto si cambian campos de `PosUserRead`, `AuthLoginResponse`, `pos/settings`, `pos/stations`, `pos/sales*`.

Condicion de compatibilidad:
- Mantener respuestas actuales; cualquier campo nuevo debe ser aditivo (optional para clientes viejos).

### 2) kensar_pos_desktop
Contrato detectado:
- `POST /auth/pos-station-login`
- `POST /auth/logout`
- Base API configurable (`POS_API_BASE_URL`), por defecto `https://api.metrikpos.com`.

Riesgo de ruptura:
- Critico si cambia payload de `pos-station-login`:
  - request: `station_email`, `station_password`, `device_id`, `device_label`
  - response esperada: `station_id`, `station_label`, `station_email`

Condicion de compatibilidad:
- Este endpoint debe permanecer estable durante toda la migracion.

### 3) kensar_mobile (Metrik Stock)
Contrato detectado:
- Auth:
  - `POST /auth/login`
  - `POST /auth/tablet-login` (fallback a `POST /auth/pos-login` cuando hay 404)
  - `POST /auth/tablet-email-check`
  - `GET /auth/session-status`
- Receiving:
  - `GET/POST/PATCH /receiving/lots...`
  - `GET /receiving/products/search`
  - `POST /receiving/products/quick-create`
  - `GET /receiving/products/next-codes`
  - `POST /receiving/lots/{id}/support-file`
  - `GET /receiving/documents`, `GET /receiving/products/created`

Riesgo de ruptura:
- Alto en `receiving/*` y auth tablet.

Condicion de compatibilidad:
- Mantener `tablet-login` y fallback funcional a `pos-login` en transicion.
- Mantener rutas/formatos de archivos de soporte.

### 4) kensar_pos_tablet
Contrato detectado:
- Auth:
  - `POST /auth/tablet-login` (fallback a `/auth/pos-login`)
  - `POST /auth/pos-station-login`
  - `POST /auth/logout`
  - `GET /auth/session-status`
- POS:
  - `GET /products`
  - `GET /pos/payment-methods`
  - `GET/POST /pos/customers`
  - `GET /pos/sales/next-number`
  - `POST /pos/sales/reserve-number`
  - `POST /pos/sales/reservations/{id}/cancel`
  - `POST /pos/sales`
  - `GET /pos/sales/{id}/document`, `GET /pos/sales/{id}/document-view`
  - `POST /pos/sales/{id}/email`

Riesgo de ruptura:
- Critico en numeracion/reserva de ventas y login de estacion.

Condicion de compatibilidad:
- No cambiar semantica de reserva/cancelacion durante migracion.

### 5) print-agent-tray
Hallazgo:
- No depende del backend Metrik para operar.
- Expone servicio local `127.0.0.1:5177` (`/health`, `/config`, `/printers/*`, `/print`).
- La web/POS le habla localmente.

Impacto multi-tenant:
- Nulo directo en DB/API Metrik.

Consideracion futura:
- Si se agrega telemetria por tenant, debe ser opcional y no bloquear impresion local.

## Reglas de implementacion para no romper ecosistema

1. Resolucion de tenant solo en backend
- Nunca confiar en `tenant_id` enviado por clientes actuales.
- Resolver tenant desde sesion/token y/o estacion vinculada.

2. Compatibilidad hacia atras en auth
- Mantener activos: `/auth/pos-login`, `/auth/tablet-login`, `/auth/pos-station-login`.
- No renombrar ni mover en esta fase.

3. Migracion aditiva
- Agregar `tenant_id` nullable + backfill.
- Luego `NOT NULL` cuando todas las rutas ya filtren por tenant.

4. Unicidades compuestas
- Migrar gradualmente de unique global a unique por tenant en tablas clave.

5. Bandera de seguridad
- Feature flag para modo tenant estricto (inicialmente apagado en produccion).

## Checklist de validacion de regresion (antes y despues de cada migracion)

- Login dashboard web: OK
- Login POS web por PIN/correo: OK
- Login POS Desktop por estacion: OK
- Login Metrik Stock (tablet-login): OK
- Login POS Tablet (tablet-login y pos-station-login): OK
- Crear venta POS web: OK
- Crear venta POS tablet: OK
- Crear lote recepcion en Metrik Stock: OK
- Impresion etiqueta via print-agent-tray: OK
- Impresion ticket/QZ desde web: OK

## Siguiente entregable tecnico
Definir `tenant resolver` unico en backend y usarlo primero en lectura para:
- `auth/session-status`
- `pos/settings`
- `pos/stations`
- `pos/payment-methods`
- `products`

Sin tocar contratos externos.
