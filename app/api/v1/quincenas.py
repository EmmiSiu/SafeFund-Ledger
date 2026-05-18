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
from app.models.base import Aportacion, CajaConfig, Member, QuincenaPendiente


def _recalc_savings(member: Member, caja_id: int, db: Session) -> None:
    """Recalcula savings_balance desde la suma real de Aportaciones en BD."""
    apo_total = db.query(func.coalesce(func.sum(Aportacion.monto), 0)).filter(
        Aportacion.member_id == member.id,
        Aportacion.caja_id == caja_id,
    ).scalar() or Decimal("0")
    member.savings_balance = Decimal(str(apo_total))


def _sync_aportaciones(member_id: int, caja_id: int, cuota: Decimal,
                        hasta_q: int, db: Session) -> None:
    """
    Para cada quincena pagada (en quincenas_pendientes) de Q1..hasta_q:
    - Si no existe aportación _cuota_: la crea con el monto correcto.
    - Si existe pero con monto diferente: la corrige.
    """
    # Mapa qnum → aportacion existente
    existing: dict[int, Aportacion] = {}
    for apo in db.query(Aportacion).filter(
        Aportacion.member_id == member_id,
        Aportacion.caja_id == caja_id,
        Aportacion.notas == "_cuota_",
    ).all():
        try:
            qnum = int(apo.quincena[1:]) if apo.quincena else 0
            if qnum > 0:
                existing[qnum] = apo
        except (ValueError, TypeError):
            pass

    # Quincenas registradas como pagadas dentro del rango
    pagadas = {
        r.quincena_num for r in db.query(QuincenaPendiente).filter(
            QuincenaPendiente.member_id == member_id,
            QuincenaPendiente.caja_id == caja_id,
            QuincenaPendiente.quincena_num <= hasta_q,
        ).all()
    }

    for qnum in range(1, hasta_q + 1):
        if qnum not in pagadas:
            continue
        if qnum in existing:
            if existing[qnum].monto != cuota:
                existing[qnum].monto = cuota
        else:
            db.add(Aportacion(
                member_id=member_id,
                caja_id=caja_id,
                monto=cuota,
                quincena=f"Q{qnum}",
                notas="_cuota_",
                fecha_aportacion=date.today(),
            ))

router = APIRouter(prefix="/quincenas", tags=["Quincenas"])


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_current_quincena(start_date: date, total_quincenas: int) -> int:
    """
    Calcula la quincena actual basada en meses calendario (2 quincenas por mes).
    Q1: días 1-15, Q2: días 16-fin de mes.
    """
    today = date.today()
    if today < start_date:
        return 0
    
    # Diferencia en meses
    months_passed = (today.year - start_date.year) * 12 + (today.month - start_date.month)
    
    # Quincena de inicio (1 si es día 1-15, 2 si es 16+)
    start_q_in_month = 1 if start_date.day <= 15 else 2
    # Quincena actual
    current_q_in_month = 1 if today.day <= 15 else 2
    
    # Total de quincenas transcurridas
    q = (months_passed * 2) + (current_q_in_month - start_q_in_month) + 1
    
    return min(max(q, 1), total_quincenas)


def _quincena_start(start_date: date, num: int) -> date:
    """Retorna la fecha aproximada de inicio de la quincena N."""
    # Quincena de inicio en su mes (1 o 2)
    start_q_in_month = 1 if start_date.day <= 15 else 2
    
    # Cuántos pasos de quincena dar
    total_q_steps = (num - 1) + (start_q_in_month - 1)
    
    months_to_add = total_q_steps // 2
    is_second_half = (total_q_steps % 2) == 1
    
    # Calcular año y mes
    new_month = start_date.month + months_to_add
    new_year = start_date.year + (new_month - 1) // 12
    new_month = (new_month - 1) % 12 + 1
    
    if num == 1:
        return start_date # La primera quincena empieza el día que dice la caja
    
    day = 16 if is_second_half else 1
    return date(new_year, new_month, day)


