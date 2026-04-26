"""
importer.py — Migra ahorros desde CAJA_AHORROS_2026.xlsx al modelo de datos.

Uso CLI:
    python -m app.services.importer \
        --file "C:/Users/emiro/Downloads/CAJA_AHORROS_2026 (1).xlsx" \
        --caja-id 1

Uso programático:
    from app.services.importer import run_import
    result = run_import(file_path="...", caja_id=1, db=session)
"""
import argparse
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.base import CajaConfig, Member, Transaction

# ── Constantes ────────────────────────────────────────────────────────────────

QUINCENA_BASE = date(2025, 12, 15)   # Quincena 1 = 15 dic 2025
QUINCENA_DAYS = 15                   # Incremento entre quincenas

# Nombres que no son socios (filas de totales/footer)
_SKIP_NAMES = frozenset({"TOTAL PRESTAMOS", "TOTAL FUERA", "TOTAL", "TOTAL/TOTALES"})

# Patrones para extraer grupo del nombre del socio
_GROUP_PATTERNS = [
    (r"ELENITA",                     "Elenita"),
    (r"NATACI[OÓ]N",                 "Natación"),
    (r"LOLITA\s*AYALA|LOLITAAYALA|LOLITAYALA", "Lolita Ayala"),
    (r"EMMANUEL",                    "Emmanuel"),
    (r"TAICHI",                      "Taichi"),
    (r"PANADERIA",                   "Panadería"),
    (r"PILI\s*MEJIA|PILIMEJIA",      "Pili Mejía"),
    (r"AMIGO",                       "Amigos"),
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _quincena_date(n: int) -> date:
    """Fecha de la quincena N (1-indexado). Q1=15 dic 2025, Q2=30 dic 2025, …"""
    return QUINCENA_BASE + timedelta(days=(n - 1) * QUINCENA_DAYS)


def _extract_group(name: str) -> str:
    upper = name.upper()
    for pattern, group in _GROUP_PATTERNS:
        if re.search(pattern, upper):
            return group
    return "General"


def _is_valid_member_row(val) -> bool:
    """True si el valor de la columna NOMBRE corresponde a un socio real."""
    if not isinstance(val, str):
        return False
    name = val.strip()
    return bool(name) and name.upper() not in _SKIP_NAMES


def _to_decimal(val) -> Decimal | None:
    """Convierte un valor de celda a Decimal. Retorna None si es vacío o inválido."""
    if pd.isna(val):
        return None
    try:
        d = Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return d if d > 0 else None
    except Exception:
        return None


# ── Core ──────────────────────────────────────────────────────────────────────

def run_import(file_path: str, caja_id: int, db: Session) -> dict:
    """
    Lee el xlsx, crea socios y movimientos de ahorro de forma idempotente.
    Retorna un dict con estadísticas de la importación.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Archivo no encontrado: {path}")

    caja = db.get(CajaConfig, caja_id)
    if not caja:
        raise ValueError(
            f"Caja con id={caja_id} no encontrada. "
            "Créala primero en POST /api/v1/cajas antes de importar."
        )

    # Fila 0 = título decorativo → skiprows=1 usa fila 1 como header.
    # Columnas resultantes: NOMBRE | QUINCENA 1 | 2.0 | 3.0 | … | 24.0 | TOTAL
    df = pd.read_excel(path, sheet_name="GENERAL", skiprows=1)

    all_cols = list(df.columns)
    nombre_col = all_cols[0]         # "NOMBRE"
    total_col  = all_cols[-1]        # "TOTAL"

    # Columnas de quincenas: todo entre NOMBRE y TOTAL (posiciones 1..24)
    quincena_cols = [c for c in all_cols if c not in (nombre_col, total_col)]
    # quincena_cols[0] = "QUINCENA 1"  →  quincena 1
    # quincena_cols[n] = float n+1      →  quincena n+1

    members_created      = 0
    members_reused       = 0
    transactions_created = 0
    transactions_skipped = 0

    for _, row in df.iterrows():
        raw_name = row[nombre_col]
        if not _is_valid_member_row(raw_name):
            continue

        name  = raw_name.strip()
        group = _extract_group(name)

        # ── Idempotencia: buscar o crear socio ──────────────────────────────
        member = (
            db.query(Member)
            .filter(Member.name == name, Member.caja_id == caja_id)
            .first()
        )
        if not member:
            member = Member(
                name=name,
                group=group,
                caja_id=caja_id,
                savings_balance=Decimal("0.00"),
            )
            db.add(member)
            db.flush()          # necesario para obtener member.id antes del commit
            members_created += 1
        else:
            members_reused += 1

        # ── Iterar quincenas ────────────────────────────────────────────────
        for i, col in enumerate(quincena_cols):
            quincena_num = i + 1
            txn_date     = _quincena_date(quincena_num)
            amount       = _to_decimal(row[col])

            if amount is None:
                continue

            # Idempotencia: un solo registro de ahorro por socio/quincena
            exists = (
                db.query(Transaction)
                .filter(
                    Transaction.member_id       == member.id,
                    Transaction.transaction_date == txn_date,
                    Transaction.transaction_type == "savings",
                )
                .first()
            )
            if exists:
                transactions_skipped += 1
                continue

            db.add(Transaction(
                loan_id=None,
                member_id=member.id,
                amount=amount,
                transaction_date=txn_date,
                transaction_type="savings",
                notes=f"Importado — Quincena {quincena_num}",
            ))
            transactions_created += 1

        db.flush()

    # ── Recalcular savings_balance desde los movimientos reales ────────────
    # Evita que corridas repetidas acumulen incorrectamente.
    member_ids = [
        mid for (mid,) in
        db.query(Member.id).filter(Member.caja_id == caja_id).all()
    ]
    for mid in member_ids:
        savings_rows = (
            db.query(Transaction.amount)
            .filter(
                Transaction.member_id       == mid,
                Transaction.transaction_type == "savings",
            )
            .all()
        )
        total = sum(Decimal(str(r[0])) for r in savings_rows)
        m = db.get(Member, mid)
        if m:
            m.savings_balance = total

    db.commit()

    return {
        "members_created":      members_created,
        "members_reused":       members_reused,
        "transactions_created": transactions_created,
        "transactions_skipped": transactions_skipped,
    }


# ── CLI entry-point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Importa ahorros desde CAJA_AHORROS_2026.xlsx"
    )
    parser.add_argument(
        "--file",
        required=True,
        help='Ruta al archivo .xlsx (p.ej. "C:/Users/.../CAJA_AHORROS_2026 (1).xlsx")',
    )
    parser.add_argument(
        "--caja-id",
        type=int,
        required=True,
        dest="caja_id",
        help="ID de la CajaConfig de destino (debe existir en la BD).",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        result = run_import(file_path=args.file, caja_id=args.caja_id, db=db)

        print(
            f"\nCarga finalizada: {result['members_created']} socios creados, "
            f"{result['transactions_created']} movimientos de ahorro registrados."
        )
        if result["members_reused"] or result["transactions_skipped"]:
            print(
                f"  Omitidos (idempotencia): "
                f"{result['members_reused']} socios ya existentes, "
                f"{result['transactions_skipped']} movimientos duplicados."
            )
    except (FileNotFoundError, ValueError) as exc:
        print(f"\nError: {exc}")
        raise SystemExit(1)
    finally:
        db.close()
