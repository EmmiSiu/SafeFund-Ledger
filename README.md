# SafeFund Ledger - Especificaciones Financieras

## Lógica de Negocio
1. **Caja de Ahorro:** 24 quincenas. Cada socio tiene una cuota fija.
2. **Préstamos:**
   - Internos: 5% mensual. (se puede cambiar)
   - Externos: 8% mensual. (se puede cambiar)
3. **Regla de Oro de Amortización:** Cada pago recibido se desglosa:
   - Primero cubre el interés devengado a la fecha.
   - El excedente se aplica a reducción de Capital.
   - El interés del siguiente mes se calcula solo sobre el nuevo saldo de capital.

## Entidades Principales
- **Member:** Nombre, grupo (Elenita, Natación, etc.), balance total.
- **Loan:** Socio_id, monto_inicial, tasa (5/8), estatus, fecha_inicio.
- **Transaction:** Loan_id, monto, fecha, tipo (Interés/Capital/Ahorro).

## Métricas BI Requeridas
- Yield (Rendimiento) total de la caja.
- Proyección de cierre (Capital actual + Intereses por cobrar).
- Top 5 deudores vs Top 5 ahorradores.
- Las necesarias para tomar decisiones en base al contexto e ingesta de datos del sistema.

## ESTRUCTURA DE CARPETAS 
safefund-ledger-pro/
├── app/
│   ├── api/             # Endpoints (ahorros, préstamos, reportes)
│   ├── core/            # Configuración, DB session, seguridad
│   ├── models/          # Modelos SQLAlchemy (Tablas: Socio, Ahorro, Préstamo, Movimiento)
│   ├── schemas/         # Pydantic (Validación de datos)
│   ├── services/        # Lógica de negocio (Cálculo de intereses, BI)
│   ├── static/          # CSS (Tailwind) y JS (Alpine logic modularizada)
│   ├── templates/       # Jinja2 (Views y PDF templates)
│   └── main.py          # Punto de entrada
├── data/                # Carpeta para el archivo sqlite (persistida en Docker)
├── docker-compose.yml
└── README.md
## 🛠 Guía de Instalación y Despliegue Local

Sigue estos pasos para poner en marcha **SafeFund Ledger** en tu entorno local utilizando Docker.

### 1. Requisitos Previos
* **Docker** y **Docker Compose** instalados.
* **Git** configurado.

### 2. Configuración del Entorno
Antes de levantar el servicio, asegúrate de tener el archivo `.env` en la raíz con la configuración básica:
```bash
cp .env.example .env  # O crea el archivo .env manualmente
# Crear el entorno virtual
python -m venv venv

# Activar el entorno (Windows)
.\venv\Scripts\activate

# Instalar dependencias para IntelliSense y Claude Code
pip install -r requirements.txt

### Construcción y Despliegue con Docker

# Construir e iniciar contenedores
docker-compose up --build -d

4. Ejecución del Servidor (Hot-Reload)
Para ver los cambios en tiempo real mientras programas, elige uno de estos métodos:

Opción A: Ejecución Nativa (Recomendada para Desarrollo)
Ideal para que Claude Code y tu IDE detecten errores de inmediato. Ejecuta esto con tu venv activado:

PowerShell
# Desde la raíz del proyecto
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
Nota: En este modo, asegúrate de que el archivo .env tenga DATABASE_URL=sqlite:///./data/safefund.db.

Opción B: Ejecución vía Docker (Entorno Aislado)
Si prefieres que todo corra dentro del contenedor, el Dockerfile que configuramos ya incluye el flag --reload.

Si necesitas reiniciarlo o forzar el reload después de que el contenedor ya está arriba, puedes usar:

PowerShell
# Reiniciar el servicio específico sin bajar todo el stack
docker-compose restart app

# O ver los logs para confirmar que el reload se disparó al guardar
docker-compose logs -f app

## Análisis y cierre de caja

- **Pantalla:** menú «Análisis de caja» (`/analisis`). Tres pestañas: socios (cobertura ahorro vs. deuda e instrucción de pago), simulador «¿Puedo prestar $X?» y cierre con reparto de rendimientos.
- **Un solo cálculo:** `app/services/cierre.py::build_resumen` alimenta la pantalla, el PDF (`/api/v1/reports/cierre/{caja_id}`) y la confirmación, así que los tres coinciden para el mismo corte.
- **Saldo de ahorro:** la fuente única es `app/services/savings.py` (aportaciones + saldos iniciales importados). Las pantallas de consulta no escriben; el saldo guardado se actualiza al registrar o corregir movimientos.
- **Cierre:** atómico e idempotente (`POST /api/v1/cierre/confirmar`). Exige todos los préstamos liquidados; una vez confirmado queda congelado y el PDF pasa de «preliminar» a «definitivo».
- **Migración:** `python -m alembic upgrade head` (solo agrega tablas `cierre_*`; la app también las crea al arrancar).
- **Pruebas:** `python -m pytest tests` (usan una base temporal, nunca `data/safefund.db`).
- **Ensayo sin riesgo:** copia `data/safefund.db` a `data/sandbox.db` y levanta la configuración `safefund-sandbox` de `.claude/launch.json` (puerto 8011) para practicar el cierre sobre la copia.
- **PDF en Windows sin GTK:** si WeasyPrint no puede generar el PDF, los reportes se entregan como HTML imprimible (`?formato=html` lo fuerza).
