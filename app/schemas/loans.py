from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class LoanCreate(BaseModel):
    member_id: int
    caja_id: int
    initial_amount: Decimal = Field(..., gt=0)
    interest_rate: Optional[Decimal] = Field(None, ge=0, le=1)
    loan_type: str = Field(..., pattern="^(internal|external)$")
    start_date: date


class LoanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    member_id: int
    caja_id: int
    initial_amount: Decimal
    outstanding_balance: Decimal
    interest_rate: Decimal
    loan_type: str
    status: str
    start_date: date
    created_at: datetime
    member_name: str = ""


class AbonoCapitalRequest(BaseModel):
    """Declaración de abono directo al capital con fecha personalizada."""
    monto: Decimal = Field(..., gt=0, description="Monto a abonar al capital")
    fecha_abono: date = Field(..., description="Fecha efectiva del abono (puede ser pasada)")
    notas: Optional[str] = Field(None, max_length=500)


class AbonoCapitalResponse(BaseModel):
    loan_id: int
    capital_abonado: Decimal
    nuevo_saldo_insoluto: Decimal
    prestamo_liquidado: bool
    fecha_abono: date


class InteresAcumuladoResponse(BaseModel):
    loan_id: int
    fecha_corte: date
    interes_acumulado: Decimal
    saldo_insoluto_actual: Decimal
    periodos: list[dict] = []


class TransaccionResumen(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    amount: Decimal
    transaction_date: date
    transaction_type: str
    notes: Optional[str] = None


class LoanDetailRead(BaseModel):
    """Detalle completo de un préstamo con historial de transacciones."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    member_id: int
    member_name: str = ""
    caja_id: int
    initial_amount: Decimal
    outstanding_balance: Decimal
    interest_rate: Decimal
    loan_type: str
    status: str
    start_date: date
    created_at: datetime
    historial: list[TransaccionResumen] = []
    interes_acumulado_hoy: Decimal = Decimal("0")
    interes_mes_actual: Decimal = Decimal("0")


# --- Regularización bulk de intereses ---

class RegularizarInteresesBulkRequest(BaseModel):
    caja_id: int
    exclude_loan_ids: list[int] = []
    notas: Optional[str] = Field(None, max_length=500)


class RegularizarLoanDetalle(BaseModel):
    loan_id: int
    member_name: str
    outstanding_balance: Decimal
    interes_pagado: Decimal
    fecha_pago: date
    dia_cobro: int


class RegularizarLoanNoVencido(BaseModel):
    loan_id: int
    member_name: str
    outstanding_balance: Decimal
    interes_mensual: Decimal
    dia_cobro: int
    fecha_vencimiento: date


class RegularizarInteresesBulkResponse(BaseModel):
    total_prestamos: int
    total_intereses: Decimal
    detalles: list[RegularizarLoanDetalle] = []
    no_vencidos: list[RegularizarLoanNoVencido] = []


# --- Miembros con préstamos (para panel "Excepto a") ---

class LoanResumen(BaseModel):
    loan_id: int
    initial_amount: Decimal
    outstanding_balance: Decimal
    interest_rate: Decimal
    interes_mensual: Decimal
    start_date: date
    loan_type: str
    dias_sin_pago_interes: int


class MiembroConPrestamos(BaseModel):
    member_id: int
    member_name: str
    group_name: str = ""
    loans: list[LoanResumen] = []


# --- Préstamos liquidados + interés extra ---

class LoanLiquidadoRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    member_id: int
    member_name: str = ""
    group_name: str = ""
    initial_amount: Decimal
    interest_rate: Decimal
    loan_type: str
    start_date: date
    total_intereses_pagados: Decimal = Decimal("0")
    total_capital_pagado: Decimal = Decimal("0")
    fecha_liquidacion: Optional[date] = None


class InteresExtraRequest(BaseModel):
    monto: Decimal = Field(..., gt=0)
    fecha_pago: date
    motivo: str = Field(..., min_length=5, max_length=500)


# --- Detector de intereses duplicados ---

class TransaccionSospechosaRead(BaseModel):
    id: int
    fecha: date
    monto: Decimal
    notas: Optional[str] = None


class GrupoInteresDuplicadoRead(BaseModel):
    loan_id: int
    member_id: int
    member_name: str
    loan_type: str
    transacciones: list[TransaccionSospechosaRead]
    dias_entre_pagos: list[int]
    total_monto: Decimal
    sugerido_conservar_id: int


# --- Timeline de evolución del préstamo ---

class EventoTimelineRead(BaseModel):
    fecha: date
    tipo: str
    monto: Decimal
    dias_desde_anterior: int
    interes_generado_periodo: Decimal
    saldo_antes: Decimal
    saldo_despues: Decimal
    notas: Optional[str] = None
    transaction_id: Optional[int] = None


class LoanTimelineResponse(BaseModel):
    loan_id: int
    eventos: list[EventoTimelineRead]
    interes_generado_total: Decimal
    interes_pagado_total: Decimal
    interes_pendiente: Decimal
    saldo_actual: Decimal


# --- Préstamos agrupados por socio (activos + liquidados, orden cronológico) ---

class LoanEnGrupoRead(BaseModel):
    loan_id: int
    initial_amount: Decimal
    outstanding_balance: Decimal
    interest_rate: Decimal
    loan_type: str
    status: str
    start_date: date
    interes_mensual: Decimal
    interest_paid_month: bool = False
    total_intereses_pagados: Decimal = Decimal("0")
    fecha_liquidacion: Optional[date] = None


class MiembroAgrupadoRead(BaseModel):
    member_id: int
    member_name: str
    member_type: str
    group_name: str = ""
    first_loan_date: date
    n_activos: int
    n_liquidados: int
    total_initial: Decimal
    total_balance: Decimal
    total_interest_mensual: Decimal
    loans: list[LoanEnGrupoRead]
