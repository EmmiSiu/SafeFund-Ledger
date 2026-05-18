from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.base import Aportacion, CajaConfig, Member, QuincenaPendiente
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
