import json
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.api.v1.aportaciones import router as aportaciones_router
from app.api.v1.quincenas import router as quincenas_router
from app.api.v1.cajas import router as cajas_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.groups import router as groups_router
from app.api.v1.loans import router as loans_router
from app.api.v1.members import router as members_router
from app.api.v1.reports import router as reports_router
from app.api.v1.transactions import router as transactions_router
from app.core.config import settings
from app.core.database import engine, get_db
from app.models.base import (
    Aportacion,
    Base,
    CajaConfig,
    Loan,
    Member,
    MemberGroup,
    QuincenaPendiente,
    Transaction,
)
from app.api.v1.quincenas import _get_current_quincena


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    # Migración: agregar quota_personal si no existe
    from sqlalchemy import text
    with engine.connect() as conn:
        try:
            conn.execute(text(
                "ALTER TABLE members ADD COLUMN quota_personal NUMERIC(12,2) NOT NULL DEFAULT 0.00"
            ))
            conn.commit()
        except Exception:
            pass  # columna ya existe
    yield


app = FastAPI(title=settings.project_name, lifespan=lifespan)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# ── API routers ────────────────────────────────────────────────────────────────
PREFIX = "/api/v1"
app.include_router(cajas_router,         prefix=PREFIX)
app.include_router(dashboard_router,     prefix=PREFIX)
app.include_router(groups_router,        prefix=PREFIX)
app.include_router(members_router,       prefix=PREFIX)
app.include_router(loans_router,         prefix=PREFIX)
app.include_router(transactions_router,  prefix=PREFIX)
app.include_router(reports_router,       prefix=PREFIX)
app.include_router(aportaciones_router,  prefix=PREFIX)
app.include_router(quincenas_router,     prefix=PREFIX)


# ── Health ─────────────────────────────────────────────────────────────────────
@app.get("/health")
def health_check():
    return {"status": "ok", "service": settings.project_name}


# ── View helpers ───────────────────────────────────────────────────────────────
def _get_cajas(db: Session) -> list[CajaConfig]:
    return db.query(CajaConfig).order_by(CajaConfig.name).all()


def _resolve_caja(caja_id: int, cajas: list[CajaConfig]) -> CajaConfig | None:
    match = next((c for c in cajas if c.id == caja_id), None)
    return match or (cajas[0] if cajas else None)


# ── Template views ─────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return RedirectResponse(url="/dashboard?caja_id=1")


