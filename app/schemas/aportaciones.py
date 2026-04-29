from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class AportacionCreate(BaseModel):
    member_id: int
    caja_id: int
    monto: Decimal = Field(..., gt=0)
    fecha_aportacion: date
    quincena: Optional[str] = Field(None, max_length=20, description="Ej: '2026-04-Q1'")
    notas: Optional[str] = Field(None, max_length=500)


class AportacionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    member_id: int
    caja_id: int
    monto: Decimal
    fecha_aportacion: date
    quincena: Optional[str] = None
    notas: Optional[str] = None
    created_at: datetime
    member_name: str = ""


class CapitalDisponibleResponse(BaseModel):
    caja_id: int
    capital_aportado_total: Decimal
    capital_en_prestamos: Decimal
    intereses_cobrados: Decimal
    capital_disponible: Decimal
    rendimiento_pct: Decimal
    n_aportaciones: int
    n_prestamos_activos: int
