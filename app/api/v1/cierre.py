"""
Cierre API — análisis de caja, simulador de préstamo y cierre con reparto.

Todos los endpoints que modifican el borrador devuelven el resumen recalculado,
para que la pantalla siempre muestre exactamente lo que se exportará y cerrará.
"""
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services import cierre as svc
from app.services.savings import reconcile_caja

router = APIRouter(prefix="/cierre", tags=["Cierre de caja"])


def _http(exc: svc.CierreError) -> HTTPException:
    detail = {"message": exc.message, **exc.extra} if exc.extra else exc.message
    return HTTPException(status_code=exc.status, detail=detail)


@router.get("/resumen")
def get_resumen(caja_id: int = Query(...), db: Session = Depends(get_db)):
    """Análisis de la caja al corte + vista previa del cierre y del reparto. Solo lectura."""
    try:
        return svc.build_resumen(db, caja_id)
    except svc.CierreError as e:
        raise _http(e)


class BorradorUpdate(BaseModel):
    caja_id: int
    fecha_corte: Optional[date] = None
    usar_hoy: bool = False
    factor_sin_prestamo: Optional[Decimal] = Field(None, ge=0, le=1)
    incluir_interes: Optional[bool] = None
    # {member_id: porcentaje | null}; null quita el ajuste manual de ese socio
    overrides: Optional[dict[int, Optional[Decimal]]] = None
    limpiar_overrides: bool = False


@router.put("/borrador")
def put_borrador(payload: BorradorUpdate, db: Session = Depends(get_db)):
    """Actualiza fecha de corte, factor, interés pendiente o porcentajes manuales."""
    try:
        svc.update_borrador(
            db, payload.caja_id,
            fecha_corte=payload.fecha_corte,
            usar_hoy=payload.usar_hoy,
            factor_sin_prestamo=payload.factor_sin_prestamo,
            incluir_interes=payload.incluir_interes,
            overrides=payload.overrides,
            limpiar_overrides=payload.limpiar_overrides,
        )
        db.commit()
        return svc.build_resumen(db, payload.caja_id)
    except svc.CierreError as e:
        db.rollback()
        raise _http(e)


class InstruccionUpdate(BaseModel):
    caja_id: int
    member_ids: list[int] = Field(..., min_length=1)
    instruccion: str = Field(..., pattern="^(ahorro|separado|por_definir)$")


@router.put("/instruccion")
def put_instruccion(payload: InstruccionUpdate, db: Session = Depends(get_db)):
    """Define cómo paga su deuda uno o varios socios: con ahorro, por separado o por definir."""
    try:
        svc.set_instruccion(db, payload.caja_id, payload.member_ids, payload.instruccion)
        db.commit()
        return svc.build_resumen(db, payload.caja_id)
    except svc.CierreError as e:
        db.rollback()
        raise _http(e)


class SimularRequest(BaseModel):
    caja_id: int
    member_id: int
    monto: Decimal = Field(..., gt=0)


@router.post("/simular")
def simular(payload: SimularRequest, db: Session = Depends(get_db)):
    """«¿Puedo prestar $X?» — no escribe nada."""
    try:
        return svc._jsonable(svc.simulate_loan(db, payload.caja_id, payload.member_id, payload.monto))
    except svc.CierreError as e:
        raise _http(e)


@router.post("/reconciliar-ahorros")
def reconciliar_ahorros(caja_id: int = Query(...), db: Session = Depends(get_db)):
    """Corrige los saldos de ahorro guardados que no cuadran con sus movimientos."""
    corregidos = reconcile_caja(db, caja_id)
    db.commit()
    return {"corregidos": svc._jsonable(corregidos)}


class ConfirmarRequest(BaseModel):
    caja_id: int
    idempotency_key: str = Field(..., min_length=8, max_length=64)
    expected_hash: str = Field(..., min_length=64, max_length=64)


@router.post("/confirmar")
def confirmar(payload: ConfirmarRequest, db: Session = Depends(get_db)):
    """
    Confirma el cierre de forma atómica. Reintentar con la misma
    `idempotency_key` devuelve el cierre guardado sin duplicar movimientos.
    """
    try:
        resumen, creado = svc.confirm_cierre(
            db, payload.caja_id, payload.idempotency_key, payload.expected_hash
        )
    except svc.CierreError as e:
        db.rollback()
        raise _http(e)
    return {"creado": creado, "resumen": resumen}
