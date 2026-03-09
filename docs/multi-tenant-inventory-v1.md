# Inventario Multi-tenant v1 (Metrik)

Fecha: 2026-03-08  
Fuente: `models.py` y `db_migrations.py`

## Objetivo de este documento
Definir el primer entregable de la migracion multi-tenant: inventario de tablas y clasificacion de alcance para introducir `tenant_id` sin riesgo para la data actual de Kensar.

## Decision base (v1)
- Modelo objetivo: **una sola base de datos + una sola estructura + `tenant_id` en tablas de negocio**.
- Tenant inicial de produccion: `kensar`.
- No se ejecutan cambios destructivos en esta fase.

## Tablas detectadas (modelo actual)
1. `products`
2. `product_audit_logs`
3. `product_groups`
4. `inventory_movements`
5. `receiving_lots`
6. `receiving_lot_items`
7. `sales`
8. `document_adjustments`
9. `sale_number_reservations`
10. `sale_items`
11. `sale_payments`
12. `payment_methods`
13. `pos_settings`
14. `pos_customers`
15. `hr_employees`
16. `schedule_templates`
17. `schedule_weeks`
18. `schedule_shifts`
19. `pos_users`
20. `pos_sessions`
21. `pos_user_documents`
22. `hr_employee_documents`
23. `password_resets`
24. `pos_stations`
25. `pos_station_notices`
26. `pos_closures`
27. `separated_orders`
28. `separated_order_payments`
29. `sale_returns`
30. `sale_return_items`
31. `sale_return_payments`
32. `sale_changes`
33. `sale_change_return_items`
34. `sale_change_new_items`
35. `sale_change_payments`

## Clasificacion de alcance

### A) Tablas de negocio (deben llevar `tenant_id`)
- `products`
- `product_audit_logs`
- `product_groups`
- `inventory_movements`
- `receiving_lots`
- `receiving_lot_items`
- `sales`
- `document_adjustments`
- `sale_number_reservations`
- `sale_items`
- `sale_payments`
- `payment_methods`
- `pos_settings`
- `pos_customers`
- `hr_employees`
- `schedule_templates`
- `schedule_weeks`
- `schedule_shifts`
- `pos_users`
- `pos_sessions`
- `pos_user_documents`
- `hr_employee_documents`
- `password_resets`
- `pos_stations`
- `pos_station_notices`
- `pos_closures`
- `separated_orders`
- `separated_order_payments`
- `sale_returns`
- `sale_return_items`
- `sale_return_payments`
- `sale_changes`
- `sale_change_return_items`
- `sale_change_new_items`
- `sale_change_payments`

### B) Tablas de plataforma (nuevas)
Estas no existen hoy y deben crearse antes del backfill:
- `tenants`
- `tenant_users` (si un usuario puede pertenecer a varios tenants)  
  o alternativa simple: `pos_users.tenant_id` (un usuario pertenece a un solo tenant).

## Puntos criticos de integridad (unicidades globales actuales)
Estas reglas hoy son globales y deben revisarse para pasar a multi-tenant:
- `products.sku` (unique)
- `product_groups.path` (unique)
- `sales.document_number` (unique)
- `sale_number_reservations.sale_number` (unique)
- `sale_number_reservations.document_number` (unique)
- `payment_methods.slug` (unique)
- `pos_users.email` (unique)
- `schedule_weeks.week_start` (unique)
- `pos_closures.consecutive` (unique)
- `sale_returns.document_number` (unique)
- `sale_changes.document_number` (unique)

## Orden recomendado de migracion (solo diseno, sin ejecucion)

### Fase 0 - Resguardo
- Backup completo de Postgres.
- Prueba de restauracion en entorno aislado.

### Fase 1 - Estructura base
- Crear tabla `tenants`.
- Insertar tenant inicial `kensar`.
- Agregar columna `tenant_id` (nullable) a tablas de negocio maestras:
  - `pos_users`, `pos_settings`, `payment_methods`, `products`, `product_groups`, `pos_customers`, `pos_stations`, `hr_employees`, `schedule_templates`, `schedule_weeks`.

### Fase 2 - Backfill controlado
- Backfill `tenant_id` = `kensar` en tablas maestras.
- Backfill en tablas transaccionales por lotes:
  - `sales`, `sale_items`, `sale_payments`, `pos_closures`, `separated_orders`, `separated_order_payments`, devoluciones/cambios, lotes, movimientos, documentos.

### Fase 3 - Endurecimiento
- `tenant_id` -> `NOT NULL`.
- FKs a `tenants(id)`.
- Reemplazar unicidades globales por compuestas con tenant cuando aplique:
  - ejemplo: `UNIQUE (tenant_id, sku)`.

### Fase 4 - Capa aplicativa
- Resolver `tenant_id` desde sesion/token en backend.
- Todo CRUD filtra por tenant.
- Prohibir confiar en `tenant_id` enviado por frontend.

### Fase 5 - Blindaje
- Activar Row Level Security (RLS) en tablas sensibles.
- Tests de aislamiento (tenant A no puede leer/escribir tenant B).

## Riesgos a vigilar
- Consultas legacy sin filtro tenant en `crud.py`.
- Endpoints que usan llaves unicas globales (document numbers, emails, station email).
- Dependencias cruzadas por FK sin tenant consistente.

## Primer entregable completado
Este documento cubre el **inventario y clasificacion**, que es el primer punto para proceder con seguridad.

## Siguiente entregable sugerido
Generar matriz tabla-por-tabla con:
- columna `tenant_id` tipo,
- estrategia de backfill,
- nuevos indices,
- reglas de unicidad finales,
- query de validacion post-migracion.

## Documento complementario
Para proteger integraciones activas del ecosistema (web, desktop, mobile, tablet y print-agent), ver:
- `docs/multi-tenant-compatibility-matrix-v1.md`
