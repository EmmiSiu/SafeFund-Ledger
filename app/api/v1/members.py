from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.base import Aportacion, CajaConfig, Loan, Member, QuincenaPendiente
from app.schemas.members import MemberCreate, MemberRead

router = APIRouter(prefix="/members", tags=["Members"])


@router.get("/quota-amounts")
def get_quota_amounts(caja_id: int = Query(...), db: Session = Depends(get_db)):
    """Retorna los montos de cuota distintos ya usados en esta caja (para sugerencias)."""
    rows = (
        db.query(Member.quota_personal)
        .filter(Member.caja_id == caja_id, Member.quota_personal > 0)
        .distinct()
        .order_by(Member.quota_personal)
        .all()
    )
    return {"amounts": sorted({float(r.quota_personal) for r in rows})}


@router.get("/", response_model=list[MemberRead])
def list_members(caja_id: int = Query(...), db: Session = Depends(get_db)):
    return (
        db.query(Member)
        .filter(Member.caja_id == caja_id, Member.is_active == True)
        .order_by(Member.name)
        .all()
    )


@router.get("/{member_id}", response_model=MemberRead)
def get_member(member_id: int, db: Session = Depends(get_db)):
    member = db.get(Member, member_id)
    if not member:
        raise HTTPException(status_code=404, detail="Socio no encontrado.")
    return member


@router.post("/", response_model=MemberRead, status_code=status.HTTP_201_CREATED)
def create_member(payload: MemberCreate, db: Session = Depends(get_db)):
    if not db.get(CajaConfig, payload.caja_id):
        raise HTTPException(status_code=404, detail="Caja no encontrada.")

    from app.models.base import MemberGroup
    if not db.get(MemberGroup, payload.group_id):
        raise HTTPException(status_code=404, detail="Grupo no encontrado.")
    member = Member(**payload.model_dump())
    db.add(member)
    db.commit()
    db.refresh(member)
    return member


class CuotaUpdate(BaseModel):
    quota_personal: Decimal = Field(..., ge=0)


@router.patch("/{member_id}/cuota", response_model=MemberRead)
def update_cuota(member_id: int, payload: CuotaUpdate, db: Session = Depends(get_db)):
    """Actualiza la cuota quincenal personal del socio."""
    member = db.get(Member, member_id)
    if not member:
        raise HTTPException(status_code=404, detail="Socio no encontrado.")
    member.quota_personal = payload.quota_personal
    db.commit()
    db.refresh(member)
    return member


class InterestRateUpdate(BaseModel):
    interest_rate: Optional[Decimal] = Field(None, ge=0, le=1)


@router.patch("/{member_id}/interest-rate", response_model=MemberRead)
def update_interest_rate(member_id: int, payload: InterestRateUpdate, db: Session = Depends(get_db)):
    """
    Actualiza la tasa de interés personalizada del socio (usada como default al
    crear un préstamo para él). None restaura el uso de la tasa por defecto de la caja.
    """
    member = db.get(Member, member_id)
    if not member:
        raise HTTPException(status_code=404, detail="Socio no encontrado.")
    member.interest_rate = payload.interest_rate
    db.commit()
    db.refresh(member)
    return member


class RecalcularRequest(BaseModel):
    caja_id: int


