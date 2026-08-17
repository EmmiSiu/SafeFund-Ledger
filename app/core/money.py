"""
money.py — Helpers de dinero compartidos por toda la app.

Antes cada módulo redeclaraba su propio `CENT = Decimal("0.01")` y repetía
`Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)` a mano. Este módulo
centraliza esa constante y el patrón de redondeo/conversión para que todos
los cálculos monetarios usen exactamente la misma precisión.
"""
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def to_decimal(value) -> Decimal:
    """Convierte un valor (float, str, Decimal, columna Numeric de SQLAlchemy) a Decimal."""
    return Decimal(str(value))


def round_money(value) -> Decimal:
    """Convierte a Decimal y redondea a centavos (ROUND_HALF_UP)."""
    return to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)
