from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session, joinedload

from app.core.database import get_db
from app.models.base import CajaConfig, Loan, Member, Transaction
from app.schemas.loans import (
    AbonoCapitalRequest,
    AbonoCapitalResponse,
    InteresAcumuladoResponse,
    LoanCreate,
    LoanDetailRead,
    LoanRead,
    TransaccionResumen,
)
from app.services.loan_engine import calculate_accrued_interest

router = APIRouter(prefix="/loans", tags=["Loans"])


@router.get("/", response_model=list[LoanRead])
def list_loans(
    caja_id: int = Query(...),
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    q = (
        db.query(Loan)
        .options(joinedload(Loan.member))
        .filter(Loan.caja_id == caja_id)
    )
    if status:
        q = q.filter(Loan.status == status)
    loans = q.order_by(Loan.id).all()

    result = []
    for loan in loans:
        data = LoanRead.model_validate(loan)
        data.member_name = loan.member.name if loan.member else ""
        result.append(data)
    return result


@router.get("/{loan_id}", response_model=LoanRead)
def get_loan(loan_id: int, db: Session = Depends(get_db)):
    loan = (
        db.query(Loan)
        .options(joinedload(Loan.member))
        .filter(Loan.id == loan_id)
        .first()
    )
    if not loan:
        raise HTTPException(status_code=404, detail="Préstamo no encontrado.")
    data = LoanRead.model_validate(loan)
    data.member_name = loan.member.name if loan.member else ""
    return data


@router.post("/", response_model=LoanRead, status_code=status.HTTP_201_CREATED)
def create_loan(payload: LoanCreate, db: Session = Depends(get_db)):
    caja = db.get(CajaConfig, payload.caja_id)
    if not caja:
        raise HTTPException(status_code=404, detail="Caja no encontrada.")
    member = db.get(Member, payload.member_id)
    if not member:
        raise HTTPException(status_code=404, detail="Socio no encontrado.")

    rate = payload.interest_rate
    if rate is None:
        rate = (
            caja.interest_rate_internal
            if payload.loan_type == "internal"
            else caja.interest_rate_external
        )

    loan = Loan(
        member_id=payload.member_id,
        caja_id=payload.caja_id,
        initial_amount=payload.initial_amount,
        outstanding_balance=payload.initial_amount,
        interest_rate=rate,
        loan_type=payload.loan_type,
        start_date=payload.start_date,
    )
    db.add(loan)
    db.commit()
    db.refresh(loan)
    data = LoanRead.model_validate(loan)
    data.member_name = member.name
    return data


@router.get("/{loan_id}/detalle", response_model=LoanDetailRead)
def get_loan_detail(loan_id: int, db: Session = Depends(get_db)):
    """Detalle completo: historial de transacciones + interés acumulado a hoy."""
    loan = (
        db.query(Loan)
        .options(joinedload(Loan.member), joinedload(Loan.transactions))
        .filter(Loan.id == loan_id)
        .first()
    )
    if not loan:
        raise HTTPException(status_code=404, detail="Préstamo no encontrado.")

    today = date.today()
    CENT = Decimal("0.01")
    balance = Decimal(str(loan.outstanding_balance))
    rate = Decimal(str(loan.interest_rate))
    interes_mes = (balance * rate).quantize(CENT, rounding=ROUND_HALF_UP)

    try:
        acumulado = calculate_accrued_interest(loan_id, today, db)
        interes_acumulado = acumulado.interes_acumulado
    except Exception:
        interes_acumulado = Decimal("0.00")

    historial = sorted(loan.transactions, key=lambda t: (t.transaction_date, t.id), reverse=True)

    detail = LoanDetailRead(
        id=loan.id,
        member_id=loan.member_id,
        member_name=loan.member.name if loan.member else "",
        caja_id=loan.caja_id,
        initial_amount=Decimal(str(loan.initial_amount)),
        outstanding_balance=balance,
        interest_rate=rate,
        loan_type=loan.loan_type,
        status=loan.status,
        start_date=loan.start_date,
        created_at=loan.created_at,
        historial=[TransaccionResumen.model_validate(t) for t in historial],
        interes_acumulado_hoy=interes_acumulado,
        interes_mes_actual=interes_mes,
    )
    return detail


@router.get("/{loan_id}/interes-acumulado", response_model=InteresAcumuladoResponse)
def get_interes_acumulado(
    loan_id: int,
    fecha_corte: date = Query(default=None, description="Fecha de corte (default: hoy)"),
    db: Session = Depends(get_db),
):
    """Calcula el interés acumulado desde el inicio del préstamo hasta fecha_corte."""
    if fecha_corte is None:
        fecha_corte = date.today()

    loan = db.get(Loan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="Préstamo no encontrado.")

    result = calculate_accrued_interest(loan_id, fecha_corte, db)

    return InteresAcumuladoResponse(
        loan_id=result.loan_id,
        fecha_corte=result.fecha_corte,
        interes_acumulado=result.interes_acumulado,
        saldo_insoluto_actual=result.saldo_insoluto_actual,
        periodos=[
            {
                "fecha_inicio": str(p.fecha_inicio),
                "fecha_fin": str(p.fecha_fin),
                "saldo_insoluto": float(p.saldo_insoluto),
                "dias": p.dias,
                "interes": float(p.interes),
            }
            for p in result.periodos
        ],
    )


@router.post("/{loan_id}/abono-capital", response_model=AbonoCapitalResponse, status_code=status.HTTP_201_CREATED)
def declarar_abono_capital(
    loan_id: int,
    payload: AbonoCapitalRequest,
    db: Session = Depends(get_db),
):
    """
    Declara un abono directo al capital con fecha personalizada (puede ser retroactiva).
    Usa transacción atómica para garantizar consistencia del saldo insoluto.
    """
    loan = db.get(Loan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="Préstamo no encontrado.")
    if loan.status != "active":
        raise HTTPException(status_code=400, detail="El préstamo no está activo.")

    CENT = Decimal("0.01")
    balance = Decimal(str(loan.outstanding_balance))
    monto = payload.monto.quantize(CENT, rounding=ROUND_HALF_UP)

    capital_paid = min(monto, balance).quantize(CENT, rounding=ROUND_HALF_UP)
    new_balance = (balance - capital_paid).quantize(CENT, rounding=ROUND_HALF_UP)
    fully_paid = new_balance == Decimal("0")

    # Transacción atómica
    try:
        txn = Transaction(
            loan_id=loan.id,
            member_id=loan.member_id,
            amount=capital_paid,
            transaction_date=payload.fecha_abono,
            transaction_type="capital_payment",
            notes=payload.notas,
        )
        db.add(txn)
        loan.outstanding_balance = new_balance
        if fully_paid:
            loan.status = "paid"
        db.commit()
        db.refresh(txn)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al registrar abono: {exc}")

    return AbonoCapitalResponse(
        loan_id=loan.id,
        capital_abonado=capital_paid,
        nuevo_saldo_insoluto=new_balance,
        prestamo_liquidado=fully_paid,
        fecha_abono=payload.fecha_abono,
    )
