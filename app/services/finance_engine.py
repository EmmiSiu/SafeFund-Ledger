"""
Finance Engine — SafeFund Ledger

Regla de Oro de Amortización:
  1. El pago cubre primero el interés devengado sobre el saldo insoluto.
  2. El excedente reduce capital.
  3. El interés del siguiente período se calcula únicamente sobre el nuevo saldo.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def calculate_monthly_interest(
    outstanding_balance: Decimal, interest_rate: Decimal
) -> Decimal:
    """Interés mensual = saldo insoluto × tasa mensual, redondeado a centavos."""
    return (outstanding_balance * interest_rate).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class PaymentBreakdown:
    interest_paid: Decimal
    capital_paid: Decimal
    remaining_balance: Decimal
    overpayment: Decimal
    loan_fully_paid: bool


def apply_payment(
    outstanding_balance: Decimal,
    interest_rate: Decimal,
    payment_amount: Decimal,
) -> PaymentBreakdown:
    """
    Desglosa un pago recibido según la Regla de Oro.
    Retorna cuánto fue a interés, cuánto a capital y el nuevo saldo insoluto.
    """
    if payment_amount <= Decimal("0"):
        raise ValueError("El monto del pago debe ser mayor a cero.")

    interest_due = calculate_monthly_interest(outstanding_balance, interest_rate)

    # Paso 1: cubrir interés
    interest_paid = min(payment_amount, interest_due)
    remainder = payment_amount - interest_paid

    # Paso 2: remainder reduce capital
    capital_paid = min(remainder, outstanding_balance)
    overpayment = (remainder - capital_paid).quantize(CENT, rounding=ROUND_HALF_UP)

    new_balance = (outstanding_balance - capital_paid).quantize(
        CENT, rounding=ROUND_HALF_UP
    )

    return PaymentBreakdown(
        interest_paid=interest_paid.quantize(CENT, rounding=ROUND_HALF_UP),
        capital_paid=capital_paid.quantize(CENT, rounding=ROUND_HALF_UP),
        remaining_balance=new_balance,
        overpayment=overpayment,
        loan_fully_paid=new_balance == Decimal("0.00"),
    )


def project_interest(
    outstanding_balance: Decimal, interest_rate: Decimal, months: int
) -> Decimal:
    """
    Proyecta el total de intereses que acumularía un préstamo en N meses
    asumiendo que no se hacen abonos a capital.
    Útil para la métrica de 'intereses por cobrar' del BI.
    """
    total = Decimal("0.00")
    balance = outstanding_balance
    for _ in range(months):
        interest = calculate_monthly_interest(balance, interest_rate)
        total += interest
    return total.quantize(CENT, rounding=ROUND_HALF_UP)
