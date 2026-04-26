from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.base import CajaConfig, Loan, Member
from app.schemas.cajas import (
    CajaConfigCreate,
    CajaConfigRead,
    CajaInitializeRequest,
    CajaInitializeResponse,
)

router = APIRouter(prefix="/cajas", tags=["Cajas"])


@router.post("/", response_model=CajaConfigRead, status_code=status.HTTP_201_CREATED)
def create_caja(payload: CajaConfigCreate, db: Session = Depends(get_db)):
    if db.query(CajaConfig).filter(CajaConfig.name == payload.name).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya existe una caja con el nombre '{payload.name}'.",
        )
    caja = CajaConfig(**payload.model_dump())
    db.add(caja)
    db.commit()
    db.refresh(caja)
    return caja


@router.get("/{caja_id}", response_model=CajaConfigRead)
def get_caja(caja_id: int, db: Session = Depends(get_db)):
    caja = db.get(CajaConfig, caja_id)
    if not caja:
        raise HTTPException(status_code=404, detail="Caja no encontrada.")
    return caja


@router.post(
    "/{caja_id}/initialize",
    response_model=CajaInitializeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Inicializar caja con saldos manuales (migración)",
)
def initialize_caja(
    caja_id: int,
    payload: CajaInitializeRequest,
    db: Session = Depends(get_db),
):
    """
    Carga saldos históricos de socios y préstamos directamente en la BD.
    Útil para la migración desde archivos CSV/Excel.
    No valida pagos anteriores — solo establece el estado actual.
    """
    caja = db.get(CajaConfig, caja_id)
    if not caja:
        raise HTTPException(status_code=404, detail="Caja no encontrada.")

    members_created = 0
    loans_created = 0
    total_savings = Decimal("0.00")
    total_outstanding = Decimal("0.00")

    for member_data in payload.members:
        member = Member(
            name=member_data.name,
            group=member_data.group,
            caja_id=caja_id,
            savings_balance=member_data.savings_balance,
        )
        db.add(member)
        db.flush()  # obtener member.id antes del commit
        members_created += 1
        total_savings += member_data.savings_balance

        for loan_data in member_data.loans:
            loan = Loan(
                member_id=member.id,
                caja_id=caja_id,
                initial_amount=loan_data.initial_amount,
                outstanding_balance=loan_data.outstanding_balance,
                interest_rate=loan_data.interest_rate,
                loan_type=loan_data.loan_type,
                start_date=loan_data.start_date,
                status="active" if loan_data.outstanding_balance > 0 else "paid",
            )
            db.add(loan)
            loans_created += 1
            total_outstanding += loan_data.outstanding_balance

    db.commit()

    return CajaInitializeResponse(
        caja_id=caja_id,
        members_created=members_created,
        loans_created=loans_created,
        total_savings=total_savings,
        total_outstanding=total_outstanding,
    )
