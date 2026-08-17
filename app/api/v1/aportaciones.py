from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.money import CENT
from app.models.base import Aportacion, CajaConfig, Loan, Member, Transaction
from app.schemas.aportaciones import (
    AportacionCreate,
    AportacionRead,
    CapitalDisponibleResponse,
)

router = APIRouter(prefix="/aportaciones", tags=["Aportaciones"])


@router.post("/", response_model=AportacionRead, status_code=status.HTTP_201_CREATED)
def registrar_aportacion(payload: AportacionCreate, db: Session = Depends(get_db)):
    """
    Registra una aportación quincenal de un socio al capital de la caja.
    También actualiza el savings_balance del socio de forma atómica.
    """
    member = db.get(Member, payload.member_id)
    if not member:
        raise HTTPException(status_code=404, detail="Socio no encontrado.")
    if not db.get(CajaConfig, payload.caja_id):
        raise HTTPException(status_code=404, detail="Caja no encontrada.")
    if member.caja_id != payload.caja_id:
        raise HTTPException(status_code=400, detail="El socio no pertenece a esta caja.")

    try:
        aportacion = Aportacion(
            member_id=payload.member_id,
            caja_id=payload.caja_id,
            monto=payload.monto,
            fecha_aportacion=payload.fecha_aportacion,
            quincena=payload.quincena,
            notas=payload.notas,
        )
        db.add(aportacion)
        member.savings_balance = Decimal(str(member.savings_balance)) + payload.monto
        db.commit()
        db.refresh(aportacion)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al registrar aportación: {exc}")

    result = AportacionRead.model_validate(aportacion)
    result.member_name = member.name
    return result


@router.get("/", response_model=list[AportacionRead])
def list_aportaciones(
    caja_id: int = Query(...),
    member_id: int = Query(None),
    db: Session = Depends(get_db),
):
    """Lista aportaciones de una caja, con filtro opcional por socio."""
    q = db.query(Aportacion).filter(Aportacion.caja_id == caja_id)
    if member_id:
        q = q.filter(Aportacion.member_id == member_id)
    aportaciones = q.order_by(Aportacion.fecha_aportacion.desc()).all()

    result = []
    members_cache: dict[int, str] = {}
    for a in aportaciones:
        if a.member_id not in members_cache:
            m = db.get(Member, a.member_id)
            members_cache[a.member_id] = m.name if m else ""
        r = AportacionRead.model_validate(a)
        r.member_name = members_cache[a.member_id]
        result.append(r)
    return result


@router.get("/capital-disponible", response_model=CapitalDisponibleResponse)
def get_capital_disponible(caja_id: int = Query(...), db: Session = Depends(get_db)):
    """
    Calcula el capital disponible en caja:
      Capital disponible = Total aportado + Intereses cobrados - Capital en préstamos activos
    """
    if not db.get(CajaConfig, caja_id):
        raise HTTPException(status_code=404, detail="Caja no encontrada.")

    capital_aportado = db.query(func.sum(Aportacion.monto)).filter(
        Aportacion.caja_id == caja_id
    ).scalar() or Decimal("0")

    capital_prestado = db.query(func.sum(Loan.outstanding_balance)).filter(
        Loan.caja_id == caja_id,
        Loan.status == "active",
    ).scalar() or Decimal("0")

    intereses_cobrados = db.query(func.sum(Transaction.amount)).join(
        Loan, Transaction.loan_id == Loan.id
    ).filter(
        Loan.caja_id == caja_id,
        Transaction.transaction_type == "interest_payment",
    ).scalar() or Decimal("0")

    n_aportaciones = db.query(func.count(Aportacion.id)).filter(
        Aportacion.caja_id == caja_id
    ).scalar() or 0

    n_prestamos = db.query(func.count(Loan.id)).filter(
        Loan.caja_id == caja_id,
        Loan.status == "active",
    ).scalar() or 0

    capital_aportado = Decimal(str(capital_aportado))
    capital_prestado = Decimal(str(capital_prestado))
    intereses_cobrados = Decimal(str(intereses_cobrados))

    capital_disponible = (capital_aportado + intereses_cobrados - capital_prestado).quantize(CENT)

    rendimiento_pct = (
        (intereses_cobrados / capital_aportado * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if capital_aportado > 0 else Decimal("0.00")
    )

    return CapitalDisponibleResponse(
        caja_id=caja_id,
        capital_aportado_total=capital_aportado,
        capital_en_prestamos=capital_prestado,
        intereses_cobrados=intereses_cobrados,
        capital_disponible=capital_disponible,
        rendimiento_pct=rendimiento_pct,
        n_aportaciones=n_aportaciones,
        n_prestamos_activos=n_prestamos,
    )


@router.delete("/{aportacion_id}", status_code=status.HTTP_204_NO_CONTENT)
def eliminar_aportacion(aportacion_id: int, db: Session = Depends(get_db)):
    """Elimina una aportación y revierte el savings_balance del socio."""
    aportacion = db.get(Aportacion, aportacion_id)
    if not aportacion:
        raise HTTPException(status_code=404, detail="Aportación no encontrada.")

    member = db.get(Member, aportacion.member_id)
    try:
        if member:
            member.savings_balance = (
                Decimal(str(member.savings_balance)) - Decimal(str(aportacion.monto))
            ).quantize(CENT)
        db.delete(aportacion)
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al eliminar aportación: {exc}")
