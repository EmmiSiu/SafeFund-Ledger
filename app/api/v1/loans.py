from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session, joinedload
from typing import Optional

from app.core.database import get_db
from app.models.base import CajaConfig, Loan, Member
from app.schemas.loans import LoanCreate, LoanRead

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
