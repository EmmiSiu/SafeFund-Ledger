"""
savings.py — Fuente única del saldo de ahorro por socio.

Regla: ahorro del socio = Σ aportaciones (cuotas, retiros y compensaciones, con
signo) + Σ transacciones de tipo "savings" (saldos iniciales importados de Excel).

`Member.savings_balance` es una copia materializada de ese cálculo. Se actualiza
SOLO al registrar o corregir movimientos (vía `sync_member_savings`); las
pantallas de consulta lo leen pero nunca lo escriben.
"""
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.money import round_money
from app.models.base import Aportacion, Member, Transaction

ZERO = Decimal("0.00")


def savings_by_member(db: Session, caja_id: int) -> dict[int, Decimal]:
    """Ahorro calculado de todos los socios de una caja, en dos consultas agrupadas."""
    totals: dict[int, Decimal] = {}

    for member_id, total in (
        db.query(Aportacion.member_id, func.sum(Aportacion.monto))
        .filter(Aportacion.caja_id == caja_id)
        .group_by(Aportacion.member_id)
    ):
        totals[member_id] = Decimal(str(total or 0))

    for member_id, total in (
        db.query(Transaction.member_id, func.sum(Transaction.amount))
        .join(Member, Transaction.member_id == Member.id)
        .filter(Member.caja_id == caja_id, Transaction.transaction_type == "savings")
        .group_by(Transaction.member_id)
    ):
        totals[member_id] = totals.get(member_id, Decimal("0")) + Decimal(str(total or 0))

    return {mid: round_money(v) for mid, v in totals.items()}


def member_savings(db: Session, member: Member) -> Decimal:
    """Ahorro calculado de un socio desde sus movimientos."""
    apo = db.query(func.coalesce(func.sum(Aportacion.monto), 0)).filter(
        Aportacion.member_id == member.id,
        Aportacion.caja_id == member.caja_id,
    ).scalar()
    txn = db.query(func.coalesce(func.sum(Transaction.amount), 0)).filter(
        Transaction.member_id == member.id,
        Transaction.transaction_type == "savings",
    ).scalar()
    return round_money(Decimal(str(apo or 0)) + Decimal(str(txn or 0)))


def sync_member_savings(db: Session, member: Member) -> Decimal:
    """
    Recalcula y guarda `savings_balance` del socio. Llamar dentro de la misma
    transacción que registra/corrige el movimiento (hace flush antes de sumar,
    porque la sesión no usa autoflush).
    """
    db.flush()
    total = member_savings(db, member)
    if Decimal(str(member.savings_balance)) != total:
        member.savings_balance = total
    return total


def find_savings_drift(db: Session, caja_id: int) -> list[dict]:
    """Socios cuyo saldo guardado no coincide (a centavos) con sus movimientos."""
    computed = savings_by_member(db, caja_id)
    drift = []
    for m in db.query(Member).filter(Member.caja_id == caja_id).order_by(Member.name):
        real = computed.get(m.id, ZERO)
        stored = round_money(m.savings_balance)
        if stored != real:
            drift.append({
                "member_id": m.id,
                "member_name": m.name,
                "guardado": stored,
                "calculado": real,
                "diferencia": stored - real,
            })
    return drift


def reconcile_caja(db: Session, caja_id: int) -> list[dict]:
    """Corrige los saldos guardados que no cuadran. Devuelve lo que se corrigió."""
    drift = find_savings_drift(db, caja_id)
    if drift:
        by_id = {d["member_id"]: d["calculado"] for d in drift}
        for m in db.query(Member).filter(Member.id.in_(by_id.keys())):
            m.savings_balance = by_id[m.id]
    return drift
