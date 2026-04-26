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

from app.api.v1.cajas import router as cajas_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.groups import router as groups_router
from app.api.v1.loans import router as loans_router
from app.api.v1.members import router as members_router
from app.api.v1.reports import router as reports_router
from app.api.v1.transactions import router as transactions_router
from app.core.config import settings
from app.core.database import engine, get_db
from app.models.base import Base, CajaConfig, Loan, Member, Transaction


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title=settings.project_name, lifespan=lifespan)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# ── API routers ────────────────────────────────────────────────────────────────
PREFIX = "/api/v1"
app.include_router(cajas_router,        prefix=PREFIX)
app.include_router(dashboard_router,    prefix=PREFIX)
app.include_router(groups_router,       prefix=PREFIX)
app.include_router(members_router,      prefix=PREFIX)
app.include_router(loans_router,        prefix=PREFIX)
app.include_router(transactions_router, prefix=PREFIX)
app.include_router(reports_router,      prefix=PREFIX)


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
            {"id": m.id, "name": m.name}
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
            .options(joinedload(Member.loans))
            .filter(Member.caja_id == caja.id, Member.is_active == True)
            .order_by(Member.name)
            .all()
        )
        for m in rows:
            active = [l for l in m.loans if l.status == "active"]
            members_data.append({
                "id":              m.id,
                "name":            m.name,
                "group":           m.group,
                "savings_balance": float(m.savings_balance),
                "active_loans":    len(active),
                "total_debt":      float(sum(l.outstanding_balance for l in active)),
            })
            
        group_rows = db.query(MemberGroup).filter(MemberGroup.caja_id == caja.id).order_by(MemberGroup.name).all()
        for g in group_rows:
            groups_data.append({"name": g.name})
            
    return templates.TemplateResponse(request, "members.html", {
        "cajas":           cajas,
        "current_caja_id": caja.id if caja else None,
        "active_page":     "members",
        "members":         members_data,
        "caja_name":       caja.name if caja else "",
        "caja_id":         caja.id if caja else None,
        "groups_json":     json.dumps(groups_data),
    })


@app.get("/groups")
def groups_view(request: Request, caja_id: int = 1, db: Session = Depends(get_db)):
    from app.models.base import MemberGroup
    cajas = _get_cajas(db)
    caja  = _resolve_caja(caja_id, cajas)
    
    groups_data = []
    if caja:
        rows = db.query(MemberGroup).filter(MemberGroup.caja_id == caja.id).order_by(MemberGroup.name).all()
        for g in rows:
            members_count = db.query(func.count(Member.id)).filter(
                Member.group == g.name, 
                Member.caja_id == caja.id,
                Member.is_active == True
            ).scalar() or 0
            
            groups_data.append({
                "id": g.id,
                "name": g.name,
                "members_count": members_count,
            })
            
    return templates.TemplateResponse(request, "groups.html", {
        "cajas":           cajas,
        "current_caja_id": caja.id if caja else None,
        "active_page":     "groups",
        "groups_json":     json.dumps(groups_data),
        "caja_name":       caja.name if caja else "",
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
        })
    return templates.TemplateResponse(request, "settings.html", {
        "cajas":           cajas,
        "current_caja_id": cajas[0].id if cajas else None,
        "active_page":     "settings",
        "cajas_json":      json.dumps(cajas_data),
    })