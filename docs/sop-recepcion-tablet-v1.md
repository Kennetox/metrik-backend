# SOP Operativo v1 - Recepcion Unica en Tablet (Kensar)

## 1) Objetivo
Estandarizar la recepcion de mercancia en un solo flujo rapido, sin estados provisionales, para lograr:
- disponibilidad inmediata para venta,
- trazabilidad de ingreso por lote,
- etiquetado consistente,
- control de inventario util para alertas y reposicion.

## 2) Alcance
Aplica a toda mercancia que entra a Kensar (productos existentes y productos nuevos), recibida en mostrador.

## 3) Roles por turno
- Responsable de recepcion (1 persona): captura lote en tablet y confirma cierre.
- Responsable de etiquetado (1 persona, o la misma si esta sola): imprime y pega etiquetas.
- Comprador (dueno): entrega mercancia con contexto de compra (factura o compra cash).

Nota: Siempre debe haber una responsable del cierre del lote en el turno.

## 4) Herramientas
- Tablet con app de recepcion.
- Impresora SATO FX3-LX conectada por red local.
- Sistema Metrik (catalogo + inventario + etiquetas).

## 5) Principios operativos (no negociables)
- Ningun producto entra a venta sin pasar por un lote de recepcion.
- El lote siempre se cierra el mismo dia.
- Producto nuevo exige alta minima, no catalogo perfecto.
- Si la impresion falla, se deja trazabilidad de etiqueta pendiente dentro del lote.

## 6) Flujo unico de recepcion (paso a paso)

### Paso 0 - Preparacion (3-5 min)
- Verificar que tablet tenga sesion activa.
- Verificar conectividad con impresora SATO.
- Confirmar que haya etiquetas fisicas disponibles.

### Paso 1 - Abrir lote (1 min)
Crear lote con datos minimos:
- tipo de compra: `factura` o `cash`,
- origen/proveedor,
- responsable de recepcion,
- observacion corta opcional (ej: "compra centro", "pedido audio").

### Paso 2 - Capturar items (modo rapido)
Para cada producto recibido:
- si ya existe: buscar por codigo/SKU/nombre y registrar cantidad,
- si es nuevo: crear alta minima y registrar cantidad.

Alta minima obligatoria para nuevo:
- nombre comercial,
- familia/grupo,
- costo,
- precio,
- SKU o codigo de barras (si no existe, codigo interno temporal valido).

Reglas de velocidad:
- usar botones de cantidad rapida: `+1`, `+5`, `+10`, `caja xN`,
- capturar primero cantidad total del mismo item y luego pasar al siguiente,
- evitar editar productos antiguos durante recepcion (eso va fuera del lote).

### Paso 3 - Etiquetado en paralelo
- Imprimir etiquetas desde cola del lote (no una por una manualmente).
- Pegar etiqueta al producto apenas salga.
- Si hay error de impresion, marcar item como `pendiente_etiqueta` dentro del lote.

### Paso 4 - Verificacion rapida de lote (5-10 min)
Antes de cerrar:
- revisar items sin cantidad,
- revisar duplicados evidentes,
- revisar items nuevos sin costo/precio,
- revisar pendientes de etiqueta.

### Paso 5 - Cerrar lote (1 clic)
Al cerrar lote:
- se registran entradas de inventario,
- quedan trazados responsable, fecha y origen,
- productos quedan disponibles para venta.

Regla: No dejar lote abierto al cierre del turno.

## 7) Modo operativo segun personal disponible

### Escenario A: dos personas
- Persona 1: captura en tablet.
- Persona 2: imprime y pega etiquetas.
- Ventaja: maxima velocidad y menor cuello de botella.

### Escenario B: una sola persona
Trabajar por olas:
1. capturar 10-20 items,
2. imprimir lote corto,
3. pegar etiquetas,
4. repetir.

## 8) Tiempos objetivo
- Apertura de lote: <= 1 minuto.
- Captura item existente: 20-45 segundos.
- Captura item nuevo (alta minima): 90-180 segundos.
- Cierre de lote: <= 2 minutos.
- Tiempo total lote grande (meta negocio): 2 a 3 horas.

## 9) Reglas para compra cash (sin factura)
Cuando no haya factura, el lote debe llevar soporte interno minimo:
- tipo: `cash`,
- origen (tienda/persona),
- quien compro,
- evidencia simple (nota, foto, chat o referencia escrita),
- costo unitario y cantidad.

Sin ese minimo, el lote no se debe cerrar.

## 10) Manejo de excepciones

### Excepcion 1 - Impresora fuera de linea
- Continuar captura de lote.
- Marcar etiquetas pendientes.
- Reintentar impresion al finalizar captura.
- No cerrar turno sin resolver pendientes criticos.

### Excepcion 2 - Producto nuevo sin codigo barra
- Crear codigo interno temporal valido.
- Imprimir etiqueta con ese codigo.
- Estandarizar barcode definitivo en mantenimiento de catalogo.

### Excepcion 3 - Llego mercancia en hora pico de venta
- Priorizar captura de items de alta rotacion.
- Completar resto del lote en la misma jornada.
- Mantener una sola recepcion y un solo cierre.

### Excepcion 4 - Diferencias fisicas (llego menos/mas)
- Registrar cantidad real recibida.
- Dejar nota corta en lote.
- No inventar cantidades para "cuadrar".

## 11) Checklist de cierre de turno (recepcion)
- [ ] No hay lotes abiertos.
- [ ] No hay items sin cantidad.
- [ ] No hay productos nuevos sin costo/precio.
- [ ] Etiquetas pendientes <= umbral acordado.
- [ ] Incidencias documentadas en observaciones.

## 12) KPIs semanales (control operativo)
- % lotes cerrados el mismo dia.
- tiempo promedio de recepcion por lote.
- items por hora (existentes vs nuevos).
- % items con etiqueta pendiente al cierre.
- quiebres en top SKUs (audio profesional primero).

## 13) Plan de implementacion (2 semanas)

### Semana 1 - Adopcion del proceso
- Dia 1: capacitacion corta (30-45 min) con simulacion real.
- Dia 2-3: ejecucion acompanada en turnos.
- Dia 4-6: medir tiempos y cuellos de botella.
- Dia 7: ajustar reglas de cantidad rapida y checklist.

### Semana 2 - Estabilizacion
- Aplicar SOP completo en todos los ingresos.
- Revisar KPIs diarios de cierre.
- Afinar manejo de etiquetas pendientes y nuevos productos.

## 14) Criterio de exito v1
El SOP v1 se considera exitoso si por 10 dias seguidos se cumple:
- >= 90% lotes cerrados el mismo dia,
- tiempo por lote dentro de meta operativa,
- reduccion visible de faltantes en top productos,
- equipo operando sin friccion relevante.

---
Version: v1
Estado: Propuesto para piloto operativo
