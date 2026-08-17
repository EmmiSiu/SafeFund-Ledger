import logging
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from sqlalchemy import func

from app.core.database import get_db
from app.core.money import CENT
from app.models.base import CajaConfig, Loan, Member, MemberGroup, Transaction
import calendar

from app.schemas.loans import (
    AbonoCapitalRequest,
    AbonoCapitalResponse,
    GrupoInteresDuplicadoRead,
    InteresAcumuladoResponse,
    InteresExtraRequest,
    LoanCreate,
    LoanDetailRead,
    LoanLiquidadoRead,
    LoanEnGrupoRead,
    LoanRead,
    LoanResumen,
    LoanTimelineResponse,
    MiembroAgrupadoRead,
    MiembroConPrestamos,
    RegularizarInteresesBulkRequest,
    RegularizarInteresesBulkResponse,
    RegularizarLoanDetalle,
    RegularizarLoanNoVencido,
    TransaccionResumen,
)
from app.services.loan_engine import (
    build_loan_timeline,
    calculate_accrued_interest,
    find_duplicate_interest_payments,
)

router = APIRouter(prefix="/loans", tags=["Loans"])
logger = logging.getLogger(__name__)


@router.get("/", response_model=list[LoanRead])
def list_loans(
    caja_id: int = Query(...),
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    q = (
        db.query(Loan)
        .options(joinedload(Loan.member))
        .filter(Loan.caja_id == caja_id)
    )
    if status:
        q = q.filter(Loan.status == status)
    loans = q.order_by(Loan.id).all()

    result = []
    for loan in loans:
        data = LoanRead.model_validate(loan)
        data.member_name = loan.member.name if loan.member else ""
        result.append(data)
    return result


@router.get("/metrics")
def get_loan_metrics(caja_id: int = Query(...), db: Session = Depends(get_db)):
    """Métricas globales de la cartera de préstamos para el dashboard."""
    today = date.today()
    start_of_month = today.replace(day=1)

    active_loans = (
        db.query(Loan)
        .filter(Loan.caja_id == caja_id, Loan.status == "active")
        .all()
    )
    n_activos = len(active_loans)
    n_liquidados = (
        db.query(func.count(Loan.id))
        .filter(Loan.caja_id == caja_id, Loan.status == "paid")
        .scalar() or 0
    )

    total_prestado = sum(Decimal(str(l.initial_amount)) for l in active_loans)
    saldo_insoluto = sum(Decimal(str(l.outstanding_balance)) for l in active_loans)

    ganancias_raw = (
        db.query(func.sum(Transaction.amount))
        .join(Loan, Transaction.loan_id == Loan.id)
        .filter(
            Loan.caja_id == caja_id,
            Transaction.transaction_type == "interest_payment",
        )
        .scalar() or Decimal("0")
    )
    ganancias_mes_raw = (
        db.query(func.sum(Transaction.amount))
        .join(Loan, Transaction.loan_id == Loan.id)
        .filter(
            Loan.caja_id == caja_id,
            Transaction.transaction_type == "interest_payment",
            Transaction.transaction_date >= start_of_month,
        )
        .scalar() or Decimal("0")
    )

    capital_interno = sum(
        Decimal(str(l.outstanding_balance)) for l in active_loans if l.loan_type == "internal"
    )
    capital_externo = sum(
        Decimal(str(l.outstanding_balance)) for l in active_loans if l.loan_type == "external"
    )
    total_cartera = capital_interno + capital_externo
    pct_interno = float(
        (capital_interno / total_cartera * 100).quantize(CENT, rounding=ROUND_HALF_UP)
    ) if total_cartera > 0 else 0.0
    pct_externo = float(
        (capital_externo / total_cartera * 100).quantize(CENT, rounding=ROUND_HALF_UP)
    ) if total_cartera > 0 else 0.0

    # Ranking socios por intereses generados (all loans, not just active)
    member_interest_rows = (
        db.query(Transaction.member_id, func.sum(Transaction.amount).label("total"))
        .join(Loan, Transaction.loan_id == Loan.id)
        .filter(
            Loan.caja_id == caja_id,
            Transaction.transaction_type == "interest_payment",
        )
        .group_by(Transaction.member_id)
        .order_by(func.sum(Transaction.amount).desc())
        .limit(10)
        .all()
    )
    ranking = []
    for row in member_interest_rows:
        m = db.get(Member, row.member_id)
        if not m:
            continue
        m_active = [l for l in active_loans if l.member_id == row.member_id]
        ranking.append({
            "member_id": row.member_id,
            "member_name": m.name,
            "total_intereses": float(Decimal(str(row.total)).quantize(CENT, rounding=ROUND_HALF_UP)),
            "outstanding_balance": float(
                sum(Decimal(str(l.outstanding_balance)) for l in m_active).quantize(CENT, rounding=ROUND_HALF_UP)
            ),
            "n_loans_activos": len(m_active),
        })

    return {
        "total_prestado":      float(total_prestado.quantize(CENT, rounding=ROUND_HALF_UP)),
        "saldo_insoluto_total": float(saldo_insoluto.quantize(CENT, rounding=ROUND_HALF_UP)),
        "ganancias_acumuladas": float(Decimal(str(ganancias_raw)).quantize(CENT, rounding=ROUND_HALF_UP)),
        "ganancias_mes":        float(Decimal(str(ganancias_mes_raw)).quantize(CENT, rounding=ROUND_HALF_UP)),
        "n_activos":    n_activos,
        "n_liquidados": n_liquidados,
        "capital_interno": float(capital_interno.quantize(CENT, rounding=ROUND_HALF_UP)),
        "capital_externo": float(capital_externo.quantize(CENT, rounding=ROUND_HALF_UP)),
        "pct_interno": pct_interno,
        "pct_externo": pct_externo,
        "ranking_socios": ranking,
    }


# ────────────────────────────────────────────────────────
# Regularización bulk de intereses
# ────────────────────────────────────────────────────────

def _fecha_cobro_mes_actual(dia_cobro: int, today: date) -> date:
    """Calcula la fecha de cobro del mes actual, ajustando si el día no existe en el mes."""
    last_day = calendar.monthrange(today.year, today.month)[1]
    dia = min(dia_cobro, last_day)
    return date(today.year, today.month, dia)


@router.post("/regularizar-intereses-bulk", response_model=RegularizarInteresesBulkResponse)
def regularizar_intereses_bulk(
    payload: RegularizarInteresesBulkRequest,
    db: Session = Depends(get_db),
):
    """
    Paga el interés mensual de todos los préstamos activos de una caja.
    Cada préstamo usa su propio día de cobro (start_date.day):
    - Si la fecha de cobro del mes actual <= hoy: se regulariza con esa fecha
    - Si la fecha de cobro del mes actual > hoy: se salta (aún no vence)
    """
    today = date.today()

    loans = (
        db.query(Loan)
        .options(joinedload(Loan.member))
        .filter(
            Loan.caja_id == payload.caja_id,
            Loan.status == "active",
            Loan.outstanding_balance > 0,
        )
        .all()
    )

    if payload.exclude_loan_ids:
        loans = [l for l in loans if l.id not in payload.exclude_loan_ids]

    if not loans:
        return RegularizarInteresesBulkResponse(
            total_prestamos=0,
            total_intereses=Decimal("0"),
            detalles=[],
            no_vencidos=[],
        )

    detalles: list[RegularizarLoanDetalle] = []
    no_vencidos: list[RegularizarLoanNoVencido] = []
    total_intereses = Decimal("0")

    try:
        for loan in loans:
            balance = Decimal(str(loan.outstanding_balance))
            rate = Decimal(str(loan.interest_rate))
            interes = (balance * rate).quantize(CENT, rounding=ROUND_HALF_UP)

            if interes <= 0:
                continue

            dia_cobro = loan.start_date.day
            fecha_cobro = _fecha_cobro_mes_actual(dia_cobro, today)

            # Si la fecha de cobro aún no llega, no regularizar
            if fecha_cobro > today:
                no_vencidos.append(RegularizarLoanNoVencido(
                    loan_id=loan.id,
                    member_name=loan.member.name if loan.member else "",
                    outstanding_balance=balance,
                    interes_mensual=interes,
                    dia_cobro=dia_cobro,
                    fecha_vencimiento=fecha_cobro,
                ))
                continue

            txn = Transaction(
                loan_id=loan.id,
                member_id=loan.member_id,
                amount=interes,
                transaction_date=fecha_cobro,
                transaction_type="interest_payment",
                notes=payload.notas or "Regularización bulk de intereses",
            )
            db.add(txn)
            total_intereses += interes

            detalles.append(RegularizarLoanDetalle(
                loan_id=loan.id,
                member_name=loan.member.name if loan.member else "",
                outstanding_balance=balance,
                interes_pagado=interes,
                fecha_pago=fecha_cobro,
                dia_cobro=dia_cobro,
            ))

        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al regularizar: {exc}")

    return RegularizarInteresesBulkResponse(
        total_prestamos=len(detalles),
        total_intereses=total_intereses.quantize(CENT, rounding=ROUND_HALF_UP),
        detalles=detalles,
        no_vencidos=no_vencidos,
    )


# ────────────────────────────────────────────────────────
# Miembros con préstamos activos (para panel "Excepto a")
# ────────────────────────────────────────────────────────

@router.get("/miembros-con-prestamos", response_model=list[MiembroConPrestamos])
def miembros_con_prestamos(
    caja_id: int = Query(...),
    search: str = Query("", min_length=0),
    db: Session = Depends(get_db),
):
    """
    Lista miembros que tienen préstamos activos, con detalle de cada préstamo.
    Usado para el panel 'Excepto a' en la regularización bulk.
    """
    q = (
        db.query(Member)
        .options(
            joinedload(Member.loans),
            joinedload(Member.member_group),
        )
        .filter(Member.caja_id == caja_id, Member.is_active == True)
    )

    if search and len(search) >= 2:
        q = q.filter(Member.name.ilike(f"%{search}%"))

    members = q.order_by(Member.name).limit(30).all()
    today = date.today()

    result: list[MiembroConPrestamos] = []
    for m in members:
        active_loans = [l for l in m.loans if l.status == "active" and l.outstanding_balance > 0]
        if not active_loans:
            continue

        loan_items: list[LoanResumen] = []
        for loan in active_loans:
            balance = Decimal(str(loan.outstanding_balance))
            rate = Decimal(str(loan.interest_rate))
            interes_mensual = (balance * rate).quantize(CENT, rounding=ROUND_HALF_UP)

            # Días desde último pago de interés
            last_interest = (
                db.query(func.max(Transaction.transaction_date))
                .filter(
                    Transaction.loan_id == loan.id,
                    Transaction.transaction_type == "interest_payment",
                )
                .scalar()
            )
            if last_interest:
                dias_sin_pago = (today - last_interest).days
            else:
                dias_sin_pago = (today - loan.start_date).days

            loan_items.append(LoanResumen(
                loan_id=loan.id,
                initial_amount=Decimal(str(loan.initial_amount)),
                outstanding_balance=balance,
                interest_rate=rate,
                interes_mensual=interes_mensual,
                start_date=loan.start_date,
                loan_type=loan.loan_type,
                dias_sin_pago_interes=max(0, dias_sin_pago),
            ))

        result.append(MiembroConPrestamos(
            member_id=m.id,
            member_name=m.name,
            group_name=m.member_group.name if m.member_group else "",
            loans=loan_items,
        ))

    return result


# ────────────────────────────────────────────────────────
# Préstamos agrupados por socio (activos + liquidados, cronológico)
# ────────────────────────────────────────────────────────

@router.get("/agrupado-por-socio", response_model=list[MiembroAgrupadoRead])
def get_loans_agrupado_por_socio(caja_id: int = Query(...), db: Session = Depends(get_db)):
    """
    Agrupa todos los préstamos (activos + liquidados) por socio, en orden
    cronológico dentro de cada socio, y ordena los socios por la fecha de su
    primer préstamo (el más antiguo primero). Fuente única para la vista
    "Por Socio" en /loans — incluye histórico para que un préstamo liquidado
    no desaparezca de la vista al liquidarse.
    """
    today = date.today()
    start_of_month = today.replace(day=1)

    loans = (
        db.query(Loan)
        .options(joinedload(Loan.member).joinedload(Member.member_group))
        .filter(Loan.caja_id == caja_id, Loan.status.in_(["active", "paid"]))
        .all()
    )
    if not loans:
        return []

    loan_ids = [l.id for l in loans]

    last_interest_map = dict(
        db.query(Transaction.loan_id, func.max(Transaction.transaction_date))
        .filter(Transaction.loan_id.in_(loan_ids), Transaction.transaction_type == "interest_payment")
        .group_by(Transaction.loan_id)
        .all()
    )
    paid_interest_map = dict(
        db.query(Transaction.loan_id, func.sum(Transaction.amount))
        .filter(Transaction.loan_id.in_(loan_ids), Transaction.transaction_type == "interest_payment")
        .group_by(Transaction.loan_id)
        .all()
    )
    fecha_liq_map = dict(
        db.query(Transaction.loan_id, func.max(Transaction.transaction_date))
        .filter(Transaction.loan_id.in_(loan_ids), Transaction.transaction_type == "capital_payment")
        .group_by(Transaction.loan_id)
        .all()
    )

    groups: dict[int, dict] = {}
    for loan in loans:
        member = loan.member
        if not member:
            continue

        balance = Decimal(str(loan.outstanding_balance))
        rate = Decimal(str(loan.interest_rate))
        is_active = loan.status == "active"
        interes_mensual = (
            (balance * rate).quantize(CENT, rounding=ROUND_HALF_UP) if is_active else Decimal("0.00")
        )
        last_interest = last_interest_map.get(loan.id)

        loan_item = LoanEnGrupoRead(
            loan_id=loan.id,
            initial_amount=Decimal(str(loan.initial_amount)),
            outstanding_balance=balance,
            interest_rate=rate,
            loan_type=loan.loan_type,
            status=loan.status,
            start_date=loan.start_date,
            interes_mensual=interes_mensual,
            interest_paid_month=bool(is_active and last_interest and last_interest >= start_of_month),
            total_intereses_pagados=Decimal(str(paid_interest_map.get(loan.id, 0))).quantize(CENT, rounding=ROUND_HALF_UP),
            fecha_liquidacion=fecha_liq_map.get(loan.id) if not is_active else None,
        )

        if member.id not in groups:
            groups[member.id] = {
                "member_id": member.id,
                "member_name": member.name,
                "member_type": member.member_type,
                "group_name": member.member_group.name if member.member_group else "",
                "loans": [],
            }
        groups[member.id]["loans"].append(loan_item)

    result: list[MiembroAgrupadoRead] = []
    for g in groups.values():
        g["loans"].sort(key=lambda l: l.start_date)
        activos = [l for l in g["loans"] if l.status == "active"]
        liquidados = [l for l in g["loans"] if l.status == "paid"]
        result.append(MiembroAgrupadoRead(
            member_id=g["member_id"],
            member_name=g["member_name"],
            member_type=g["member_type"],
            group_name=g["group_name"],
            first_loan_date=g["loans"][0].start_date,
            n_activos=len(activos),
            n_liquidados=len(liquidados),
            total_initial=sum((l.initial_amount for l in g["loans"]), Decimal("0")).quantize(CENT, rounding=ROUND_HALF_UP),
            total_balance=sum((l.outstanding_balance for l in activos), Decimal("0")).quantize(CENT, rounding=ROUND_HALF_UP),
            total_interest_mensual=sum((l.interes_mensual for l in activos), Decimal("0")).quantize(CENT, rounding=ROUND_HALF_UP),
            loans=g["loans"],
        ))

    result.sort(key=lambda g: g.first_loan_date)
    return result


# ────────────────────────────────────────────────────────
# Préstamos liquidados
# ────────────────────────────────────────────────────────

@router.get("/liquidados", response_model=list[LoanLiquidadoRead])
def list_liquidados(
    caja_id: int = Query(...),
    db: Session = Depends(get_db),
):
    """Lista préstamos con status='paid', incluyendo totales de interés y capital pagado."""
    loans = (
        db.query(Loan)
        .options(joinedload(Loan.member).joinedload(Member.member_group))
        .filter(Loan.caja_id == caja_id, Loan.status == "paid")
        .order_by(Loan.id.desc())
        .all()
    )

    result: list[LoanLiquidadoRead] = []
    for loan in loans:
        total_int = (
            db.query(func.sum(Transaction.amount))
            .filter(
                Transaction.loan_id == loan.id,
                Transaction.transaction_type == "interest_payment",
            )
            .scalar() or Decimal("0")
        )
        total_cap = (
            db.query(func.sum(Transaction.amount))
            .filter(
                Transaction.loan_id == loan.id,
                Transaction.transaction_type == "capital_payment",
            )
            .scalar() or Decimal("0")
        )
        fecha_liq = (
            db.query(func.max(Transaction.transaction_date))
            .filter(
                Transaction.loan_id == loan.id,
                Transaction.transaction_type == "capital_payment",
            )
            .scalar()
        )

        result.append(LoanLiquidadoRead(
            id=loan.id,
            member_id=loan.member_id,
            member_name=loan.member.name if loan.member else "",
            group_name=loan.member.member_group.name if loan.member and loan.member.member_group else "",
            initial_amount=Decimal(str(loan.initial_amount)),
            interest_rate=Decimal(str(loan.interest_rate)),
            loan_type=loan.loan_type,
            start_date=loan.start_date,
            total_intereses_pagados=Decimal(str(total_int)).quantize(CENT, rounding=ROUND_HALF_UP),
            total_capital_pagado=Decimal(str(total_cap)).quantize(CENT, rounding=ROUND_HALF_UP),
            fecha_liquidacion=fecha_liq,
        ))

    return result


# ────────────────────────────────────────────────────────
# Detector de intereses duplicados
# ────────────────────────────────────────────────────────

@router.get("/duplicados-interes", response_model=list[GrupoInteresDuplicadoRead])
def get_duplicados_interes(
    caja_id: int = Query(...),
    db: Session = Depends(get_db),
):
    """
    Detecta pagos de interés sospechosos de estar duplicados (mismo préstamo,
    menos de ~25 días entre pagos). Solo reporta — la purga se hace vía
    DELETE /transactions/{id} sobre las transacciones que el usuario elija.
    """
    grupos = find_duplicate_interest_payments(caja_id, db)
    return [
        GrupoInteresDuplicadoRead(
            loan_id=g.loan_id,
            member_id=g.member_id,
            member_name=g.member_name,
            loan_type=g.loan_type,
            transacciones=[
                {"id": t.id, "fecha": t.fecha, "monto": t.monto, "notas": t.notas}
                for t in g.transacciones
            ],
            dias_entre_pagos=g.dias_entre_pagos,
            total_monto=g.total_monto,
            sugerido_conservar_id=g.sugerido_conservar_id,
        )
        for g in grupos
    ]


# ────────────────────────────────────────────────────────
# Rutas con path parameter {loan_id}
# ────────────────────────────────────────────────────────

@router.get("/{loan_id}", response_model=LoanRead)
def get_loan(loan_id: int, db: Session = Depends(get_db)):
    loan = (
        db.query(Loan)
        .options(joinedload(Loan.member))
        .filter(Loan.id == loan_id)
        .first()
    )
    if not loan:
        raise HTTPException(status_code=404, detail="Préstamo no encontrado.")
    data = LoanRead.model_validate(loan)
    data.member_name = loan.member.name if loan.member else ""
    return data


@router.post("/", response_model=LoanRead, status_code=status.HTTP_201_CREATED)
def create_loan(payload: LoanCreate, db: Session = Depends(get_db)):
    caja = db.get(CajaConfig, payload.caja_id)
    if not caja:
        raise HTTPException(status_code=404, detail="Caja no encontrada.")
    member = db.get(Member, payload.member_id)
    if not member:
        raise HTTPException(status_code=404, detail="Socio no encontrado.")

    rate = payload.interest_rate
    if rate is None:
        rate = (
            caja.interest_rate_internal
            if payload.loan_type == "internal"
            else caja.interest_rate_external
        )

    loan = Loan(
        member_id=payload.member_id,
        caja_id=payload.caja_id,
        initial_amount=payload.initial_amount,
        outstanding_balance=payload.initial_amount,
        interest_rate=rate,
        loan_type=payload.loan_type,
        start_date=payload.start_date,
    )
    db.add(loan)
    db.commit()
    db.refresh(loan)
    data = LoanRead.model_validate(loan)
    data.member_name = member.name
    return data


@router.get("/{loan_id}/detalle", response_model=LoanDetailRead)
def get_loan_detail(loan_id: int, db: Session = Depends(get_db)):
    """Detalle completo: historial de transacciones + interés acumulado a hoy."""
    loan = (
        db.query(Loan)
        .options(joinedload(Loan.member), joinedload(Loan.transactions))
        .filter(Loan.id == loan_id)
        .first()
    )
    if not loan:
        raise HTTPException(status_code=404, detail="Préstamo no encontrado.")

    today = date.today()
    balance = Decimal(str(loan.outstanding_balance))
    rate = Decimal(str(loan.interest_rate))
    interes_mes = (balance * rate).quantize(CENT, rounding=ROUND_HALF_UP)

    try:
        acumulado = calculate_accrued_interest(loan_id, today, db)
        interes_acumulado = acumulado.interes_acumulado
    except Exception:
        logger.exception("Error calculando interés acumulado para el préstamo %s", loan_id)
        interes_acumulado = Decimal("0.00")

    historial = sorted(loan.transactions, key=lambda t: (t.transaction_date, t.id), reverse=True)

    detail = LoanDetailRead(
        id=loan.id,
        member_id=loan.member_id,
        member_name=loan.member.name if loan.member else "",
        caja_id=loan.caja_id,
        initial_amount=Decimal(str(loan.initial_amount)),
        outstanding_balance=balance,
        interest_rate=rate,
        loan_type=loan.loan_type,
        status=loan.status,
        start_date=loan.start_date,
        created_at=loan.created_at,
        historial=[TransaccionResumen.model_validate(t) for t in historial],
        interes_acumulado_hoy=interes_acumulado,
        interes_mes_actual=interes_mes,
    )
    return detail


@router.get("/{loan_id}/interes-acumulado", response_model=InteresAcumuladoResponse)
def get_interes_acumulado(
    loan_id: int,
    fecha_corte: date = Query(default=None, description="Fecha de corte (default: hoy)"),
    db: Session = Depends(get_db),
):
    """Calcula el interés acumulado desde el inicio del préstamo hasta fecha_corte."""
    if fecha_corte is None:
        fecha_corte = date.today()

    loan = db.get(Loan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="Préstamo no encontrado.")

    result = calculate_accrued_interest(loan_id, fecha_corte, db)

    return InteresAcumuladoResponse(
        loan_id=result.loan_id,
        fecha_corte=result.fecha_corte,
        interes_acumulado=result.interes_acumulado,
        saldo_insoluto_actual=result.saldo_insoluto_actual,
        periodos=[
            {
                "fecha_inicio": str(p.fecha_inicio),
                "fecha_fin": str(p.fecha_fin),
                "saldo_insoluto": float(p.saldo_insoluto),
                "dias": p.dias,
                "interes": float(p.interes),
            }
            for p in result.periodos
        ],
    )


@router.get("/{loan_id}/timeline", response_model=LoanTimelineResponse)
def get_loan_timeline(
    loan_id: int,
    fecha_corte: date = Query(default=None, description="Fecha de corte (default: hoy)"),
    db: Session = Depends(get_db),
):
    """
    Línea de tiempo del préstamo: apertura + cada movimiento en orden cronológico,
    con el saldo antes/después y el interés generado desde el evento anterior.
    """
    if fecha_corte is None:
        fecha_corte = date.today()
    try:
        result = build_loan_timeline(loan_id, db, fecha_corte)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return LoanTimelineResponse(
        loan_id=result.loan_id,
        eventos=[
            {
                "fecha": e.fecha,
                "tipo": e.tipo,
                "monto": e.monto,
                "dias_desde_anterior": e.dias_desde_anterior,
                "interes_generado_periodo": e.interes_generado_periodo,
                "saldo_antes": e.saldo_antes,
                "saldo_despues": e.saldo_despues,
                "notas": e.notas,
                "transaction_id": e.transaction_id,
            }
            for e in result.eventos
        ],
        interes_generado_total=result.interes_generado_total,
        interes_pagado_total=result.interes_pagado_total,
        interes_pendiente=result.interes_pendiente,
        saldo_actual=result.saldo_actual,
    )


class IncrementarRequest(BaseModel):
    monto_adicional: Decimal
    notas: str | None = None


@router.post("/{loan_id}/incrementar", response_model=LoanRead)
def incrementar_prestamo(loan_id: int, payload: IncrementarRequest, db: Session = Depends(get_db)):
    """
    Incrementa un préstamo activo sumando monto_adicional al saldo insoluto
    y al monto inicial. Preserva todas las transacciones anteriores.
    El nuevo interés mensual se calcula automáticamente sobre el nuevo saldo.
    """
    loan = db.get(Loan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="Préstamo no encontrado.")
    if loan.status != "active":
        raise HTTPException(status_code=400, detail="Solo se pueden incrementar préstamos activos.")

    adicional = payload.monto_adicional.quantize(CENT, rounding=ROUND_HALF_UP)
    if adicional <= 0:
        raise HTTPException(status_code=400, detail="El monto adicional debe ser mayor a 0.")

    try:
        old_initial = Decimal(str(loan.initial_amount))
        old_balance = Decimal(str(loan.outstanding_balance))

        loan.initial_amount = (old_initial + adicional).quantize(CENT, rounding=ROUND_HALF_UP)
        loan.outstanding_balance = (old_balance + adicional).quantize(CENT, rounding=ROUND_HALF_UP)

        # Registrar como transacción informativa para trazabilidad
        txn = Transaction(
            loan_id=loan.id,
            member_id=loan.member_id,
            amount=adicional,
            transaction_date=date.today(),
            transaction_type="loan_increment",
            notes=payload.notas or f"Incremento de préstamo: +${adicional:,.2f}",
        )
        db.add(txn)
        db.commit()
        db.refresh(loan)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al incrementar: {exc}")

    member = db.get(Member, loan.member_id)
    data = LoanRead.model_validate(loan)
    data.member_name = member.name if member else ""
    return data


class FechaInicioUpdate(BaseModel):
    start_date: date


@router.patch("/{loan_id}/fecha-inicio", response_model=LoanRead)
def update_fecha_inicio(loan_id: int, payload: FechaInicioUpdate, db: Session = Depends(get_db)):
    """Corrige la fecha de inicio de un préstamo (registros retroactivos)."""
    loan = db.get(Loan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="Préstamo no encontrado.")
    loan.start_date = payload.start_date
    db.commit()
    db.refresh(loan)
    data = LoanRead.model_validate(loan)
    m = db.get(Member, loan.member_id)
    data.member_name = m.name if m else ""
    return data


@router.delete("/{loan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_loan(loan_id: int, db: Session = Depends(get_db)):
    """Elimina un préstamo y todas sus transacciones asociadas."""
    loan = db.get(Loan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="Préstamo no encontrado.")
    try:
        db.query(Transaction).filter(Transaction.loan_id == loan_id).delete()
        db.delete(loan)
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al eliminar: {exc}")


@router.post("/{loan_id}/abono-capital", response_model=AbonoCapitalResponse, status_code=status.HTTP_201_CREATED)
def declarar_abono_capital(
    loan_id: int,
    payload: AbonoCapitalRequest,
    db: Session = Depends(get_db),
):
    """
    Declara un abono directo al capital con fecha personalizada (puede ser retroactiva).
    Usa transacción atómica para garantizar consistencia del saldo insoluto.
    """
    loan = db.get(Loan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="Préstamo no encontrado.")
    if loan.status != "active":
        raise HTTPException(status_code=400, detail="El préstamo no está activo.")

    balance = Decimal(str(loan.outstanding_balance))
    monto = payload.monto.quantize(CENT, rounding=ROUND_HALF_UP)

    capital_paid = min(monto, balance).quantize(CENT, rounding=ROUND_HALF_UP)
    new_balance = (balance - capital_paid).quantize(CENT, rounding=ROUND_HALF_UP)
    fully_paid = new_balance == Decimal("0")

    # Transacción atómica
    try:
        txn = Transaction(
            loan_id=loan.id,
            member_id=loan.member_id,
            amount=capital_paid,
            transaction_date=payload.fecha_abono,
            transaction_type="capital_payment",
            notes=payload.notas,
        )
        db.add(txn)
        loan.outstanding_balance = new_balance
        if fully_paid:
            loan.status = "paid"
        db.commit()
        db.refresh(txn)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al registrar abono: {exc}")

    return AbonoCapitalResponse(
        loan_id=loan.id,
        capital_abonado=capital_paid,
        nuevo_saldo_insoluto=new_balance,
        prestamo_liquidado=fully_paid,
        fecha_abono=payload.fecha_abono,
    )


# ────────────────────────────────────────────────────────
# Interés extra sobre préstamos liquidados (o activos)
# ────────────────────────────────────────────────────────

@router.post("/{loan_id}/interes-extra", status_code=status.HTTP_201_CREATED)
def registrar_interes_extra(
    loan_id: int,
    payload: InteresExtraRequest,
    db: Session = Depends(get_db),
):
    """
    Registra un pago de interés adicional sobre un préstamo (liquidado o activo).
    No modifica outstanding_balance ni status. Impacta directamente en métricas de ganancias.
    """
    loan = db.get(Loan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="Préstamo no encontrado.")

    monto = payload.monto.quantize(CENT, rounding=ROUND_HALF_UP)

    try:
        txn = Transaction(
            loan_id=loan.id,
            member_id=loan.member_id,
            amount=monto,
            transaction_date=payload.fecha_pago,
            transaction_type="interest_payment",
            notes=f"[INTERÉS EXTRA] {payload.motivo}",
        )
        db.add(txn)
        db.commit()
        db.refresh(txn)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al registrar interés extra: {exc}")

    return {
        "transaction_id": txn.id,
        "loan_id": loan.id,
        "member_name": loan.member.name if loan.member else "",
        "monto": float(monto),
        "fecha_pago": str(payload.fecha_pago),
        "motivo": payload.motivo,
    }
