"""
pdf_generator.py — Genera PDFs con WeasyPrint para SafeFund Ledger.

Reportes disponibles:
  - Estado de Cuenta por socio (ahorros + historial de préstamos)
  - Reporte de Caja (capital, prestado, rendimientos, estatus de intereses)
"""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.base import CajaConfig, Loan, Member, Transaction

REPORTS_DIR = Path(__file__).parent.parent / "templates" / "reports"
CENT = Decimal("0.01")


def _jinja_env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(REPORTS_DIR)), autoescape=False)
    env.filters["mxn"] = _fmt_mxn
    env.filters["fdate"] = lambda d: d.strftime("%d/%m/%Y") if d else "—"
    return env


def _fmt_mxn(value) -> str:
    try:
        d = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
        return f"${d:,.2f}"
    except Exception:
        return "$0.00"


def _render(template_name: str, ctx: dict) -> str:
    return _jinja_env().get_template(template_name).render(**ctx)


def _to_pdf(html: str) -> bytes:
    from weasyprint import HTML
    return HTML(string=html).write_pdf()


# ── Estado de Cuenta ──────────────────────────────────────────────────────────

def generate_estado_cuenta(member_id: int, db: Session) -> bytes:
    member = db.get(Member, member_id)
    if not member:
        raise ValueError(f"Socio {member_id} no encontrado.")

    caja = db.get(CajaConfig, member.caja_id)

    savings = (
        db.query(Transaction)
        .filter(
            Transaction.member_id == member_id,
            Transaction.transaction_type == "savings",
        )
        .order_by(Transaction.transaction_date)
        .all()
    )

    loans = (
        db.query(Loan)
        .filter(Loan.member_id == member_id)
        .order_by(Loan.start_date)
        .all()
    )

    loan_details = []
    for loan in loans:
        txns = (
            db.query(Transaction)
            .filter(Transaction.loan_id == loan.id)
            .order_by(Transaction.transaction_date)
            .all()
        )
        loan_details.append(
            {
                "loan": loan,
                "transactions": txns,
                "total_interest_paid": sum(
                    Decimal(str(t.amount))
                    for t in txns
                    if t.transaction_type == "interest_payment"
                ),
                "total_capital_paid": sum(
                    Decimal(str(t.amount))
                    for t in txns
                    if t.transaction_type == "capital_payment"
                ),
            }
        )

    total_savings = sum(Decimal(str(t.amount)) for t in savings)
    total_interest_all = sum(d["total_interest_paid"] for d in loan_details)
    total_capital_all = sum(d["total_capital_paid"] for d in loan_details)
    outstanding_total = sum(
        Decimal(str(d["loan"].outstanding_balance))
        for d in loan_details
        if d["loan"].status == "active"
    )

    html = _render(
        "estado_cuenta.html",
        {
            "member": member,
            "caja": caja,
            "savings": savings,
            "total_savings": total_savings,
            "loan_details": loan_details,
            "total_interest_all": total_interest_all,
            "total_capital_all": total_capital_all,
            "outstanding_total": outstanding_total,
            "generated_at": date.today(),
        },
    )
    return _to_pdf(html)


# ── Reporte de Caja ───────────────────────────────────────────────────────────

def generate_reporte_caja(caja_id: int, db: Session) -> bytes:
    caja = db.get(CajaConfig, caja_id)
    if not caja:
        raise ValueError(f"Caja {caja_id} no encontrada.")

    total_ahorros = Decimal(
        str(
            db.query(func.sum(Member.savings_balance))
            .filter(Member.caja_id == caja_id)
            .scalar()
            or 0
        )
    )
    total_prestado = Decimal(
        str(
            db.query(func.sum(Loan.outstanding_balance))
            .filter(Loan.caja_id == caja_id, Loan.status == "active")
            .scalar()
            or 0
        )
    )
    total_rendimientos = Decimal(
        str(
            db.query(func.sum(Transaction.amount))
            .join(Loan, Transaction.loan_id == Loan.id)
            .filter(
                Loan.caja_id == caja_id,
                Transaction.transaction_type == "interest_payment",
            )
            .scalar()
            or 0
        )
    )

    active_loans = (
        db.query(Loan)
        .filter(Loan.caja_id == caja_id, Loan.status == "active")
        .order_by(Loan.member_id)
        .all()
    )

    today = date.today()
    start_of_month = today.replace(day=1)

    loan_rows = []
    for loan in active_loans:
        member = db.get(Member, loan.member_id)
        last_interest = (
            db.query(Transaction)
            .filter(
                Transaction.loan_id == loan.id,
                Transaction.transaction_type == "interest_payment",
            )
            .order_by(Transaction.transaction_date.desc())
            .first()
        )
        monthly_interest = (
            Decimal(str(loan.outstanding_balance)) * Decimal(str(loan.interest_rate))
        ).quantize(CENT, rounding=ROUND_HALF_UP)

        interest_current = (
            last_interest is not None
            and last_interest.transaction_date >= start_of_month
        )
        loan_rows.append(
            {
                "loan": loan,
                "member_name": member.name if member else "—",
                "group": member.group if member else "—",
                "monthly_interest": monthly_interest,
                "last_interest_date": last_interest.transaction_date if last_interest else None,
                "interest_current": interest_current,
            }
        )

    n_current = sum(1 for r in loan_rows if r["interest_current"])
    n_overdue = len(loan_rows) - n_current
    total_interest_due_month = sum(r["monthly_interest"] for r in loan_rows)

    html = _render(
        "reporte_caja.html",
        {
            "caja": caja,
            "total_ahorros": total_ahorros,
            "total_prestado": total_prestado,
            "total_rendimientos": total_rendimientos,
            "capital_disponible": total_ahorros - total_prestado,
            "loan_rows": loan_rows,
            "n_current": n_current,
            "n_overdue": n_overdue,
            "total_interest_due_month": total_interest_due_month,
            "generated_at": today,
        },
    )
    return _to_pdf(html)
