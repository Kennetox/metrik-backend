# Comercio Web v1 (Metrik)

Fecha: 2026-03-27

## Objetivo

Definir la arquitectura correcta para integrar el comercio web de Kensar dentro de Metrik sin romper:

- la numeracion documental actual
- los tickets de tienda fisica
- los dashboards operativos del POS
- los cierres de caja
- la trazabilidad comercial

La decision base es que el modulo se llamara **Comercio Web**.


## Decision principal

Metrik sera el backend central de:

- inventario
- catalogo web
- clientes web
- carritos
- ordenes web
- pagos web
- conversion a venta final

No se creara un backend separado para la web.


## Problema que debemos evitar

Una orden web no debe comportarse como una venta POS final desde el inicio.

Si cada checkout crea directamente una `Sale` normal:

- contaminaríamos la numeracion de ventas
- mezclaríamos ventas pendientes con ventas reales
- dañariamos reportes del dashboard
- afectariamos cierres de caja y metricas del POS
- perderiamos claridad entre intencion de compra y venta consolidada


## Distincion obligatoria de entidades

### 1. Carrito web

Representa seleccion temporal del cliente.

No es documento comercial.

### 2. Orden web

Representa una compra iniciada o confirmada dentro del canal web.

Puede estar:

- pendiente
- esperando pago
- pagada
- en preparacion
- lista
- entregada
- cancelada
- fallida
- reembolsada

Es un documento operativo del canal web, no una venta POS final.

### 3. Venta

Representa el documento comercial consolidado dentro del sistema actual.

Es la entidad que debe seguir usando:

- `sale_number`
- `document_number`
- serie `V-000001`

### 4. Pago web

Representa intentos y confirmaciones de pago asociados a una orden web.

No debe confundirse con `SalePayment` hasta que la orden se convierta en venta real o se consolide en el flujo contable definido.


## Decision documental

### Serie documental existente

Hoy las ventas usan:

- `sale_number`
- `document_number`
- formato `V-000001`

Eso debe mantenerse para las ventas reales del sistema.

### Serie documental nueva para Comercio Web

Las ordenes web deben tener su propia serie.

Propuesta:

- `OW-000001`

Donde:

- `OW` = Orden Web

Alternativas posibles:

- `CW-000001`
- `WEB-000001`

Recomendacion final:

- usar `OW-000001`

Es corta, clara y no compite con `V-`.


## Regla de convivencia documental

### Regla base

- `WebOrder.document_number` usa serie `OW-xxxxxx`
- `Sale.document_number` sigue usando serie `V-xxxxxx`

### Consecuencia

Una orden web puede existir sin haber generado aun una venta.

Cuando la orden deba consolidarse como venta real:

- se crea una `Sale`
- se genera su numero `V-xxxxxx`
- la orden queda vinculada a esa venta


## Modelo recomendado

### A. WebOrder

Nueva entidad principal del modulo `Comercio Web`.

Campos minimos:

- `id`
- `tenant_id`
- `order_number`
- `document_number`
- `status`
- `customer_account_id`
- `pos_customer_id`
- `customer_name`
- `customer_email`
- `customer_phone`
- `customer_tax_id`
- `customer_address`
- `subtotal`
- `discount_amount`
- `shipping_amount`
- `total`
- `currency`
- `notes`
- `created_at`
- `updated_at`
- `submitted_at`
- `paid_at`
- `cancelled_at`
- `converted_to_sale_at`
- `sale_id` nullable
- `sale_document_number` nullable
- `payment_status`
- `fulfillment_status`

### B. WebOrderItem

- `id`
- `tenant_id`
- `web_order_id`
- `product_id`
- `product_name_snapshot`
- `product_sku_snapshot`
- `product_barcode_snapshot`
- `unit_price_snapshot`
- `quantity`
- `line_discount_value`
- `line_total`

### C. WebOrderPayment

- `id`
- `tenant_id`
- `web_order_id`
- `provider`
- `provider_reference`
- `method`
- `status`
- `amount`
- `currency`
- `raw_payload` json
- `approved_at`
- `failed_at`
- `cancelled_at`
- `created_at`

### D. WebOrderStatusLog

Para trazabilidad operacional:

- `id`
- `tenant_id`
- `web_order_id`
- `from_status`
- `to_status`
- `note`
- `actor_type`
- `actor_user_id`
- `created_at`


## Estados recomendados de WebOrder

Estados iniciales:

- `draft`
- `pending_payment`
- `paid`
- `processing`
- `ready`
- `fulfilled`
- `cancelled`
- `payment_failed`
- `refunded`

### Significado

- `draft`: checkout iniciado pero no enviado
- `pending_payment`: orden creada esperando confirmacion
- `paid`: pago confirmado
- `processing`: operacion la esta preparando
- `ready`: lista para entrega o retiro
- `fulfilled`: entregada/completada
- `cancelled`: cancelada antes de cierre
- `payment_failed`: el intento de pago fallo
- `refunded`: pagada y luego reversada


## Momento correcto para crear la Sale

Esta es la decision mas importante.

### Recomendacion

