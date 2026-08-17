from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.money import CENT
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


class DeleteTransactionResponse(BaseModel):
    deleted_transaction_id: int
    transaction_type: str
    amount: float
    loan_id: int | None
    loan_outstanding_balance: float | None
    loan_status: str | None
    loan_initial_amount: float | None


@router.delete("/{txn_id}", response_model=DeleteTransactionResponse)
def delete_transaction(txn_id: int, db: Session = Depends(get_db)):
    """
    Elimina una transacción y revierte su efecto en el préstamo asociado:
    - interest_payment: solo elimina (no afecta saldo)
    - capital_payment: restaura outstanding_balance; si el préstamo estaba 'paid', lo reactiva
    - loan_increment: resta del outstanding_balance e initial_amount
    - savings: no permitido desde este endpoint
    """
    txn = db.get(Transaction, txn_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transacción no encontrada.")

    if txn.transaction_type == "savings":
        raise HTTPException(
            status_code=400,
            detail="No se pueden eliminar transacciones de ahorro desde este endpoint.",
        )

    amount = Decimal(str(txn.amount))
    loan = db.get(Loan, txn.loan_id) if txn.loan_id else None

    try:
        if txn.transaction_type == "capital_payment" and loan:
            # Restaurar el monto abonado al saldo insoluto
            old_balance = Decimal(str(loan.outstanding_balance))
            loan.outstanding_balance = (old_balance + amount).quantize(CENT, rounding=ROUND_HALF_UP)
            # Si el préstamo estaba liquidado, reactivarlo
            if loan.status == "paid":
                loan.status = "active"

        elif txn.transaction_type == "loan_increment" and loan:
            # Restar el incremento del saldo y del monto inicial
            old_balance = Decimal(str(loan.outstanding_balance))
            old_initial = Decimal(str(loan.initial_amount))
            new_balance = (old_balance - amount).quantize(CENT, rounding=ROUND_HALF_UP)
            new_initial = (old_initial - amount).quantize(CENT, rounding=ROUND_HALF_UP)
            if new_balance < Decimal("0") or new_initial < Decimal("0"):
                raise HTTPException(
                    status_code=400,
                    detail="No se puede eliminar: el saldo resultante sería negativo.",
                )
            loan.outstanding_balance = new_balance
            loan.initial_amount = new_initial

        # interest_payment: no modifica saldo, solo se elimina la transacción

        db.delete(txn)
        db.commit()
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al eliminar transacción: {exc}")

    if loan:
        db.refresh(loan)

    return DeleteTransactionResponse(
        deleted_transaction_id=txn_id,
        transaction_type=txn.transaction_type,
        amount=float(amount),
        loan_id=loan.id if loan else None,
        loan_outstanding_balance=float(loan.outstanding_balance) if loan else None,
        loan_status=loan.status if loan else None,
        loan_initial_amount=float(loan.initial_amount) if loan else None,
    )


class UpdateFechaRequest(BaseModel):
    transaction_date: date


@router.patch("/{txn_id}/fecha", response_model=TransactionRead)
def update_transaction_date(txn_id: int, payload: UpdateFechaRequest, db: Session = Depends(get_db)):
    """Corrige la fecha de un movimiento existente. Solo modifica la fecha, nada más."""
    txn = db.get(Transaction, txn_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transacción no encontrada.")
    txn.transaction_date = payload.transaction_date
    db.commit()
    db.refresh(txn)
    return txn


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
    loan = db.get(Loan, payload.loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="Préstamo no encontrado.")
    if loan.status != "active":
        raise HTTPException(status_code=400, detail="El préstamo no está activo.")

    balance = Decimal(str(loan.outstanding_balance))
    capital_paid = min(payload.amount, balance).quantize(CENT, rounding=ROUND_HALF_UP)
    new_balance = (balance - capital_paid).quantize(CENT, rounding=ROUND_HALF_UP)
    fully_paid = new_balance == Decimal("0")

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