@app.get("/dashboard")
def dashboard_view(request: Request, caja_id: int = 1, db: Session = Depends(get_db)):
    cajas  = _get_cajas(db)
    caja   = _resolve_caja(caja_id, cajas)
    today  = date.today()

    metrics = None
    if caja:
        caja_id = caja.id
        start   = today.replace(day=1)

        capital = db.query(func.sum(Loan.outstanding_balance)).filter(
            Loan.caja_id == caja_id, Loan.status == "active"
        ).scalar() or Decimal("0")

        ahorros = db.query(func.sum(Member.savings_balance)).filter(
            Member.caja_id == caja_id
        ).scalar() or Decimal("0")

        intereses = db.query(func.sum(Transaction.amount)).join(
            Loan, Transaction.loan_id == Loan.id
        ).filter(
            Loan.caja_id == caja_id,
            Transaction.transaction_type == "interest_payment",
            Transaction.transaction_date >= start,
        ).scalar() or Decimal("0")

        n_prestamos = db.query(func.count(Loan.id)).filter(
            Loan.caja_id == caja_id, Loan.status == "active"
        ).scalar() or 0

        n_socios = db.query(func.count(Member.id)).filter(
            Member.caja_id == caja_id, Member.is_active == True
        ).scalar() or 0

        from app.services.finance_engine import calculate_monthly_interest
        from decimal import ROUND_HALF_UP
        from datetime import date as _date

        CYCLE_END = _date(2026, 11, 30)
        remaining_months = max(0, round((CYCLE_END - today).days / 30))

        active_loans = (
            db.query(Loan)
            .filter(Loan.caja_id == caja_id, Loan.status == "active")
            .all()
        )
        intereses_totales = Decimal(str(
            db.query(func.sum(Transaction.amount))
            .join(Loan, Transaction.loan_id == Loan.id)
            .filter(Loan.caja_id == caja_id, Transaction.transaction_type == "interest_payment")
            .scalar() or 0
        ))
        intereses_proyectados = sum(
            calculate_monthly_interest(l.outstanding_balance, l.interest_rate) * remaining_months
            for l in active_loans
        )
        proyeccion = (
            Decimal(str(ahorros)) + intereses_totales + Decimal(str(intereses_proyectados))
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        metrics = {
            "caja_id":             caja_id,
            "caja_name":           caja.name,
            "total_en_caja":       Decimal(str(ahorros)),
            "capital_en_prestamos": Decimal(str(capital)),
            "intereses_totales":   intereses_totales,
            "proyeccion_cierre":   proyeccion,
            "capital_disponible":  Decimal(str(ahorros)) - Decimal(str(capital)),
            "intereses_mes":       Decimal(str(intereses)),
            "prestamos_activos":   n_prestamos,
            "total_socios":        n_socios,
        }

    return templates.TemplateResponse(request, "dashboard.html", {
        "cajas":           cajas,
        "current_caja_id": caja.id if caja else None,
        "active_page":     "dashboard",
        "metrics":         metrics,
        "today":           today.strftime("%d %b %Y"),
    })


@app.get("/loans")
def loans_view(request: Request, caja_id: int = 1, db: Session = Depends(get_db)):
    cajas = _get_cajas(db)
    caja  = _resolve_caja(caja_id, cajas)

    loans_data = []
    if caja:
        caja_id = caja.id
        start_of_month = date.today().replace(day=1)
        rows = (
            db.query(Loan)
            .options(joinedload(Loan.member))
            .filter(Loan.caja_id == caja_id, Loan.status == "active")
            .order_by(Loan.id)
            .all()
        )
        for l in rows:
            last_interest = (
                db.query(Transaction)
                .filter(
                    Transaction.loan_id == l.id,
                    Transaction.transaction_type == "interest_payment",
                )
                .order_by(Transaction.transaction_date.desc())
                .first()
            )
            interest_paid_this_month = (
                last_interest is not None
                and last_interest.transaction_date >= start_of_month
            )
            loans_data.append({
                "id":                     l.id,
                "member_name":            l.member.name if l.member else "",
                "initial_amount":         float(l.initial_amount),
                "outstanding_balance":    float(l.outstanding_balance),
                "interest_rate":          float(l.interest_rate),
                "loan_type":              l.loan_type,
                "start_date":             str(l.start_date),
                "interest_paid_month":    interest_paid_this_month,
                "last_interest_date":     str(last_interest.transaction_date) if last_interest else None,
                "_saving":                False,
            })

    members_dropdown = []
    if caja:
        members_dropdown = [
            {"id": m.id, "name": m.name, "member_type": m.member_type}
            for m in db.query(Member)
                .filter(Member.caja_id == caja.id, Member.is_active == True)
                .order_by(Member.name)
                .all()
        ]

    return templates.TemplateResponse(request, "loans.html", {
        "cajas":            cajas,
        "current_caja_id":  caja.id if caja else None,
        "active_page":      "loans",
        "loans_json":       json.dumps(loans_data),
        "caja_name":        caja.name if caja else "",
        "members_json":     json.dumps(members_dropdown),
        "caja_rates":       json.dumps({
            "internal": float(caja.interest_rate_internal) if caja else 0.05,
            "external": float(caja.interest_rate_external) if caja else 0.08,
        }),
    })


@app.get("/members")
def members_view(request: Request, caja_id: int = 1, db: Session = Depends(get_db)):
    from app.models.base import MemberGroup
    cajas = _get_cajas(db)
    caja  = _resolve_caja(caja_id, cajas)
    members_data = []
    groups_data = []
    if caja:
        rows = (
            db.query(Member)
            .options(joinedload(Member.loans), joinedload(Member.member_group))
            .filter(Member.caja_id == caja.id, Member.is_active == True)
            .order_by(Member.name)
            .all()
        )
        current_q = _get_current_quincena(caja.start_date, caja.total_quincenas) if caja.start_date else 0
        needs_commit = False
        for m in rows:
            active = [l for l in m.loans if l.status == "active"]

            # Quincenas pagadas
            pagadas_count = db.query(func.count(QuincenaPendiente.id)).filter(
                QuincenaPendiente.member_id == m.id,
                QuincenaPendiente.quincena_num <= current_q
            ).scalar() or 0

            quincena_hasta_m = db.query(func.max(QuincenaPendiente.quincena_num)).filter(
                QuincenaPendiente.member_id == m.id,
                QuincenaPendiente.quincena_num <= current_q
            ).scalar() or 0

            is_interno = m.member_type == "dentro"
            p_count = max(0, current_q - pagadas_count) if is_interno else 0

            # Recalcular savings_balance desde registros reales para corregir datos obsoletos:
            # sum(Aportaciones del miembro en esta caja) + sum(Transacciones de ahorro)
            apo_sum = db.query(func.coalesce(func.sum(Aportacion.monto), 0)).filter(
                Aportacion.member_id == m.id,
                Aportacion.caja_id == caja.id,
            ).scalar() or Decimal("0")
            txn_sum = db.query(func.coalesce(func.sum(Transaction.amount), 0)).filter(
                Transaction.member_id == m.id,
                Transaction.transaction_type == "savings",
            ).scalar() or Decimal("0")
            correct_balance = apo_sum + txn_sum

            if m.savings_balance != correct_balance:
                m.savings_balance = correct_balance
                needs_commit = True

            members_data.append({
                "id":               m.id,
                "name":             m.name,
                "group":            m.member_group.name if m.member_group else "General",
                "group_id":         m.group_id,
                "member_type":      m.member_type,
                "quota_personal":   float(m.quota_personal),
                "quincena_hasta":   quincena_hasta_m,
                "savings_balance":  float(correct_balance),
                "active_loans":     len(active),
                "total_debt":       float(sum(l.outstanding_balance for l in active)),
                "pendientes_count": p_count,
            })

        if needs_commit:
            db.commit()
            
        group_rows = db.query(MemberGroup).filter(MemberGroup.caja_id == caja.id).order_by(MemberGroup.id).all()
        for g in group_rows:
            groups_data.append({"id": g.id, "name": g.name})
            
    return templates.TemplateResponse(request, "members.html", {
        "cajas":           cajas,
        "current_caja_id": caja.id if caja else None,
        "active_page":     "members",
        "members":         members_data,
        "caja_name":       caja.name if caja else "",
        "caja_id":         caja.id if caja else None,
        "groups_json":     json.dumps(groups_data),
        "caja_rates":      json.dumps({
            "internal": float(caja.interest_rate_internal) if caja else 0.05,
            "external": float(caja.interest_rate_external) if caja else 0.08,
        }),
    })


@app.get("/groups")
def groups_view(request: Request, caja_id: int = 1, db: Session = Depends(get_db)):
    from app.models.base import MemberGroup
    cajas = _get_cajas(db)
    caja  = _resolve_caja(caja_id, cajas)
    
    groups_data = []
    externos_count = 0
    if caja:
        rows = db.query(MemberGroup).filter(MemberGroup.caja_id == caja.id).order_by(MemberGroup.id).all()
        for g in rows:
            members_count = db.query(func.count(Member.id)).filter(
                Member.group_id == g.id,
                Member.caja_id == caja.id,
                Member.member_type == 'dentro',
                Member.is_active == True
            ).scalar() or 0

            groups_data.append({
                "id": g.id,
                "name": g.name,
                "members_count": members_count,
            })

        externos_count = db.query(func.count(Member.id)).filter(
            Member.caja_id == caja.id,
            Member.member_type == 'fuera',
            Member.is_active == True
        ).scalar() or 0

    return templates.TemplateResponse(request, "groups.html", {
        "cajas":           cajas,
        "current_caja_id": caja.id if caja else None,
        "active_page":     "groups",
        "groups_json":     json.dumps(groups_data),
        "externos_count":  externos_count,
        "caja_name":       caja.name if caja else "",
    })

@app.get("/quincenas")
def quincenas_view(request: Request, caja_id: int = 1, db: Session = Depends(get_db)):
    cajas = _get_cajas(db)
    caja  = _resolve_caja(caja_id, cajas)

    # Datos de quincenas se cargan vía fetch en el cliente para no bloquear si no hay start_date
    caja_info = None
    if caja:
        caja_info = {
            "id":              caja.id,
            "name":            caja.name,
            "start_date":      str(caja.start_date) if caja.start_date else None,
            "quota_amount":    float(caja.quota_amount),
            "total_quincenas": caja.total_quincenas,
        }

    return templates.TemplateResponse(request, "quincenas.html", {
        "cajas":           cajas,
        "current_caja_id": caja.id if caja else None,
        "active_page":     "quincenas",
        "caja_name":       caja.name if caja else "",
        "caja_json":       json.dumps(caja_info),
    })


@app.get("/bi-dashboard")
def bi_dashboard_view(request: Request, caja_id: int = 1, db: Session = Depends(get_db)):
    from app.services.loan_engine import get_loans_with_mora
    cajas = _get_cajas(db)
    caja  = _resolve_caja(caja_id, cajas)

    bi_data: dict = {
        "caja_name":          caja.name if caja else "",
        "capital_aportado":   0.0,
        "capital_prestado":   0.0,
        "intereses_cobrados": 0.0,
        "capital_disponible": 0.0,
        "rendimiento_pct":    0.0,
        "n_aportaciones":     0,
        "n_prestamos_activos":0,
        "mora":               [],
        "miembros":           [],
    }

    if caja:
        cid = caja.id

        cap_aportado = db.query(func.sum(Aportacion.monto)).filter(
            Aportacion.caja_id == cid
        ).scalar() or Decimal("0")

        cap_prestado = db.query(func.sum(Loan.outstanding_balance)).filter(
            Loan.caja_id == cid, Loan.status == "active"
        ).scalar() or Decimal("0")

        intereses = db.query(func.sum(Transaction.amount)).join(
            Loan, Transaction.loan_id == Loan.id
        ).filter(
            Loan.caja_id == cid,
            Transaction.transaction_type == "interest_payment",
        ).scalar() or Decimal("0")

        n_aport = db.query(func.count(Aportacion.id)).filter(
            Aportacion.caja_id == cid
        ).scalar() or 0

        n_prest = db.query(func.count(Loan.id)).filter(
            Loan.caja_id == cid, Loan.status == "active"
        ).scalar() or 0

        cap_aportado = Decimal(str(cap_aportado))
        cap_prestado = Decimal(str(cap_prestado))
        intereses    = Decimal(str(intereses))
        disponible   = (cap_aportado + intereses - cap_prestado)
        rend_pct = (
            (intereses / cap_aportado * 100).quantize(Decimal("0.01"))
            if cap_aportado > 0 else Decimal("0")
        )

        mora_loans = get_loans_with_mora(cid, db)
        mora_list = [
            {
                "loan_id":            l.loan_id,
                "member_id":          l.member_id,
                "member_name":        l.member_name,
                "loan_type":          l.loan_type,
                "outstanding_balance": float(l.outstanding_balance),
                "interes_mes_actual": float(l.interes_mes_actual),
                "dias_sin_pago":      l.dias_sin_pago,
                "ultimo_pago_interes": str(l.ultimo_pago_interes) if l.ultimo_pago_interes else None,
                "en_mora":            l.en_mora,
            }
            for l in mora_loans
        ]

        miembros = [
            {"id": m.id, "name": m.name}
            for m in db.query(Member)
                .filter(Member.caja_id == cid, Member.is_active == True)
                .order_by(Member.name).all()
        ]

        from app.models.base import MemberGroup
        group_rows = db.query(MemberGroup).filter(MemberGroup.caja_id == cid).order_by(MemberGroup.id).all()
        group_map = {g.id: g.name for g in group_rows}

        all_members = db.query(Member).filter(Member.caja_id == cid, Member.is_active == True).all()
        member_rankings = []
        for m in all_members:
            m_aport = float(
                db.query(func.sum(Aportacion.monto))
                .filter(Aportacion.member_id == m.id, Aportacion.caja_id == cid)
                .scalar() or Decimal("0")
            )
            member_rankings.append({
                "id":              m.id,
                "name":            m.name,
                "group_id":        m.group_id,
                "group_name":      group_map.get(m.group_id, "Sin grupo"),
                "total_aportado":  m_aport,
                "savings_balance": float(m.savings_balance),
            })
        member_rankings.sort(key=lambda x: x["total_aportado"], reverse=True)

        group_stats = []
        for g in group_rows:
            g_members = [mr for mr in member_rankings if mr["group_id"] == g.id]
            g_total = sum(mr["total_aportado"] for mr in g_members)
            group_stats.append({
                "id":       g.id,
                "name":     g.name,
                "total":    round(g_total, 2),
                "n_members": len(g_members),
                "members":  g_members,
            })
        group_stats.sort(key=lambda x: x["total"], reverse=True)

        bi_data.update({
            "capital_aportado":    float(cap_aportado),
            "capital_prestado":    float(cap_prestado),
            "intereses_cobrados":  float(intereses),
            "capital_disponible":  float(disponible),
            "rendimiento_pct":     float(rend_pct),
            "n_aportaciones":      n_aport,
            "n_prestamos_activos": n_prest,
            "mora":                mora_list,
            "miembros":            miembros,
            "group_stats":         group_stats,
            "member_rankings":     member_rankings,
        })

    return templates.TemplateResponse(request, "admin/dashboard_bi.html", {
        "cajas":           cajas,
        "current_caja_id": caja.id if caja else None,
        "active_page":     "bi",
        "bi":              bi_data,
        "bi_json":         json.dumps(bi_data),
        "today":           date.today().strftime("%d %b %Y"),
    })


@app.get("/loans/metrics-partial")
def loans_metrics_partial(request: Request, caja_id: int = 1, db: Session = Depends(get_db)):
    """Fragmento HTMX: banda de métricas de la cartera de préstamos."""
    from decimal import ROUND_HALF_UP

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
    pct_int = float(
        (capital_interno / total_cartera * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    ) if total_cartera > 0 else 0.0
    pct_ext = float(
        (capital_externo / total_cartera * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    ) if total_cartera > 0 else 0.0

    # Ranking top 5
    member_interest_rows = (
        db.query(Transaction.member_id, func.sum(Transaction.amount).label("total"))
        .join(Loan, Transaction.loan_id == Loan.id)
        .filter(
            Loan.caja_id == caja_id,
            Transaction.transaction_type == "interest_payment",
        )
        .group_by(Transaction.member_id)
        .order_by(func.sum(Transaction.amount).desc())
        .limit(5)
        .all()
    )
    ranking = []
    for row in member_interest_rows:
        m = db.get(Member, row.member_id)
        if not m:
            continue
        m_active = [l for l in active_loans if l.member_id == row.member_id]
        ranking.append({
            "member_name": m.name,
            "total_intereses": float(Decimal(str(row.total))),
            "outstanding_balance": float(sum(Decimal(str(l.outstanding_balance)) for l in m_active)),
            "n_loans": len(m_active),
        })

    metrics = {
        "total_prestado":      float(total_prestado),
        "saldo_insoluto_total": float(saldo_insoluto),
        "ganancias_acumuladas": float(Decimal(str(ganancias_raw))),
        "ganancias_mes":        float(Decimal(str(ganancias_mes_raw))),
        "n_activos":   n_activos,
        "n_liquidados": n_liquidados,
        "capital_interno": float(capital_interno),
        "capital_externo": float(capital_externo),
        "pct_interno": pct_int,
        "pct_externo": pct_ext,
        "ranking": ranking,
    }

    return templates.TemplateResponse(request, "partials/loan_metrics.html", {
        "m": metrics,
        "caja_id": caja_id,
    })


@app.get("/loans/{loan_id}/gestionar-partial")
def loan_gestionar_partial(request: Request, loan_id: int, db: Session = Depends(get_db)):
    """Fragmento HTMX: panel de gestión individual de un préstamo."""
    from decimal import ROUND_HALF_UP
    from app.services.loan_engine import calculate_accrued_interest

    loan = (
        db.query(Loan)
        .options(joinedload(Loan.member), joinedload(Loan.transactions))
        .filter(Loan.id == loan_id)
        .first()
    )
    if not loan:
        return templates.TemplateResponse(request, "partials/loan_gestionar.html", {
            "error": "Préstamo no encontrado.", "loan": None,
        })

    today = date.today()
    CENT = Decimal("0.01")
    balance = Decimal(str(loan.outstanding_balance))
    rate = Decimal(str(loan.interest_rate))
    interes_mes = (balance * rate).quantize(CENT, rounding=ROUND_HALF_UP)

    try:
        acumulado = calculate_accrued_interest(loan_id, today, db)
        interes_acumulado = float(acumulado.interes_acumulado)
    except Exception:
        interes_acumulado = 0.0

    historial = sorted(
        loan.transactions,
        key=lambda t: (t.transaction_date, t.id),
        reverse=True,
    )

    loan_data = {
        "id": loan.id,
        "member_name": loan.member.name if loan.member else "",
        "member_id": loan.member_id,
        "initial_amount": float(loan.initial_amount),
        "outstanding_balance": float(loan.outstanding_balance),
        "interest_rate": float(loan.interest_rate),
        "loan_type": loan.loan_type,
        "status": loan.status,
        "start_date": str(loan.start_date),
        "interes_mes": float(interes_mes),
        "interes_acumulado": interes_acumulado,
    }

    txns = [
        {
            "id": t.id,
            "transaction_date": str(t.transaction_date),
            "transaction_type": t.transaction_type,
            "amount": float(t.amount),
            "notes": t.notes or "",
        }
        for t in historial
    ]

    return templates.TemplateResponse(request, "partials/loan_gestionar.html", {
        "loan": loan_data,
        "loan_json": json.dumps(loan_data),
        "historial": txns,
        "today": str(today),
        "error": None,
    })


@app.get("/settings")
def settings_view(request: Request, db: Session = Depends(get_db)):
    cajas = _get_cajas(db)
    cajas_data = []
    for c in cajas:
        n_members = db.query(func.count(Member.id)).filter(
            Member.caja_id == c.id, Member.is_active == True
        ).scalar() or 0
        n_loans = db.query(func.count(Loan.id)).filter(
            Loan.caja_id == c.id, Loan.status == "active"
        ).scalar() or 0
        total_sav = db.query(func.sum(Member.savings_balance)).filter(
            Member.caja_id == c.id
        ).scalar() or Decimal("0")
        cajas_data.append({
            "id":                     c.id,
            "name":                   c.name,
            "interest_rate_internal": float(c.interest_rate_internal),
            "interest_rate_external": float(c.interest_rate_external),
            "total_quincenas":        c.total_quincenas,
            "quota_amount":           float(c.quota_amount),
            "n_members":              n_members,
            "n_loans":                n_loans,
            "total_savings":          float(total_sav),
            "start_date":             c.start_date.isoformat() if c.start_date else None,
        })
    return templates.TemplateResponse(request, "settings.html", {
        "cajas":           cajas,
        "current_caja_id": cajas[0].id if cajas else None,
        "active_page":     "settings",
        "cajas_json":      json.dumps(cajas_data),
    })