@router.post("/recalcular-aportaciones")
def recalcular_aportaciones(payload: RecalcularRequest, db: Session = Depends(get_db)):
    """
    Corrige las Aportaciones _cuota_ con monto=0 para socios que ya tienen quota_personal > 0.
    Útil cuando al-corriente se corrió antes de configurar la cuota personal.
    """
    from datetime import date

    members = (
        db.query(Member)
        .filter(
            Member.caja_id == payload.caja_id,
            Member.is_active == True,
            Member.member_type == "dentro",
            Member.quota_personal > 0,
        )
        .all()
    )

    updated = 0
    for m in members:
        cuota = m.quota_personal
        pendientes = db.query(QuincenaPendiente).filter(
            QuincenaPendiente.member_id == m.id,
            QuincenaPendiente.caja_id == payload.caja_id,
        ).all()

        for qp in pendientes:
            apo = db.query(Aportacion).filter(
                Aportacion.member_id == m.id,
                Aportacion.caja_id == payload.caja_id,
                Aportacion.quincena == f"Q{qp.quincena_num}",
                Aportacion.notas == "_cuota_",
            ).first()

            if apo:
                if apo.monto != cuota:
                    apo.monto = cuota
                    updated += 1
            else:
                db.add(Aportacion(
                    member_id=m.id,
                    caja_id=payload.caja_id,
                    monto=cuota,
                    quincena=f"Q{qp.quincena_num}",
                    notas="_cuota_",
                    fecha_aportacion=date.today(),
                ))
                updated += 1

    db.commit()

    # Recalcular savings_balance desde la suma real en BD
    for m in members:
        apo_sum = db.query(func.coalesce(func.sum(Aportacion.monto), 0)).filter(
            Aportacion.member_id == m.id,
            Aportacion.caja_id == payload.caja_id,
        ).scalar() or Decimal("0")
        m.savings_balance = Decimal(str(apo_sum))
    db.commit()

    return {"ok": True, "updated": updated, "members_processed": len(members)}


class RetirarMemberRequest(BaseModel):
    motivo: str | None = None
    fecha_retiro: date | None = None


class RetirarMemberResponse(BaseModel):
    member_id: int
    member_name: str
    monto_devuelto: float
    fecha_retiro: date
    message: str


@router.post("/{member_id}/retirar", response_model=RetirarMemberResponse)
def retirar_member(
    member_id: int,
    payload: RetirarMemberRequest,
    db: Session = Depends(get_db),
):
    """
    Retira a un socio de la caja sin intereses (baja definitiva):
    - Valida que no tenga préstamos activos con saldo insoluto
    - Registra una Aportación negativa (log contable que descuenta del capital total de la caja)
    - Pone savings_balance = 0
    - Pone is_active = False (excluyéndolo de métricas activas, quincenas y rendimientos)
    """
    member = db.get(Member, member_id)
    if not member:
        raise HTTPException(status_code=404, detail="Socio no encontrado.")
    if not member.is_active:
        raise HTTPException(status_code=400, detail="El socio ya se encuentra inactivo.")

    # Validar préstamos activos
    active_loans = (
        db.query(Loan)
        .filter(Loan.member_id == member.id, Loan.status == "active", Loan.outstanding_balance > 0)
        .all()
    )
    if active_loans:
        total_debt = sum(Decimal(str(l.outstanding_balance)) for l in active_loans)
        raise HTTPException(
            status_code=400,
            detail=f"No se puede retirar al socio porque tiene préstamos activos con deuda total de ${total_debt:,.2f}. Debe liquidar sus préstamos primero.",
        )

    fecha = payload.fecha_retiro or date.today()
    monto_devolucion = Decimal(str(member.savings_balance))

    # Si tenía ahorros acumulados, registrar aportación negativa de salida (log contable)
    if monto_devolucion > 0:
        db.add(
            Aportacion(
                member_id=member.id,
                caja_id=member.caja_id,
                monto=-monto_devolucion,
                fecha_aportacion=fecha,
                quincena=None,
                notas=f"[RETIRO] {payload.motivo or 'Baja de socio sin intereses'}",
            )
        )

    member.savings_balance = Decimal("0.00")
    member.is_active = False

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al procesar el retiro del socio: {exc}")

    return RetirarMemberResponse(
        member_id=member.id,
        member_name=member.name,
        monto_devuelto=float(monto_devolucion),
        fecha_retiro=fecha,
        message=f"Socio {member.name} retirado exitosamente. Se devolvió ${monto_devolucion:,.2f} sin intereses.",
    )

