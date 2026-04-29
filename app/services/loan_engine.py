"""
Loan Engine — SafeFund Ledger

Motor de cálculo retrospectivo de intereses sobre saldo insoluto.
Soporta múltiples préstamos activos por socio y abonos con fechas personalizadas.

Fórmula base:
  interés_período = saldo_insoluto × tasa_mensual × (días_período / 30)
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy.orm import Session

CENT = Decimal("0.01")
DAYS_PER_MONTH = Decimal("30")


# ── Tipos de retorno ────────────────────────────────────────────────────────────

@dataclass
class PeriodoInteres:
    fecha_inicio: date
    fecha_fin: date
    saldo_insoluto: Decimal
    dias: int
    interes: Decimal


@dataclass
class InteresAcumuladoResult:
    loan_id: int
    fecha_corte: date
    interes_acumulado: Decimal
    saldo_insoluto_actual: Decimal
    periodos: list[PeriodoInteres] = field(default_factory=list)


@dataclass
class LoanSummary:
    loan_id: int
    member_id: int
    member_name: str
    initial_amount: Decimal
    outstanding_balance: Decimal
    interest_rate: Decimal
    loan_type: str
    status: str
    start_date: date
    interes_acumulado: Decimal
    interes_mes_actual: Decimal
    dias_sin_pago: int
    ultimo_pago_interes: Optional[date]
    ultimo_abono_capital: Optional[date]
    en_mora: bool


# ── Cálculo retrospectivo de interés ───────────────────────────────────────────

def calculate_accrued_interest(
    loan_id: int,
    fecha_corte: date,
    db: Session,
) -> InteresAcumuladoResult:
    """
    Calcula el interés acumulado de un préstamo desde su fecha de inicio
    hasta fecha_corte, considerando todos los abonos a capital registrados.

    El interés se calcula por períodos entre abonos:
      interés = saldo_insoluto × tasa_mensual × (días / 30)
    """
    from app.models.base import Loan, Transaction

    loan = db.get(Loan, loan_id)
    if not loan:
        raise ValueError(f"Préstamo {loan_id} no encontrado.")

    if fecha_corte < loan.start_date:
        return InteresAcumuladoResult(
            loan_id=loan_id,
            fecha_corte=fecha_corte,
            interes_acumulado=Decimal("0.00"),
            saldo_insoluto_actual=Decimal(str(loan.outstanding_balance)),
        )

    # Obtener todos los abonos a capital hasta fecha_corte, ordenados por fecha
    capital_txns = (
        db.query(Transaction)
        .filter(
            Transaction.loan_id == loan_id,
            Transaction.transaction_type == "capital_payment",
            Transaction.transaction_date <= fecha_corte,
        )
        .order_by(Transaction.transaction_date, Transaction.id)
        .all()
    )

    # Construir períodos: (fecha_inicio, saldo_en_ese_período)
    balance = Decimal(str(loan.initial_amount))
    checkpoints: list[tuple[date, Decimal]] = [(loan.start_date, balance)]

    for txn in capital_txns:
        balance = (balance - Decimal(str(txn.amount))).quantize(CENT, rounding=ROUND_HALF_UP)
        if balance < Decimal("0"):
            balance = Decimal("0")
        checkpoints.append((txn.transaction_date, balance))

    # Añadir fecha de corte como cierre del último período
    checkpoints.append((fecha_corte, balance))

    # Calcular interés acumulado período a período
    total_interest = Decimal("0.00")
    rate = Decimal(str(loan.interest_rate))
    periodos: list[PeriodoInteres] = []

    for i in range(len(checkpoints) - 1):
        p_start, p_balance = checkpoints[i]
        p_end = checkpoints[i + 1][0]

        if p_end <= p_start:
            continue

        dias = (p_end - p_start).days
        interes = (
            p_balance * rate * Decimal(str(dias)) / DAYS_PER_MONTH
        ).quantize(CENT, rounding=ROUND_HALF_UP)

        total_interest += interes
        periodos.append(PeriodoInteres(
            fecha_inicio=p_start,
            fecha_fin=p_end,
            saldo_insoluto=p_balance,
            dias=dias,
            interes=interes,
        ))

    return InteresAcumuladoResult(
        loan_id=loan_id,
        fecha_corte=fecha_corte,
        interes_acumulado=total_interest.quantize(CENT, rounding=ROUND_HALF_UP),
        saldo_insoluto_actual=balance,
        periodos=periodos,
    )


# ── Métricas de morosidad ───────────────────────────────────────────────────────

def get_loans_with_mora(caja_id: int, db: Session, dias_gracia: int = 35) -> list[LoanSummary]:
    """
    Retorna préstamos activos con indicador de mora.
    Un préstamo está en mora si el último pago de interés fue hace más de
    `dias_gracia` días (default: 35 = un mes + 5 días de gracia).
    """
    from app.models.base import Loan, Member, Transaction
    from sqlalchemy import func

    today = date.today()
    loans = (
        db.query(Loan)
        .join(Member, Loan.member_id == Member.id)
        .filter(Loan.caja_id == caja_id, Loan.status == "active")
        .all()
    )

    result: list[LoanSummary] = []
    for loan in loans:
        member = db.get(Member, loan.member_id)

        last_interest = (
            db.query(Transaction)
            .filter(
                Transaction.loan_id == loan.id,
                Transaction.transaction_type == "interest_payment",
            )
            .order_by(Transaction.transaction_date.desc())
            .first()
        )

        last_capital = (
            db.query(Transaction)
            .filter(
                Transaction.loan_id == loan.id,
                Transaction.transaction_type == "capital_payment",
            )
            .order_by(Transaction.transaction_date.desc())
            .first()
        )

        ultimo_pago_fecha = last_interest.transaction_date if last_interest else None
        dias_sin_pago = (today - ultimo_pago_fecha).days if ultimo_pago_fecha else (today - loan.start_date).days
        en_mora = dias_sin_pago > dias_gracia

        # Interés del mes actual (estático)
        balance = Decimal(str(loan.outstanding_balance))
        rate = Decimal(str(loan.interest_rate))
        interes_mes = (balance * rate).quantize(CENT, rounding=ROUND_HALF_UP)

        result.append(LoanSummary(
            loan_id=loan.id,
            member_id=loan.member_id,
            member_name=member.name if member else "",
            initial_amount=Decimal(str(loan.initial_amount)),
            outstanding_balance=balance,
            interest_rate=rate,
            loan_type=loan.loan_type,
            status=loan.status,
            start_date=loan.start_date,
            interes_acumulado=Decimal("0.00"),  # calculado bajo demanda
            interes_mes_actual=interes_mes,
            dias_sin_pago=dias_sin_pago,
            ultimo_pago_interes=ultimo_pago_fecha,
            ultimo_abono_capital=last_capital.transaction_date if last_capital else None,
            en_mora=en_mora,
        ))

    result.sort(key=lambda x: (-x.dias_sin_pago, x.member_name))
    return result


# ── Métricas de capital para cierre de caja ────────────────────────────────────

def get_capital_metrics(caja_id: int, fecha_corte: date, db: Session) -> dict:
    """
    Calcula las métricas de capital para la fecha de corte dada.
    Retorna un dict con totales de aportaciones, préstamos e intereses.
    """
    from app.models.base import Aportacion, Loan, Transaction
    from sqlalchemy import func

    # Capital acumulado de aportaciones hasta fecha_corte
    capital_aportado = db.query(func.sum(Aportacion.monto)).filter(
        Aportacion.caja_id == caja_id,
        Aportacion.fecha_aportacion <= fecha_corte,
    ).scalar() or Decimal("0")

    # Capital actualmente en préstamos
    capital_prestado = db.query(func.sum(Loan.outstanding_balance)).filter(
        Loan.caja_id == caja_id,
        Loan.status == "active",
    ).scalar() or Decimal("0")

    # Intereses cobrados hasta fecha_corte
    intereses_cobrados = db.query(func.sum(Transaction.amount)).join(
        Loan, Transaction.loan_id == Loan.id
    ).filter(
        Loan.caja_id == caja_id,
        Transaction.transaction_type == "interest_payment",
        Transaction.transaction_date <= fecha_corte,
    ).scalar() or Decimal("0")

    # Abonos a capital recibidos hasta fecha_corte
    capital_recuperado = db.query(func.sum(Transaction.amount)).join(
        Loan, Transaction.loan_id == Loan.id
    ).filter(
        Loan.caja_id == caja_id,
        Transaction.transaction_type == "capital_payment",
        Transaction.transaction_date <= fecha_corte,
    ).scalar() or Decimal("0")

    capital_aportado = Decimal(str(capital_aportado))
    capital_prestado = Decimal(str(capital_prestado))
    intereses_cobrados = Decimal(str(intereses_cobrados))
    capital_recuperado = Decimal(str(capital_recuperado))

    capital_disponible = (capital_aportado + intereses_cobrados + capital_recuperado - capital_prestado).quantize(CENT)

    rendimiento_pct = (
        (intereses_cobrados / capital_aportado * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if capital_aportado > 0 else Decimal("0.00")
    )

    return {
        "fecha_corte": str(fecha_corte),
        "capital_aportado": float(capital_aportado),
        "capital_prestado": float(capital_prestado),
        "intereses_cobrados": float(intereses_cobrados),
        "capital_recuperado": float(capital_recuperado),
        "capital_disponible": float(capital_disponible),
        "rendimiento_pct": float(rendimiento_pct),
    }
