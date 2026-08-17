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

from app.core.money import CENT

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
class TransaccionSospechosa:
    id: int
    fecha: date
    monto: Decimal
    notas: Optional[str]


@dataclass
class GrupoInteresDuplicado:
    loan_id: int
    member_id: int
    member_name: str
    loan_type: str
    transacciones: list[TransaccionSospechosa]
    dias_entre_pagos: list[int]
    total_monto: Decimal
    sugerido_conservar_id: int


@dataclass
class EventoTimeline:
    fecha: date
    tipo: str  # "apertura" | "interest_payment" | "capital_payment" | "loan_increment" | "hoy"
    monto: Decimal
    dias_desde_anterior: int
    interes_generado_periodo: Decimal
    saldo_antes: Decimal
    saldo_despues: Decimal
    notas: Optional[str]
    transaction_id: Optional[int]


@dataclass
class LoanTimelineResult:
    loan_id: int
    eventos: list[EventoTimeline]
    interes_generado_total: Decimal
    interes_pagado_total: Decimal
    interes_pendiente: Decimal
    saldo_actual: Decimal


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


# ── Detector de intereses duplicados ───────────────────────────────────────────

def find_duplicate_interest_payments(
    caja_id: int, db: Session, min_gap_days: int = 25
) -> list[GrupoInteresDuplicado]:
    """
    Detecta pagos de interés sospechosos de estar duplicados: dos o más
    `interest_payment` del mismo préstamo separados por menos de `min_gap_days`
    (un ciclo mensual normal es ~30 días). Transacciones consecutivas dentro de
    ese margen se agrupan en una sola cadena sospechosa.

    No elimina nada — solo reporta para que el usuario decida qué purgar
    (vía el endpoint existente DELETE /transactions/{id}).
    """
    from app.models.base import Loan, Member, Transaction

    loans = db.query(Loan).filter(Loan.caja_id == caja_id).all()
    grupos: list[GrupoInteresDuplicado] = []

    for loan in loans:
        txns = (
            db.query(Transaction)
            .filter(
                Transaction.loan_id == loan.id,
                Transaction.transaction_type == "interest_payment",
            )
            .order_by(Transaction.transaction_date, Transaction.id)
            .all()
        )
        if len(txns) < 2:
            continue

        chains: list[list[Transaction]] = []
        current_chain = [txns[0]]
        for prev_txn, curr_txn in zip(txns, txns[1:]):
            gap = (curr_txn.transaction_date - prev_txn.transaction_date).days
            if gap < min_gap_days:
                current_chain.append(curr_txn)
            else:
                if len(current_chain) > 1:
                    chains.append(current_chain)
                current_chain = [curr_txn]
        if len(current_chain) > 1:
            chains.append(current_chain)

        if not chains:
            continue

        member = db.get(Member, loan.member_id)
        for chain in chains:
            gaps = [
                (chain[i + 1].transaction_date - chain[i].transaction_date).days
                for i in range(len(chain) - 1)
            ]
            total = sum(Decimal(str(t.amount)) for t in chain).quantize(CENT, rounding=ROUND_HALF_UP)
            grupos.append(GrupoInteresDuplicado(
                loan_id=loan.id,
                member_id=loan.member_id,
                member_name=member.name if member else "",
                loan_type=loan.loan_type,
                transacciones=[
                    TransaccionSospechosa(
                        id=t.id, fecha=t.transaction_date,
                        monto=Decimal(str(t.amount)), notas=t.notes,
                    )
                    for t in chain
                ],
                dias_entre_pagos=gaps,
                total_monto=total,
                sugerido_conservar_id=chain[0].id,
            ))

    grupos.sort(key=lambda g: g.total_monto, reverse=True)
    return grupos


# ── Timeline de evolución del préstamo ─────────────────────────────────────────

