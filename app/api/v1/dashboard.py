from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.base import CajaConfig, Loan, Member, Transaction
from app.services.finance_engine import calculate_monthly_interest

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])

CYCLE_END = date(2026, 11, 30)


class DashboardMetrics(BaseModel):
    caja_id: int
    caja_name: str
    total_en_caja: Decimal          # ahorros acumulados
    capital_en_prestamos: Decimal   # saldo insoluto total
    intereses_totales: Decimal      # rendimiento histórico (all-time)
    proyeccion_cierre: Decimal      # ahorros + intereses proyectados al cierre
    capital_disponible: Decimal
    intereses_mes: Decimal
    prestamos_activos: int
    total_socios: int


@router.get("/metrics", response_model=DashboardMetrics)
def get_metrics(caja_id: int = Query(...), db: Session = Depends(get_db)):
    caja = db.get(CajaConfig, caja_id)
    if not caja:
        raise HTTPException(status_code=404, detail="Caja no encontrada.")

    capital_en_prestamos = Decimal(str(
        db.query(func.sum(Loan.outstanding_balance))
        .filter(Loan.caja_id == caja_id, Loan.status == "active")
        .scalar() or 0
    ))

    total_ahorros = Decimal(str(
        db.query(func.sum(Member.savings_balance))
        .filter(Member.caja_id == caja_id)
        .scalar() or 0
    ))

    # All-time interest collected
    intereses_totales = Decimal(str(
        db.query(func.sum(Transaction.amount))
        .join(Loan, Transaction.loan_id == Loan.id)
        .filter(Loan.caja_id == caja_id, Transaction.transaction_type == "interest_payment")
        .scalar() or 0
    ))

    # This-month interest
    start_of_month = date.today().replace(day=1)
    intereses_mes = Decimal(str(
        db.query(func.sum(Transaction.amount))
        .join(Loan, Transaction.loan_id == Loan.id)
        .filter(
            Loan.caja_id == caja_id,
            Transaction.transaction_type == "interest_payment",
            Transaction.transaction_date >= start_of_month,
        )
        .scalar() or 0
    ))

    # Proyección de cierre: ahorros actuales + intereses que generarán los
    # préstamos activos durante los meses restantes del ciclo
    today = date.today()
    remaining_months = max(0, round((CYCLE_END - today).days / 30))
    active_loans = (
        db.query(Loan)
        .filter(Loan.caja_id == caja_id, Loan.status == "active")
        .all()
    )
    intereses_proyectados = sum(
        calculate_monthly_interest(l.outstanding_balance, l.interest_rate) * remaining_months
        for l in active_loans
    )
    proyeccion_cierre = (total_ahorros + intereses_totales + Decimal(str(intereses_proyectados))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    prestamos_activos = (
        db.query(func.count(Loan.id))
        .filter(Loan.caja_id == caja_id, Loan.status == "active")
        .scalar() or 0
    )
    total_socios = (
        db.query(func.count(Member.id))
        .filter(Member.caja_id == caja_id, Member.is_active == True)
        .scalar() or 0
    )

    return DashboardMetrics(
        caja_id=caja_id,
        caja_name=caja.name,
        total_en_caja=total_ahorros,
        capital_en_prestamos=capital_en_prestamos,
        intereses_totales=intereses_totales,
        proyeccion_cierre=proyeccion_cierre,
        capital_disponible=total_ahorros - capital_en_prestamos,
        intereses_mes=intereses_mes,
        prestamos_activos=prestamos_activos,
        total_socios=total_socios,
    )


@router.get("/chart-data")
def get_chart_data(caja_id: int = Query(...), db: Session = Depends(get_db)):
    """Datos mensuales acumulados para el gráfico de crecimiento."""
    if not db.get(CajaConfig, caja_id):
        raise HTTPException(status_code=404, detail="Caja no encontrada.")

    savings_rows = (
        db.query(
            func.strftime("%Y-%m", Transaction.transaction_date).label("month"),
            func.sum(Transaction.amount).label("total"),
        )
        .join(Member, Transaction.member_id == Member.id)
        .filter(Member.caja_id == caja_id, Transaction.transaction_type == "savings")
        .group_by(func.strftime("%Y-%m", Transaction.transaction_date))
        .order_by(func.strftime("%Y-%m", Transaction.transaction_date))
        .all()
    )

    interest_rows = (
        db.query(
            func.strftime("%Y-%m", Transaction.transaction_date).label("month"),
            func.sum(Transaction.amount).label("total"),
        )
        .join(Loan, Transaction.loan_id == Loan.id)
        .filter(Loan.caja_id == caja_id, Transaction.transaction_type == "interest_payment")
        .group_by(func.strftime("%Y-%m", Transaction.transaction_date))
        .order_by(func.strftime("%Y-%m", Transaction.transaction_date))
        .all()
    )

    all_months = sorted(
        {r.month for r in savings_rows} | {r.month for r in interest_rows}
    )
    s_dict = {r.month: float(r.total) for r in savings_rows}
    i_dict = {r.month: float(r.total) for r in interest_rows}

    labels, savings_data, interest_data = [], [], []
    cum_s = cum_i = 0.0
    for m in all_months:
        cum_s += s_dict.get(m, 0.0)
        cum_i += i_dict.get(m, 0.0)
        labels.append(m)
        savings_data.append(round(cum_s, 2))
        interest_data.append(round(cum_i, 2))

    return {"labels": labels, "savings": savings_data, "interest": interest_data}
