# Plan de acción para análisis y cierre de caja

## 1. Corregir y medir antes de ampliar

- **Saldos.** `members_view` recalcula y guarda ahorros al abrir la vista. Suma aportaciones y movimientos de ahorro, mientras que el recálculo de quincenas usa solo aportaciones. Conciliar ambas fuentes con los saldos iniciales importados, evitar duplicidades y centralizar el saldo por caja y socio. Aplicar las actualizaciones al registrar o corregir movimientos y mantener las pantallas de consulta en solo lectura.
- **Rendimiento.** `members_view` ejecuta cuatro agregaciones por socio y `generate_reporte_caja` consulta el último interés por préstamo. Agrupar por socio y préstamo en SQL, reutilizar las agregaciones entre pantalla y reporte y agregar índices solo si el plan de consulta los justifica. Medir cantidad de consultas, latencia p95 y tiempo de exportación antes y después con una caja representativa.
- **Compatibilidad.** Mantener los flujos de socios, quincenas, préstamos internos y externos, pagos y PDFs actuales. Comparar resultados con datos existentes e incorporar migraciones de Alembic para el cierre y sus movimientos. Usar `Decimal` y conciliar a centavos.

## 2. Agregar «Análisis de caja» al menú principal

- Mostrar la caja y fecha de corte, tarjetas de ahorro, deuda, liquidez e intereses cobrados, y una tabla de socios internos con cuota personal quincenal mayor a $0. Ordenar por nombre y filtrar por «Todos», «Pidió préstamos» y «Con préstamo activo». Permitir ver los préstamos individuales desde la fila del socio. Usar etiquetas con texto e icono para que los estados se distingan también sin color.
- Mostrar «El ahorro cubre la deuda» o «Faltan $X» según la deuda liquidable conjunta del socio al corte, incluido el interés pendiente cuando corresponda. Mostrar por separado la instrucción de pago «Liquidar con ahorro», «Pago por separado» o «Por definir». Aplicar el ahorro una sola vez al conjunto de préstamos y previsualizar el faltante `máx(deuda − ahorro, 0)` y el ahorro a devolver `máx(ahorro − deuda, 0)` si se autoriza la compensación.
- Incluir un simulador «¿Puedo prestar $X?» antes del cierre. Para este flujo, exigir socio interno con cuota positiva. Usar su ahorro acumulado como tope y restar la deuda vigente respaldada por ese ahorro. Limitar el desembolso al efectivo disponible de la caja e indicar el máximo y el motivo si la solicitud lo supera. Si se confirma desde el módulo, volver a validar el límite dentro de la operación de préstamo para evitar usar el mismo respaldo o efectivo dos veces.

## 3. Liquidar y repartir rendimientos

- Previsualizar el cierre, aplicar las compensaciones autorizadas como movimientos auditables de ahorro y deuda, y exigir que todos los préstamos de la caja, internos y externos, queden liquidados antes de confirmar el reparto. Revalidar saldos al confirmar y guardar el corte, los movimientos y el reparto en una operación atómica que pueda reintentarse sin duplicar cargos o pagos.
- Repartir los rendimientos efectivamente cobrados entre los socios internos con cuota positiva, usando el ahorro aportado antes de las compensaciones. Proponer una ponderación editable por grupo con un factor menor para quienes nunca pidieron préstamo. Calcular la participación de cada socio como `(ahorro aportado × factor del grupo) ÷ suma de los ahorros ponderados`. Permitir ajustar porcentajes individuales antes de exportar o confirmar. Si hay rendimiento por repartir, exigir un total del 100 % y validar que el rendimiento por peso ahorrado sea menor para el grupo sin préstamos cuando ambos grupos tengan ahorro. Distribuir el ajuste de redondeo de forma determinista para que los montos sumen el total a repartir.

## 4. Exportar y verificar

- Reutilizar Jinja y WeasyPrint para un PDF con resumen del ciclo al corte, aportaciones, préstamos otorgados, cobros y rendimientos. Incluir a todos los socios elegibles en orden alfabético, incluso si hay filtros activos en pantalla. Mostrar ahorro acumulado, si pidió préstamos, monto desembolsado, estado de cada préstamo, deuda al corte, saldo tras aplicar ahorro y porcentaje e importe del rendimiento asignado. Marcar como preliminar el reparto previo al cierre y emitir el definitivo desde el corte guardado.
- Validar con cajas que tengan saldos iniciales importados, varios préstamos por socio, cobertura exacta o insuficiente, liquidez baja, préstamos externos pendientes y rendimiento cero. Comprobar que pantalla, PDF y cierre coincidan para el mismo corte y que una corrección posterior no altere un cierre confirmado.
