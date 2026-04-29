"""
Quincenas API — SafeFund Ledger

Gestiona el panel de control de pagos quincenales.
La lógica es de toggle: si una quincena NO está en quincenas_pendientes → pagada (verde).
Si está → pendiente (amarillo). Click alterna el estado.
"""

from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.base import CajaConfig, Member, QuincenaPendiente

router = APIRouter(prefix="/quincenas", tags=["Quincenas"])


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_current_quincena(start_date: date, total_quincenas: int) -> int:
    """Quincena actual (1-indexed), 0 si el ciclo no ha iniciado."""
    today = date.today()
    if today < start_date:
        return 0
    days = (today - start_date).days
    q = (days // 15) + 1
    return min(q, total_quincenas)


def _quincena_start(start_date: date, num: int) -> date:
    return start_date + timedelta(days=(num - 1) * 15)


def _quincena_end(start_date: date, num: int) -> date:
    return start_date + timedelta(days=num * 15 - 1)


# ── Schemas ────────────────────────────────────────────────────────────────────

class ToggleRequest(BaseModel):
    member_id: int
    caja_id: int
    quincena_num: int


class AlCorrienteBulkRequest(BaseModel):
    caja_id: int
    quincena_hasta: int
    exclude_member_ids: list[int] = []


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/data")
def get_quincenas_data(caja_id: int = Query(...), db: Session = Depends(get_db)):
    """
    Retorna todos los datos para renderizar el panel de quincenas:
    - Info de la caja (start_date, quota_amount, total_quincenas, quincena_actual)
    - Miembros en orden de creación (id ASC)
    - Set de (member_id, quincena_num) con estado 'pendiente'
    """
    caja = db.get(CajaConfig, caja_id)
    if not caja:
        raise HTTPException(status_code=404, detail="Caja no encontrada.")

    if not caja.start_date:
        raise HTTPException(
            status_code=400,
            detail="La caja no tiene fecha de inicio configurada. Edítala en Configuración.",
        )

    current_q = _get_current_quincena(caja.start_date, caja.total_quincenas)

    members = (
        db.query(Member)
        .filter(Member.caja_id == caja_id, Member.is_active == True)
        .order_by(Member.id)
        .all()
    )

    # Obtener todas las quincenas pendientes de esta caja
    pendientes_rows = (
        db.query(QuincenaPendiente)
        .filter(QuincenaPendiente.caja_id == caja_id)
        .all()
    )
    pendientes_set = {(r.member_id, r.quincena_num) for r in pendientes_rows}

    members_data = [
        {
            "id":   m.id,
            "name": m.name,
            "member_type": m.member_type,
        }
        for m in members
    ]

    # Construir quincena labels
    quincenas_info = []
    for q in range(1, caja.total_quincenas + 1):
        q_start = _quincena_start(caja.start_date, q)
        quincenas_info.append({
            "num":   q,
            "label": f"Q{q}",
            "fecha": str(q_start),
            "pasada": q <= current_q,
        })

    # Capital ideal vs real
    n_socios_dentro = sum(1 for m in members if m.member_type == "dentro")
    cuota = float(caja.quota_amount)
    capital_ideal = n_socios_dentro * cuota * current_q

    pendientes_pasadas = sum(
        1 for (mid, qnum) in pendientes_set
        if qnum <= current_q and any(m.id == mid and m.member_type == "dentro" for m in members)
    )
    capital_real = capital_ideal - (pendientes_pasadas * cuota)

    return {
        "caja": {
            "id":              caja.id,
            "name":            caja.name,
            "start_date":      str(caja.start_date),
            "quota_amount":    cuota,
            "total_quincenas": caja.total_quincenas,
            "quincena_actual": current_q,
        },
        "members":    members_data,
        "quincenas":  quincenas_info,
        "pendientes": [{"member_id": mid, "quincena_num": qnum} for mid, qnum in pendientes_set],
        "resumen": {
            "n_socios":       len(members),
            "n_socios_dentro":n_socios_dentro,
            "cuota":          cuota,
            "capital_ideal":  round(capital_ideal, 2),
            "capital_real":   round(capital_real, 2),
            "pendientes_count": pendientes_pasadas,
        },
    }


@router.post("/toggle")
def toggle_quincena(payload: ToggleRequest, db: Session = Depends(get_db)):
    """
    Alterna el estado de una quincena entre pagada (sin registro) y pendiente (con registro).
    Devuelve el nuevo estado: "pendiente" | "pagado".
    """
    caja = db.get(CajaConfig, payload.caja_id)
    if not caja:
        raise HTTPException(status_code=404, detail="Caja no encontrada.")
    if not caja.start_date:
        raise HTTPException(status_code=400, detail="La caja no tiene fecha de inicio.")

    current_q = _get_current_quincena(caja.start_date, caja.total_quincenas)
    if payload.quincena_num > current_q:
        raise HTTPException(status_code=400, detail="No puedes marcar una quincena futura.")
    if payload.quincena_num < 1:
        raise HTTPException(status_code=400, detail="Número de quincena inválido.")

    member = db.get(Member, payload.member_id)
    if not member or member.caja_id != payload.caja_id:
        raise HTTPException(status_code=404, detail="Socio no encontrado en esta caja.")

    existing = (
        db.query(QuincenaPendiente)
        .filter(
            QuincenaPendiente.member_id == payload.member_id,
            QuincenaPendiente.caja_id == payload.caja_id,
            QuincenaPendiente.quincena_num == payload.quincena_num,
        )
        .first()
    )

    if existing:
        db.delete(existing)
        db.commit()
        return {"estado": "pagado", "member_id": payload.member_id, "quincena_num": payload.quincena_num}
    else:
        nuevo = QuincenaPendiente(
            member_id=payload.member_id,
            caja_id=payload.caja_id,
            quincena_num=payload.quincena_num,
        )
        db.add(nuevo)
        db.commit()
        return {"estado": "pendiente", "member_id": payload.member_id, "quincena_num": payload.quincena_num}


@router.post("/al-corriente-bulk")
def al_corriente_bulk(payload: AlCorrienteBulkRequest, db: Session = Depends(get_db)):
    """Marca al corriente a todos los socios de la caja, excepto los indicados en exclude_member_ids."""
    caja = db.get(CajaConfig, payload.caja_id)
    if not caja:
        raise HTTPException(status_code=404, detail="Caja no encontrada.")
    if not caja.start_date:
        raise HTTPException(status_code=400, detail="La caja no tiene fecha de inicio configurada.")

    current_q = _get_current_quincena(caja.start_date, caja.total_quincenas)
    quincena_hasta = min(payload.quincena_hasta, current_q)
    if quincena_hasta < 1:
        raise HTTPException(status_code=400, detail="Número de quincena inválido.")

    members = (
        db.query(Member)
        .filter(Member.caja_id == payload.caja_id, Member.is_active == True)
        .all()
    )

    excluded_set = set(payload.exclude_member_ids)
    total_deleted = 0
    processed = 0

    for m in members:
        if m.id in excluded_set:
            continue
        deleted = (
            db.query(QuincenaPendiente)
            .filter(
                QuincenaPendiente.member_id == m.id,
                QuincenaPendiente.caja_id == payload.caja_id,
                QuincenaPendiente.quincena_num <= quincena_hasta,
            )
            .delete()
        )
        total_deleted += deleted
        processed += 1

    db.commit()
    return {"ok": True, "processed": processed, "deleted": total_deleted, "quincena_hasta": quincena_hasta}


@router.post("/al-corriente")
def marcar_al_corriente(
    member_id: int = Query(...),
    caja_id: int = Query(...),
    db: Session = Depends(get_db),
):
    """Elimina todos los pendientes de un socio hasta la quincena actual (lo pone al corriente)."""
    caja = db.get(CajaConfig, caja_id)
    if not caja:
        raise HTTPException(status_code=404, detail="Caja no encontrada.")
    if not caja.start_date:
        raise HTTPException(status_code=400, detail="La caja no tiene fecha de inicio configurada.")

    member = db.get(Member, member_id)
    if not member or member.caja_id != caja_id:
        raise HTTPException(status_code=404, detail="Socio no encontrado en esta caja.")

    current_q = _get_current_quincena(caja.start_date, caja.total_quincenas)
    deleted = (
        db.query(QuincenaPendiente)
        .filter(
            QuincenaPendiente.member_id == member_id,
            QuincenaPendiente.caja_id == caja_id,
            QuincenaPendiente.quincena_num <= current_q,
        )
        .delete()
    )
    db.commit()
    return {"ok": True, "deleted": deleted, "quincena_actual": current_q, "member_name": member.name}


@router.post("/marcar-todos-pagados")
def marcar_todos_pagados(caja_id: int = Query(...), quincena_num: int = Query(...), db: Session = Depends(get_db)):
    """Elimina todos los pendientes de una quincena específica (marca todos como pagados)."""
    caja = db.get(CajaConfig, caja_id)
    if not caja:
        raise HTTPException(status_code=404, detail="Caja no encontrada.")
    db.query(QuincenaPendiente).filter(
        QuincenaPendiente.caja_id == caja_id,
        QuincenaPendiente.quincena_num == quincena_num,
    ).delete()
    db.commit()
    return {"ok": True, "quincena_num": quincena_num}