No crear `Sale` en el momento de armar carrito.
No crear `Sale` en el momento de iniciar checkout.

La `Sale` debe crearse solo cuando la orden web alcance un estado comercialmente valido.

### Regla inicial propuesta

Crear `Sale` cuando:

- la orden este `paid`
- y el sistema o la operacion decidan consolidarla

### Politica v1 sugerida

`WebOrder.paid` no crea automaticamente `Sale`.

Primero:

- pago confirmado
- validacion operativa
- verificacion de stock real

Luego:

- accion manual o automatica de “Convertir a venta”

Eso da mas control durante la fase temprana.

### Politica v2 futura

Cuando el flujo este estable:

- `paid` puede disparar conversion automatica a `Sale`


## Relacion entre WebOrder y Sale

Debe ser:

- una orden web puede no tener venta
- una orden web puede convertirse en una sola venta
- una venta creada desde web debe saber que viene de `Comercio Web`

Campos recomendados:

En `WebOrder`:

- `sale_id`
- `sale_document_number`

En `Sale`:

- `origin_channel`
- `origin_reference_type`
- `origin_reference_id`

Valores propuestos:

- `origin_channel = "web"`
- `origin_reference_type = "web_order"`
- `origin_reference_id = <web_order_id>`


## Integracion con la numeracion actual

Metrik ya tiene reservas para ventas via:

- `sale_number_reservations`

No debemos reutilizar esa tabla para ordenes web.

### Decision

Crear numeracion independiente para `WebOrder`.

Propuesta:

- `web_order_number`
- `web_order_reservations` solo si de verdad se necesita

Para v1 ni siquiera hace falta reserva anticipada.

Basta:

- crear la orden
- asignar secuencia propia
- generar `OW-000001`


## Impacto operacional en dashboard y POS

### Las ordenes web no deben entrar como ventas POS

Hasta que no se conviertan a `Sale`, no deben sumar en:

- ventas del dia del POS
- tickets del dashboard principal
- cierres
- indicadores de ticket promedio del POS

### Necesitamos vistas separadas

En Metrik frontend debe existir un nuevo modulo:

## Modulo: Comercio Web

Secciones recomendadas:

- `Resumen`
- `Catalogo Web`
- `Clientes Web`
- `Carritos`
- `Ordenes Web`
- `Pagos Web`
- `Conversiones a Venta`
- `Configuracion`


## Resumen de cada seccion

### Resumen

Metricas del canal web:

- ordenes hoy
- ordenes pendientes
- ordenes pagadas
- conversiones a venta
- carritos activos
- tasa de abandono

### Catalogo Web

Gestion de:

- publicar producto
- destacar producto
- descripcion web
- orden visual
- precio visible o consultar

### Clientes Web

Gestion de:

- cuentas web
- cliente comercial vinculado
- actividad reciente
- ordenes y carrito

### Carritos

Vista operacional:

- carritos activos
- ultimos cambios
- carritos abandonados

### Ordenes Web

Vista central del modulo.

Debe permitir:

- listar ordenes
- ver estado
- revisar pago
- preparar pedido
- marcar listo
- cancelar
- convertir a venta

### Pagos Web

Eventos de:

- aprobados
- pendientes
- rechazados
- reembolsados

### Conversiones a Venta

Trazabilidad entre:

- `OW-xxxxxx`
- `V-xxxxxx`

Esto es clave para auditoria y soporte operativo.


## Direccion de frontend

### En kensar_web

Debemos implementar:

- registro/login cliente
- carrito
- checkout
- estado de orden
- historial de ordenes

### En frontend de Metrik

Debemos implementar el modulo `Comercio Web` para operacion interna real.

Sin esto, el backend existiria pero no habria una interfaz operativa tangible para el equipo.


## Fases recomendadas

### Fase 1. Base documental correcta

Implementar:

- `WebOrder`
- `WebOrderItem`
- `WebOrderPayment`
- `WebOrderStatusLog`
- numeracion `OW-xxxxxx`

### Fase 2. Operacion web inicial

Implementar:

- checkout
- creacion de orden web
- historial de ordenes por cliente
- vista de ordenes en modulo `Comercio Web`

### Fase 3. Conversion a venta

Implementar:

- accion manual “Convertir a venta”
- crear `Sale` desde `WebOrder`
- ligar `WebOrder` con `Sale`
- mostrar trazabilidad cruzada

### Fase 4. Pagos reales

Implementar:

- integracion pasarela
- confirmacion asincrona
- estados de pago
- reintentos

### Fase 5. Operacion avanzada

Implementar:

- fulfillment
- retiro en tienda
- despacho
- devoluciones web
- reembolsos
- automatizaciones


## Decision final v1

La direccion oficial para Kensar es:

- el modulo se llamara **Comercio Web**
- las compras online primero seran `WebOrder`
- las ventas reales seguiran siendo `Sale`
- `WebOrder` y `Sale` tendran series documentales separadas
- la conversion de orden web a venta sera una accion explicita del sistema

Esto preserva la coherencia documental y prepara a Metrik para operar tienda fisica y comercio web desde un solo nucleo.