def build_loan_timeline(
    loan_id: int, db: Session, fecha_corte: Optional[date] = None
) -> LoanTimelineResult:
    """
    Reconstruye la evolución del préstamo evento por evento desde su apertura:
    monto inicial, cada movimiento (interés, abono a capital, incremento) con el
    saldo antes/después y el interés que se generó desde el evento anterior.

    Nota: `loan.initial_amount` y `outstanding_balance` reflejan el estado ACTUAL
    (incluyen incrementos ya aplicados), así que el saldo de apertura se
    reconstruye restando los incrementos históricos para no duplicarlos al
    reproducirlos en orden cronológico.
    """
    from app.models.base import Loan, Transaction

    loan = db.get(Loan, loan_id)
    if not loan:
        raise ValueError(f"Préstamo {loan_id} no encontrado.")
    if fecha_corte is None:
        fecha_corte = date.today()

    rate = Decimal(str(loan.interest_rate))
    txns = (
        db.query(Transaction)
        .filter(Transaction.loan_id == loan_id)
        .order_by(Transaction.transaction_date, Transaction.id)
        .all()
    )

    total_incrementos = sum(
        (Decimal(str(t.amount)) for t in txns if t.transaction_type == "loan_increment"),
        Decimal("0"),
    )
    balance = (Decimal(str(loan.initial_amount)) - total_incrementos).quantize(CENT, rounding=ROUND_HALF_UP)
    prev_date = loan.start_date

    eventos: list[EventoTimeline] = [
        EventoTimeline(
            fecha=loan.start_date, tipo="apertura", monto=balance,
            dias_desde_anterior=0, interes_generado_periodo=Decimal("0.00"),
            saldo_antes=Decimal("0.00"), saldo_despues=balance,
            notas="Apertura del préstamo", transaction_id=None,
        )
    ]

    interes_generado_total = Decimal("0.00")
    interes_pagado_total = Decimal("0.00")

    for txn in txns:
        dias = max(0, (txn.transaction_date - prev_date).days)
        interes_periodo = (
            balance * rate * Decimal(str(dias)) / DAYS_PER_MONTH
        ).quantize(CENT, rounding=ROUND_HALF_UP)
        interes_generado_total += interes_periodo

        saldo_antes = balance
        monto = Decimal(str(txn.amount))

        if txn.transaction_type == "capital_payment":
            balance = max(Decimal("0"), (balance - monto).quantize(CENT, rounding=ROUND_HALF_UP))
        elif txn.transaction_type == "loan_increment":
            balance = (balance + monto).quantize(CENT, rounding=ROUND_HALF_UP)
        elif txn.transaction_type == "interest_payment":
            interes_pagado_total += monto
            # No modifica el saldo insoluto.

        eventos.append(EventoTimeline(
            fecha=txn.transaction_date, tipo=txn.transaction_type, monto=monto,
            dias_desde_anterior=dias, interes_generado_periodo=interes_periodo,
            saldo_antes=saldo_antes, saldo_despues=balance,
            notas=txn.notes, transaction_id=txn.id,
        ))
        prev_date = txn.transaction_date

    if loan.status == "active" and fecha_corte > prev_date:
        dias = (fecha_corte - prev_date).days
        interes_periodo = (
            balance * rate * Decimal(str(dias)) / DAYS_PER_MONTH
        ).quantize(CENT, rounding=ROUND_HALF_UP)
        interes_generado_total += interes_periodo
        eventos.append(EventoTimeline(
            fecha=fecha_corte, tipo="hoy", monto=Decimal("0.00"),
            dias_desde_anterior=dias, interes_generado_periodo=interes_periodo,
            saldo_antes=balance, saldo_despues=balance,
            notas="Interés acumulado a la fecha (pendiente de pago)", transaction_id=None,
        ))

    return LoanTimelineResult(
        loan_id=loan_id,
        eventos=eventos,
        interes_generado_total=interes_generado_total.quantize(CENT, rounding=ROUND_HALF_UP),
        interes_pagado_total=interes_pagado_total.quantize(CENT, rounding=ROUND_HALF_UP),
        interes_pendiente=(interes_generado_total - interes_pagado_total).quantize(CENT, rounding=ROUND_HALF_UP),
        saldo_actual=balance,
    )
