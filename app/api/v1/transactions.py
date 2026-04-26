from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.base import Loan, Member, Transaction
from app.schemas.transactions import (
    CapitalReductionRequest,
    CapitalReductionResponse,
    PaymentRequest,
    PaymentResponse,
    SavingsRequest,
    TransactionRead,
)
from app.services.finance_engine import apply_payment

router = APIRouter(prefix="/transactions", tags=["Transactions"])


@router.post("/payment", response_model=PaymentResponse)
def register_payment(payload: PaymentRequest, db: Session = Depends(get_db)):
    loan = db.get(Loan, payload.loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="Préstamo no encontrado.")
    if loan.status != "active":
        raise HTTPException(status_code=400, detail="El préstamo no está activo.")

    breakdown = apply_payment(
        outstanding_balance=loan.outstanding_balance,
        interest_rate=loan.interest_rate,
        payment_amount=payload.amount,
    )

    txns: list[Transaction] = []

    if breakdown.interest_paid > 0:
        txns.append(
            Transaction(
                loan_id=loan.id,
                member_id=loan.member_id,
                amount=breakdown.interest_paid,
                transaction_date=payload.payment_date,
                transaction_type="interest_payment",
                notes=payload.notes,
            )
        )

    if breakdown.capital_paid > 0:
        txns.append(
            Transaction(
                loan_id=loan.id,
                member_id=loan.member_id,
                amount=breakdown.capital_paid,
                transaction_date=payload.payment_date,
                transaction_type="capital_payment",
                notes=payload.notes,
            )
        )

    for t in txns:
        db.add(t)

    loan.outstanding_balance = breakdown.remaining_balance
    if breakdown.loan_fully_paid:
        loan.status = "paid"

    db.commit()
    for t in txns:
        db.refresh(t)

    return PaymentResponse(
        interest_paid=breakdown.interest_paid,
        capital_paid=breakdown.capital_paid,
        new_outstanding_balance=breakdown.remaining_balance,
        loan_fully_paid=breakdown.loan_fully_paid,
        overpayment=breakdown.overpayment,
        transactions=[TransactionRead.model_validate(t) for t in txns],
    )


@router.post(
    "/capital-reduction",
    response_model=CapitalReductionResponse,
    status_code=status.HTTP_201_CREATED,
)
def capital_reduction(payload: CapitalReductionRequest, db: Session = Depends(get_db)):
    from decimal import Decimal as _D, ROUND_HALF_UP

    loan = db.get(Loan, payload.loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="Préstamo no encontrado.")
    if loan.status != "active":
        raise HTTPException(status_code=400, detail="El préstamo no está activo.")

    CENT = _D("0.01")
    balance = _D(str(loan.outstanding_balance))
    capital_paid = min(payload.amount, balance).quantize(CENT, rounding=ROUND_HALF_UP)
    new_balance = (balance - capital_paid).quantize(CENT, rounding=ROUND_HALF_UP)
    fully_paid = new_balance == _D("0")

    t = Transaction(
        loan_id=loan.id,
        member_id=loan.member_id,
        amount=capital_paid,
        transaction_date=payload.payment_date,
        transaction_type="capital_payment",
        notes=payload.notes,
    )
    db.add(t)
    loan.outstanding_balance = new_balance
    if fully_paid:
        loan.status = "paid"

    db.commit()
    db.refresh(t)

    return CapitalReductionResponse(
        capital_paid=capital_paid,
        new_outstanding_balance=new_balance,
        loan_fully_paid=fully_paid,
        transaction=TransactionRead.model_validate(t),
    )


@router.post(
    "/savings", response_model=TransactionRead, status_code=status.HTTP_201_CREATED
)
def register_savings(payload: SavingsRequest, db: Session = Depends(get_db)):
    member = db.get(Member, payload.member_id)
    if not member:
        raise HTTPException(status_code=404, detail="Socio no encontrado.")

    t = Transaction(
        loan_id=None,
        member_id=payload.member_id,
        amount=payload.amount,
        transaction_date=payload.transaction_date,
        transaction_type="savings",
        notes=payload.notes,
    )
    db.add(t)
    member.savings_balance += payload.amount
    db.commit()
    db.refresh(t)
    return t