def _quincena_end(start_date: date, num: int) -> date:
    """Retorna la fecha aproximada de fin de la quincena N."""
    start = _quincena_start(start_date, num)
    if start.day <= 15:
        return start.replace(day=15)
    else:
        # Último día del mes
        if start.month == 12:
            return start.replace(year=start.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            return start.replace(month=start.month + 1, day=1) - timedelta(days=1)


# ── Schemas ────────────────────────────────────────────────────────────────────

class ToggleRequest(BaseModel):
    member_id: int
    caja_id: int
    quincena_num: int


class AlCorrienteBulkRequest(BaseModel):
    caja_id: int
    quincena_hasta: int
    exclude_member_ids: list[int] = []
    cuota_default: float = 0.0  # Se aplica a socios sin quota_personal configurado


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/data")
def get_quincenas_data(caja_id: int = Query(...), db: Session = Depends(get_db)):
    """
    Retorna todos los datos para renderizar el panel de quincenas:
    Lógica: si una quincena TIENE registro en quincenas_pendientes (ahora quincenas_pagadas) → pagada (verde).
    Si NO tiene → pendiente (amarillo).
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

    # Obtener todas las quincenas marcadas como PAGADAS (usando la misma tabla)
    pagadas_rows = (
        db.query(QuincenaPendiente)
        .filter(QuincenaPendiente.caja_id == caja_id)
        .all()
    )
    pagadas_set = {(r.member_id, r.quincena_num) for r in pagadas_rows}

    cuota_global = float(caja.quota_amount)
    members_data = [
        {
            "id":   m.id,
            "name": m.name,
            "member_type": m.member_type,
            "quota_personal": float(m.quota_personal) if float(m.quota_personal) > 0 else cuota_global,
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

    # Capital ideal vs real — usando quota_personal por socio
    n_socios_dentro = sum(1 for m in members if m.member_type == "dentro")
    cuota_global = float(caja.quota_amount)

    # Mapa member_id → cuota efectiva (personal si > 0, si no la global)
    quota_map = {
        m.id: (float(m.quota_personal) if float(m.quota_personal) > 0 else cuota_global)
        for m in members if m.member_type == "dentro"
    }

    capital_ideal = sum(quota_map.values()) * current_q

    # Capital real: suma de cuotas de quincenas pagadas (incluye anticipaciones)
    capital_real = sum(
        quota_map[mid]
        for (mid, qnum) in pagadas_set
        if mid in quota_map
    )

    # Pendientes: solo quincenas vencidas (hasta current_q) que no están pagadas
    pagadas_vencidas_count = sum(
        1 for (mid, qnum) in pagadas_set
        if qnum <= current_q and mid in quota_map
    )
    pendientes_count = (len(quota_map) * current_q) - pagadas_vencidas_count

    return {
        "caja": {
            "id":              caja.id,
            "name":            caja.name,
            "start_date":      str(caja.start_date),
            "quota_amount":    cuota_global,
            "total_quincenas": caja.total_quincenas,
            "quincena_actual": current_q,
        },
        "members":    members_data,
        "quincenas":  quincenas_info,
        "pagadas":    [{"member_id": mid, "quincena_num": qnum} for mid, qnum in pagadas_set],
        "resumen": {
            "n_socios":        len(members),
            "n_socios_dentro": n_socios_dentro,
            "cuota":           cuota_global,
            "capital_ideal":   round(capital_ideal, 2),
            "capital_real":    round(capital_real, 2),
            "pendientes_count": pendientes_count,
        },
    }


@router.post("/toggle")
def toggle_quincena(payload: ToggleRequest, db: Session = Depends(get_db)):
    """
    Alterna el estado de una quincena.
    Ahora: Registro existe = pagado, No existe = pendiente.
    Impacta financieramente creando/borrando una Aportación de tipo 'cuota'.
    """
    caja = db.get(CajaConfig, payload.caja_id)
    if not caja:
        raise HTTPException(status_code=404, detail="Caja no encontrada.")

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

    cuota = member.quota_personal if member.quota_personal > 0 else Decimal(str(caja.quota_amount))

    if existing:
        # Estaba PAGADO -> Cambiar a PENDIENTE
        # 1. Eliminar marca de pago
        db.delete(existing)
        
        # 2. Eliminar aporte financiero si existía
        if member.member_type == "dentro":
            apo = db.query(Aportacion).filter(
                Aportacion.member_id == payload.member_id,
                Aportacion.caja_id == payload.caja_id,
                Aportacion.quincena == f"Q{payload.quincena_num}",
                Aportacion.notas == "_cuota_",
            ).first()
            if apo:
                member.savings_balance -= apo.monto
                db.delete(apo)
        
        db.commit()
        return {"estado": "pendiente", "member_id": payload.member_id, "quincena_num": payload.quincena_num}
    else:
        # Estaba PENDIENTE -> Cambiar a PAGADO
        # 1. Crear marca de pago
        nuevo = QuincenaPendiente(
            member_id=payload.member_id,
            caja_id=payload.caja_id,
            quincena_num=payload.quincena_num,
        )
        db.add(nuevo)
        
        # 2. Crear aporte financiero si es socio interno
        if member.member_type == "dentro":
            db.add(Aportacion(
                member_id=payload.member_id,
                caja_id=payload.caja_id,
                monto=cuota,
                quincena=f"Q{payload.quincena_num}",
                notas="_cuota_",
                fecha_aportacion=date.today(),
            ))
            member.savings_balance += cuota
            
        db.commit()
        return {"estado": "pagado", "member_id": payload.member_id, "quincena_num": payload.quincena_num}


@router.post("/al-corriente-bulk")
def al_corriente_bulk(payload: AlCorrienteBulkRequest, db: Session = Depends(get_db)):
    """
    Marca como PAGADAS todas las quincenas hasta quincena_hasta para todos, excepto excluidos.
    Crea registros de Aportación de tipo 'cuota' para que impacte el capital y ahorros.
    """
    caja = db.get(CajaConfig, payload.caja_id)
    if not caja:
        raise HTTPException(status_code=404, detail="Caja no encontrada.")
    
    current_q = _get_current_quincena(caja.start_date, caja.total_quincenas)
    quincena_hasta = min(payload.quincena_hasta, current_q)
    cuota_global = Decimal(str(caja.quota_amount))

    members = (
        db.query(Member)
        .filter(Member.caja_id == payload.caja_id, Member.is_active == True)
        .all()
    )

    excluded_set = set(payload.exclude_member_ids)
    added_count = 0
    deleted_count = 0
    processed = 0

    for m in members:
        if m.id in excluded_set or m.member_type != "dentro":
            continue

        # Obtener todas las quincenas registradas de este socio de una sola vez
        registradas = db.query(QuincenaPendiente).filter(
            QuincenaPendiente.member_id == m.id,
            QuincenaPendiente.caja_id == payload.caja_id,
        ).all()
        registradas_nums = {r.quincena_num for r in registradas}

        # Si no tiene cuota y se mandó cuota_default, asignarla permanentemente
        if m.quota_personal == 0 and payload.cuota_default > 0:
            m.quota_personal = Decimal(str(payload.cuota_default))
        cuota = m.quota_personal if m.quota_personal > 0 else cuota_global

        # 1. Crear QuincenaPendiente faltantes
        for qnum in range(1, quincena_hasta + 1):
            if qnum not in registradas_nums:
                db.add(QuincenaPendiente(member_id=m.id, caja_id=payload.caja_id, quincena_num=qnum))
                added_count += 1

        # Flush para que _sync_aportaciones vea las quincenas recién creadas
        db.flush()

        # 2. Crear o corregir aportaciones _cuota_ para Q1..quincena_hasta
        if m.member_type == "dentro":
            _sync_aportaciones(m.id, payload.caja_id, cuota, quincena_hasta, db)

        processed += 1

    db.commit()

    # Recalcular savings_balance desde suma real en BD para todos los socios internos
    for m in members:
        if m.id in excluded_set or m.member_type != "dentro":
            continue
        _recalc_savings(m, payload.caja_id, db)
    db.commit()

    return {
        "ok": True,
        "processed": processed,
        "added": added_count,
        "deleted": deleted_count,
        "quincena_hasta": quincena_hasta
    }


@router.post("/al-corriente")
def marcar_al_corriente(
    member_id: int = Query(...),
    caja_id: int = Query(...),
    quincena_hasta: int = Query(None),
    cuota_override: float = Query(default=None),
    db: Session = Depends(get_db),
):
    """Marca las quincenas del socio como PAGADAS hasta quincena_hasta.
    Si se pasa cuota_override > 0, actualiza quota_personal del socio antes de procesar."""
    caja = db.get(CajaConfig, caja_id)
    if not caja:
        raise HTTPException(status_code=404, detail="Caja no encontrada.")

    member = db.get(Member, member_id)
    if not member or member.caja_id != caja_id:
        raise HTTPException(status_code=404, detail="Socio no encontrado.")

    # Si el frontend manda la cuota, guardarla en el socio
    if cuota_override is not None and cuota_override > 0:
        member.quota_personal = Decimal(str(cuota_override))

    current_q = _get_current_quincena(caja.start_date, caja.total_quincenas)
    # Permite anticipar quincenas futuras hasta el máximo del ciclo
    target_q = min(quincena_hasta, caja.total_quincenas) if quincena_hasta else current_q
    added = 0
    cuota = member.quota_personal if member.quota_personal > 0 else Decimal(str(caja.quota_amount))
    
    # Obtener quincenas ya registradas de una sola vez
    registradas_nums = {
        r.quincena_num for r in db.query(QuincenaPendiente).filter(
            QuincenaPendiente.member_id == member_id,
            QuincenaPendiente.caja_id == caja_id,
        ).all()
    }

    # 1. Crear QuincenaPendiente faltantes
    for qnum in range(1, target_q + 1):
        if qnum not in registradas_nums:
            db.add(QuincenaPendiente(member_id=member_id, caja_id=caja_id, quincena_num=qnum))
            added += 1

    # Flush para que _sync_aportaciones vea las quincenas recién creadas
    db.flush()

    # 2. Crear o corregir aportaciones _cuota_ para todas las quincenas Q1..target_q
    if member.member_type == "dentro":
        _sync_aportaciones(member_id, caja_id, cuota, target_q, db)

    # Revertir quincenas por encima de target_q
    removed = 0
    for qnum in [n for n in registradas_nums if n > target_q]:
        row = db.query(QuincenaPendiente).filter(
            QuincenaPendiente.member_id == member_id,
            QuincenaPendiente.caja_id == caja_id,
            QuincenaPendiente.quincena_num == qnum,
        ).first()
        if row:
            if member.member_type == "dentro":
                apo = db.query(Aportacion).filter(
                    Aportacion.member_id == member_id,
                    Aportacion.caja_id == caja_id,
                    Aportacion.quincena == f"Q{qnum}",
                    Aportacion.notas == "_cuota_",
                ).first()
                if apo:
                    db.delete(apo)
            db.delete(row)
            removed += 1

    db.commit()

    # Recalcular savings_balance desde suma real en BD
    if member.member_type == "dentro":
        _recalc_savings(member, caja_id, db)
        db.commit()

    return {
        "ok": True,
        "added": added,
        "removed": removed,
        "quincena_hasta": target_q,
        "quincena_actual": current_q,
        "member_name": member.name,
        "savings_balance": float(member.savings_balance),
        "quota_personal": float(member.quota_personal),
    }


@router.post("/marcar-todos-pagados")
def marcar_todos_pagados(caja_id: int = Query(...), quincena_num: int = Query(...), db: Session = Depends(get_db)):
    """Marca la quincena específica como PAGADA para todos los socios."""
    caja = db.get(CajaConfig, caja_id)
    if not caja:
        raise HTTPException(status_code=404, detail="Caja no encontrada.")
    
    members = db.query(Member).filter(Member.caja_id == caja_id, Member.is_active == True).all()
    added = 0
    for m in members:
        exists = db.query(QuincenaPendiente).filter(
            QuincenaPendiente.member_id == m.id,
            QuincenaPendiente.caja_id == caja_id,
            QuincenaPendiente.quincena_num == quincena_num
        ).first()
        if not exists:
            db.add(QuincenaPendiente(member_id=m.id, caja_id=caja_id, quincena_num=quincena_num))
            added += 1
    db.commit()
    return {"ok": True, "quincena_num": quincena_num, "added": added}
