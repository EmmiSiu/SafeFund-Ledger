"""
cierre.py — Análisis de caja, simulador de préstamo y cierre con reparto de rendimientos.

Un solo cálculo (`build_resumen`) alimenta la pantalla, el PDF y la confirmación
del cierre, para que los tres coincidan siempre para el mismo corte.

Reglas de negocio:
  · Socio elegible = interno ("dentro"), activo y con cuota quincenal > $0.
  · Deuda liquidable del socio = capital insoluto de sus préstamos activos
    + interés pendiente al corte (si el borrador lo incluye).
  · El ahorro se aplica UNA sola vez al conjunto de préstamos del socio:
      faltante          = máx(deuda − ahorro, 0)
      ahorro a devolver = máx(ahorro − deuda, 0)
  · Reparto: participación = (ahorro × factor del grupo) ÷ Σ ahorros ponderados,
    usando el ahorro ANTES de compensaciones. Grupo "pidió préstamo" pesa 1;
    "nunca pidió" pesa `factor_sin_prestamo` (< 1).
"""
import hashlib
import json
from collections import defaultdict
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.core.money import CENT, round_money
from app.models.base import (
    Aportacion,
    CajaConfig,
    CierreBorrador,
    CierreCaja,
    CierreInstruccion,
    CierreMovimiento,
    CierreSocio,
    Loan,
    Member,
    Transaction,
)
from app.services.loan_engine import timeline_from_transactions
from app.services.savings import find_savings_drift, savings_by_member, sync_member_savings

ZERO = Decimal("0.00")
CIEN = Decimal("100")
PCT_Q = Decimal("0.000001")
PCT_TOLERANCIA = Decimal("0.0001")
FACTOR_DEFAULT = Decimal("0.5000")

INSTRUCCIONES = ("ahorro", "separado", "por_definir")
INSTRUCCION_LABEL = {
    "ahorro": "Liquidar con ahorro",
    "separado": "Pago por separado",
    "por_definir": "Por definir",
}
NOTA_COMPENSACION = "[COMPENSACION CIERRE]"


class CierreError(Exception):
    """Error de negocio del cierre. `status` sugiere el código HTTP."""

    def __init__(self, message: str, status: int = 400, extra: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra or {}


# ── Utilidades ─────────────────────────────────────────────────────────────────

def _mxn(value: Decimal) -> str:
    return f"${round_money(value):,.2f}"


def _jsonable(obj):
    """Convierte Decimals/fechas a texto para JSON (el dinero viaja como string exacto)."""
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


def lock_for_write(db: Session) -> None:
    """
    Toma el candado de escritura al inicio de la transacción (SQLite: BEGIN
    IMMEDIATE) para que validar-y-escribir sea atómico frente a otra petición
    concurrente: nadie más puede usar el mismo respaldo o efectivo a la vez.
    """
    bind = db.get_bind()
    if bind.dialect.name == "sqlite" and not db.in_transaction():
        db.execute(text("BEGIN IMMEDIATE"))


def is_eligible(member: Member) -> bool:
    return (
        member.member_type == "dentro"
        and bool(member.is_active)
        and Decimal(str(member.quota_personal or 0)) > 0
    )


# ── Borrador e instrucciones ───────────────────────────────────────────────────

def get_cierre(db: Session, caja_id: int) -> Optional[CierreCaja]:
    return db.query(CierreCaja).filter(CierreCaja.caja_id == caja_id).first()


def _require_open(db: Session, caja_id: int) -> None:
    if get_cierre(db, caja_id):
        raise CierreError("Esta caja ya tiene un cierre confirmado; ya no admite cambios.", 409)


def get_borrador(db: Session, caja_id: int) -> CierreBorrador:
    """Devuelve el borrador guardado o uno transitorio con valores por defecto."""
    b = db.query(CierreBorrador).filter(CierreBorrador.caja_id == caja_id).first()
    if b is None:
        b = CierreBorrador(
            caja_id=caja_id,
            fecha_corte=None,
            factor_sin_prestamo=FACTOR_DEFAULT,
            incluir_interes=True,
            overrides_json="{}",
        )
    return b


def _parse_overrides(raw: str) -> dict[int, Decimal]:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    out: dict[int, Decimal] = {}
    for k, v in data.items():
        try:
            out[int(k)] = Decimal(str(v))
        except Exception:
            continue
    return out


def update_borrador(
    db: Session,
    caja_id: int,
    *,
    fecha_corte: Optional[date] = None,
    usar_hoy: bool = False,
    factor_sin_prestamo: Optional[Decimal] = None,
    incluir_interes: Optional[bool] = None,
    overrides: Optional[dict[int, Optional[Decimal]]] = None,
    limpiar_overrides: bool = False,
) -> None:
    """Actualiza los parámetros del cierre en preparación. No hace commit."""
    _require_open(db, caja_id)
    b = get_borrador(db, caja_id)
    if b.id is None:
        db.add(b)

    if usar_hoy:
        b.fecha_corte = None
    elif fecha_corte is not None:
        b.fecha_corte = fecha_corte

    if factor_sin_prestamo is not None:
        f = Decimal(str(factor_sin_prestamo))
        if f < 0 or f > 1:
            raise CierreError("El factor debe estar entre 0 y 1.", 422)
        b.factor_sin_prestamo = f.quantize(Decimal("0.0001"))

    if incluir_interes is not None:
        b.incluir_interes = bool(incluir_interes)

    current = {} if limpiar_overrides else _parse_overrides(b.overrides_json)
    if overrides:
        for mid, pct in overrides.items():
            if pct is None:
                current.pop(int(mid), None)
                continue
            p = Decimal(str(pct))
            if p < 0 or p > CIEN:
                raise CierreError("Cada porcentaje debe estar entre 0 y 100.", 422)
            current[int(mid)] = p.quantize(PCT_Q)
    b.overrides_json = json.dumps({str(k): format(v, "f") for k, v in sorted(current.items())})


def set_instruccion(db: Session, caja_id: int, member_ids: list[int], instruccion: str) -> int:
    """Guarda la instrucción de pago para uno o varios socios. No hace commit."""
    if instruccion not in INSTRUCCIONES:
        raise CierreError("Instrucción de pago no válida.", 422)
    _require_open(db, caja_id)
    if not member_ids:
        return 0

    valid = {
        m.id for m in db.query(Member).filter(Member.caja_id == caja_id, Member.id.in_(member_ids))
    }
    existing = {
        r.member_id: r
        for r in db.query(CierreInstruccion).filter(
            CierreInstruccion.caja_id == caja_id, CierreInstruccion.member_id.in_(valid)
        )
    }
    for mid in valid:
        row = existing.get(mid)
        if row:
            row.instruccion = instruccion
        else:
            db.add(CierreInstruccion(caja_id=caja_id, member_id=mid, instruccion=instruccion))
    return len(valid)


# ── Análisis ───────────────────────────────────────────────────────────────────

def _build_analysis(db: Session, caja: CajaConfig, fecha_corte: date, incluir_interes: bool) -> dict:
    """Foto de la caja al corte, con un número fijo de consultas (sin N+1)."""
    members = (
        db.query(Member)
        .options(joinedload(Member.member_group))
        .filter(Member.caja_id == caja.id)
        .all()
    )
    members_by_id = {m.id: m for m in members}
    loans = (
        db.query(Loan)
        .filter(Loan.caja_id == caja.id)
        .order_by(Loan.start_date, Loan.id)
        .all()
    )
    txns = (
        db.query(Transaction)
        .join(Loan, Transaction.loan_id == Loan.id)
        .filter(Loan.caja_id == caja.id)
        .order_by(Transaction.transaction_date, Transaction.id)
        .all()
    )
    txns_by_loan: dict[int, list[Transaction]] = defaultdict(list)
    intereses_cobrados = Decimal("0")
    intereses_cobrados_total = Decimal("0")
    for t in txns:
        txns_by_loan[t.loan_id].append(t)
        if t.transaction_type == "interest_payment":
            amount = Decimal(str(t.amount))
            intereses_cobrados_total += amount
            if t.transaction_date <= fecha_corte:
                intereses_cobrados += amount

    savings = savings_by_member(db, caja.id)
    instrucciones = {
        r.member_id: r.instruccion
        for r in db.query(CierreInstruccion).filter(CierreInstruccion.caja_id == caja.id)
    }

    loans_by_member: dict[int, list[dict]] = defaultdict(list)
    for loan in loans:
        tl = timeline_from_transactions(loan, txns_by_loan.get(loan.id, []), fecha_corte)
        activo = loan.status == "active"
        saldo = round_money(loan.outstanding_balance) if activo else ZERO
        pendiente = max(tl.interes_pendiente, ZERO) if activo else ZERO
        loans_by_member[loan.member_id].append({
            "id": loan.id,
            "loan_type": loan.loan_type,
            "status": loan.status,
            "start_date": loan.start_date,
            "initial_amount": round_money(loan.initial_amount),
            "saldo": saldo,
            "tasa": Decimal(str(loan.interest_rate)),
            "interes_pagado": tl.interes_pagado_total,
            "interes_pendiente": pendiente,
            "liquidable": saldo + (pendiente if incluir_interes else ZERO),
        })

    socios: list[dict] = []
    otros: list[dict] = []
    deuda_capital_total = ZERO
    interes_pendiente_total = ZERO

    for m in members:
        m_loans = loans_by_member.get(m.id, [])
        activos = [l for l in m_loans if l["status"] == "active"]
        deuda_capital = sum((l["saldo"] for l in activos), ZERO)
        interes_pend = sum((l["interes_pendiente"] for l in activos), ZERO)
        deuda_capital_total += deuda_capital
        interes_pendiente_total += interes_pend

        if not is_eligible(m):
            if activos:
                otros.append({
                    "member_id": m.id,
                    "name": m.name,
                    "tipo": "Externo" if m.member_type == "fuera" else "Interno sin cuota",
                    "deuda_capital": deuda_capital,
                    "interes_pendiente": interes_pend,
                    "loans": activos,
                })
            continue

        ahorro = savings.get(m.id, ZERO)
        deuda_total = deuda_capital + (interes_pend if incluir_interes else ZERO)
        instr = instrucciones.get(m.id, "por_definir")
        socios.append({
            "member_id": m.id,
            "name": m.name,
            "group": m.member_group.name if m.member_group else "Sin grupo",
            "cuota": round_money(m.quota_personal),
            "ahorro": ahorro,
            "pidio_prestamo": bool(m_loans),
            "n_prestamos": len(m_loans),
            "n_activos": len(activos),
            "monto_desembolsado": sum((l["initial_amount"] for l in m_loans), ZERO),
            "deuda_capital": deuda_capital,
            "interes_pendiente": interes_pend,
            "deuda_total": deuda_total,
            "cubre": ahorro >= deuda_total,
            "faltante": max(deuda_total - ahorro, ZERO),
            "devolver_si_compensa": max(ahorro - deuda_total, ZERO),
            "instruccion": instr if activos else "sin_deuda",
            "loans": m_loans,
        })

    socios.sort(key=lambda s: (s["name"].casefold(), s["member_id"]))
    otros.sort(key=lambda s: (s["name"].casefold(), s["member_id"]))

    ahorro_total = sum(
        (v for mid, v in savings.items() if mid in members_by_id), ZERO
    )
    return {
        "socios": socios,
        "otros_prestamos": otros,
        "ahorro_total": ahorro_total,
        "deuda_capital": deuda_capital_total,
        "interes_pendiente": interes_pendiente_total,
        "intereses_cobrados": round_money(intereses_cobrados),
        "liquidez": round_money(ahorro_total + intereses_cobrados_total - deuda_capital_total),
    }


# ── Reparto de rendimientos ────────────────────────────────────────────────────

def compute_reparto(
    socios: list[dict],
    total: Decimal,
    factor_sin_prestamo: Decimal,
    overrides: dict[int, Decimal],
) -> dict:
    """
    Reparte `total` entre los socios elegibles. Función pura (sin BD).

    `socios`: dicts con member_id, name, ahorro, pidio_prestamo.
    `overrides`: porcentajes fijados a mano; el resto se reparte en proporción
    al ahorro ponderado hasta completar 100 %.

    El redondeo es determinista: cada monto se trunca a centavos y los centavos
    sobrantes se asignan por mayor fracción (desempate: nombre, id), de modo que
    la suma coincide exactamente con el total.
    """
    total = round_money(total)
    factor = Decimal(str(factor_sin_prestamo))
    ids = {s["member_id"] for s in socios}
    ov = {mid: Decimal(str(p)) for mid, p in overrides.items() if mid in ids}

    pesos = {
        s["member_id"]: s["ahorro"] * (Decimal("1") if s["pidio_prestamo"] else factor)
        for s in socios
    }
    suma_pesos = sum(pesos.values(), Decimal("0"))
    suma_pesos_libres = sum((w for mid, w in pesos.items() if mid not in ov), Decimal("0"))
    resto = CIEN - sum(ov.values(), Decimal("0"))

    pct: dict[int, Decimal] = {}
    pct_propuesto: dict[int, Decimal] = {}
    for s in socios:
        mid = s["member_id"]
        pct_propuesto[mid] = (pesos[mid] / suma_pesos * CIEN) if suma_pesos > 0 else Decimal("0")
        if mid in ov:
            pct[mid] = ov[mid]
        elif suma_pesos_libres > 0 and resto > 0:
            pct[mid] = pesos[mid] / suma_pesos_libres * resto
        else:
            pct[mid] = Decimal("0")

    total_pct = sum(pct.values(), Decimal("0"))
    cuadra = abs(total_pct - CIEN) <= PCT_TOLERANCIA
    errores: list[str] = []

    if total > 0:
        if not socios or sum((s["ahorro"] for s in socios), ZERO) <= 0:
            errores.append("Hay rendimiento por repartir pero ningún socio elegible tiene ahorro.")
        elif not cuadra:
            errores.append(
                f"Los porcentajes suman {total_pct.quantize(Decimal('0.0001'))} %; deben sumar 100 %."
            )

    # Montos: truncar a centavos y repartir los centavos sobrantes.
    exactos = {mid: total * p / CIEN for mid, p in pct.items()}
    montos = {mid: v.quantize(CENT, rounding=ROUND_DOWN) for mid, v in exactos.items()}
    if cuadra and total > 0 and socios:
        sobrante = int(((total - sum(montos.values(), ZERO)) / CENT).to_integral_value())
        names = {s["member_id"]: s["name"].casefold() for s in socios}
        orden = sorted(
            (mid for mid in montos if pct[mid] > 0),
            key=lambda mid: (-(exactos[mid] - montos[mid]), names[mid], mid),
        )
        i = 0
        while sobrante > 0 and orden:
            montos[orden[i % len(orden)]] += CENT
            sobrante -= 1
            i += 1

    # Rendimiento por peso ahorrado de cada grupo.
    grupos = {
        True: {"ahorro": ZERO, "rendimiento": ZERO, "n": 0},
        False: {"ahorro": ZERO, "rendimiento": ZERO, "n": 0},
    }
    for s in socios:
        g = grupos[bool(s["pidio_prestamo"])]
        g["ahorro"] += s["ahorro"]
        g["rendimiento"] += montos[s["member_id"]]
        g["n"] += 1
    for g in grupos.values():
        g["rend_pct"] = (
            (g["rendimiento"] / g["ahorro"] * CIEN).quantize(Decimal("0.0001"))
            if g["ahorro"] > 0 else Decimal("0")
        )

    con, sin = grupos[True], grupos[False]
    if total > 0 and con["ahorro"] > 0 and sin["ahorro"] > 0:
        # Comparación exacta (multiplicación cruzada) para no depender del redondeo.
        if not sin["rendimiento"] * con["ahorro"] < con["rendimiento"] * sin["ahorro"]:
            errores.append(
                "El rendimiento por peso ahorrado de quienes nunca pidieron préstamo "
                f"({sin['rend_pct']} %) debe ser menor que el de quienes sí pidieron "
                f"({con['rend_pct']} %). Baja el factor o ajusta los porcentajes."
            )

    return {
        "total": total,
        "total_pct": total_pct.quantize(PCT_Q),
        "errores": errores,
        "con_prestamo": con,
        "sin_prestamo": sin,
        "filas": {
            s["member_id"]: {
                "factor": Decimal("1") if s["pidio_prestamo"] else factor,
                "porcentaje": pct[s["member_id"]].quantize(PCT_Q),
                "porcentaje_propuesto": pct_propuesto[s["member_id"]].quantize(PCT_Q),
                "ajustado": s["member_id"] in ov,
                "rendimiento": montos[s["member_id"]],
            }
            for s in socios
        },
    }


# ── Resumen (pantalla = PDF = cierre) ──────────────────────────────────────────

def _live_resumen(db: Session, caja: CajaConfig) -> dict:
    borrador = get_borrador(db, caja.id)
    hoy = date.today()
    fecha_corte = borrador.fecha_corte or hoy
    incluir = bool(borrador.incluir_interes)
    factor = Decimal(str(borrador.factor_sin_prestamo))

    a = _build_analysis(db, caja, fecha_corte, incluir)
    socios = a["socios"]

    # Plan de compensación: solo socios con instrucción "ahorro" cuyo ahorro cubre.
    bloqueos: list[dict] = []
    interes_compensado = ZERO
    total_compensado = ZERO
    for s in socios:
        s["compensado"] = ZERO
        if s["n_activos"] == 0:
            continue
        if s["instruccion"] == "ahorro" and s["cubre"]:
            s["compensado"] = s["deuda_total"]
            total_compensado += s["deuda_total"]
            if incluir:
                interes_compensado += s["interes_pendiente"]
        elif s["instruccion"] == "ahorro":
            bloqueos.append({
                "tipo": "faltante", "member_id": s["member_id"], "name": s["name"],
                "monto": s["faltante"],
                "detalle": f"El ahorro no alcanza: faltan {_mxn(s['faltante'])}. "
                           "Registra el pago del faltante en Préstamos.",
            })
        elif s["instruccion"] == "separado":
            bloqueos.append({
                "tipo": "separado", "member_id": s["member_id"], "name": s["name"],
                "monto": s["deuda_capital"],
                "detalle": f"Pago por separado pendiente: {_mxn(s['deuda_capital'])} de capital.",
            })
        else:
            bloqueos.append({
                "tipo": "por_definir", "member_id": s["member_id"], "name": s["name"],
                "monto": s["deuda_capital"],
                "detalle": "Tiene préstamo activo y falta definir cómo lo paga.",
            })

    for o in a["otros_prestamos"]:
        bloqueos.append({
            "tipo": "externo", "member_id": o["member_id"], "name": o["name"],
            "monto": o["deuda_capital"],
            "detalle": f"{o['tipo']}: {len(o['loans'])} préstamo(s) por {_mxn(o['deuda_capital'])} sin liquidar.",
        })

    rendimiento_total = round_money(a["intereses_cobrados"] + interes_compensado)
    reparto = compute_reparto(socios, rendimiento_total, factor, _parse_overrides(borrador.overrides_json))

    for s in socios:
        fila = reparto["filas"][s["member_id"]]
        s.update(fila)
        s["ahorro_a_devolver"] = s["ahorro"] - s["compensado"]
        s["total_a_entregar"] = s["ahorro_a_devolver"] + fila["rendimiento"]
        s["instruccion_label"] = INSTRUCCION_LABEL.get(s["instruccion"], "—")

    drift = find_savings_drift(db, caja.id)
    ahorro_elegible = sum((s["ahorro"] for s in socios), ZERO)

    resumen = {
        "confirmado": False,
        "caja": {"id": caja.id, "name": caja.name},
        "fecha_corte": fecha_corte,
        "borrador": {
            "fecha_corte_fija": borrador.fecha_corte is not None,
            "factor_sin_prestamo": factor,
            "incluir_interes": incluir,
            "n_ajustes": sum(1 for s in socios if s["ajustado"]),
        },
        "totales": {
            "ahorro": a["ahorro_total"],
            "ahorro_elegible": ahorro_elegible,
            "deuda_capital": a["deuda_capital"],
            "interes_pendiente": a["interes_pendiente"],
            "liquidez": a["liquidez"],
            "intereses_cobrados": a["intereses_cobrados"],
            "interes_por_compensar": interes_compensado,
            "rendimiento_total": rendimiento_total,
            "total_compensado": total_compensado,
            "total_desembolsado": sum((s["monto_desembolsado"] for s in socios), ZERO),
            "total_a_devolver": sum((s["ahorro_a_devolver"] for s in socios), ZERO),
            "total_a_entregar": sum((s["total_a_entregar"] for s in socios), ZERO),
            "n_socios": len(socios),
            "n_pidieron": sum(1 for s in socios if s["pidio_prestamo"]),
            "n_con_activo": sum(1 for s in socios if s["n_activos"] > 0),
        },
        "reparto": {
            "total": reparto["total"],
            "total_pct": reparto["total_pct"],
            "errores": reparto["errores"],
            "con_prestamo": reparto["con_prestamo"],
            "sin_prestamo": reparto["sin_prestamo"],
        },
        "socios": socios,
        "otros_prestamos": a["otros_prestamos"],
        "bloqueos": bloqueos,
        "alertas_ahorro": drift,
        "puede_confirmar": not bloqueos and not reparto["errores"] and not drift,
    }
    payload = _jsonable(resumen)
    payload["hash"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return payload


def build_resumen(db: Session, caja_id: int) -> dict:
    """
    Resumen completo de la caja. Si ya hay cierre confirmado devuelve la copia
    congelada (las correcciones posteriores no la alteran); si no, el cálculo vivo.
    """
    caja = db.get(CajaConfig, caja_id)
    if not caja:
        raise CierreError("Caja no encontrada.", 404)

    cierre = get_cierre(db, caja_id)
    if cierre:
        payload = json.loads(cierre.snapshot_json)
        payload["confirmado"] = True
        payload["puede_confirmar"] = False
        payload["cierre"] = {
            "id": cierre.id,
            "fecha_corte": cierre.fecha_corte.isoformat(),
            "confirmado_en": cierre.created_at.isoformat(timespec="seconds"),
        }
        return payload
    return _live_resumen(db, caja)


# ── Simulador «¿Puedo prestar $X?» ─────────────────────────────────────────────

def loan_capacity(db: Session, caja_id: int, member_id: int) -> dict:
    """Máximo prestable a un socio: respaldo de su ahorro, limitado por el efectivo de la caja."""
    caja = db.get(CajaConfig, caja_id)
    if not caja:
        raise CierreError("Caja no encontrada.", 404)
    member = db.get(Member, member_id)
    if not member or member.caja_id != caja_id:
        raise CierreError("Socio no encontrado en esta caja.", 404)

    base = {
        "member_id": member.id, "member_name": member.name, "elegible": True,
        "ahorro": ZERO, "deuda_vigente": ZERO, "respaldo_libre": ZERO,
        "liquidez": ZERO, "maximo": ZERO, "limitante": None, "motivo_no_elegible": None,
    }
    if not is_eligible(member):
        base["elegible"] = False
        base["motivo_no_elegible"] = (
            "Solo aplica a socios internos activos con cuota quincenal mayor a $0."
        )
        return base

    savings = savings_by_member(db, caja_id)
    ahorro = savings.get(member.id, ZERO)
    activos = db.query(Loan).filter(Loan.caja_id == caja_id, Loan.status == "active").all()
    deuda_socio = sum((round_money(l.outstanding_balance) for l in activos if l.member_id == member.id), ZERO)
    saldo_insoluto = sum((round_money(l.outstanding_balance) for l in activos), ZERO)
    intereses = db.query(Transaction).join(Loan, Transaction.loan_id == Loan.id).filter(
        Loan.caja_id == caja_id, Transaction.transaction_type == "interest_payment"
    ).with_entities(Transaction.amount).all()
    intereses_total = sum((Decimal(str(r[0])) for r in intereses), Decimal("0"))
    member_ids = {mid for (mid,) in db.query(Member.id).filter(Member.caja_id == caja_id)}
    ahorro_total = sum((v for mid, v in savings.items() if mid in member_ids), ZERO)

    liquidez = max(round_money(ahorro_total + intereses_total - saldo_insoluto), ZERO)
    respaldo = max(ahorro - deuda_socio, ZERO)
    base.update({
        "ahorro": ahorro,
        "deuda_vigente": deuda_socio,
        "respaldo_libre": respaldo,
        "liquidez": liquidez,
        "maximo": min(respaldo, liquidez),
        "limitante": "liquidez" if liquidez < respaldo else "respaldo",
    })
    return base


def simulate_loan(db: Session, caja_id: int, member_id: int, monto: Decimal) -> dict:
    cap = loan_capacity(db, caja_id, member_id)
    monto = round_money(monto)
    cap["monto"] = monto
    if not cap["elegible"]:
        cap["aprobado"] = False
        cap["motivo"] = cap["motivo_no_elegible"]
    elif monto <= cap["maximo"]:
        cap["aprobado"] = True
        cap["motivo"] = (
            f"Sí se puede prestar {_mxn(monto)}. El máximo disponible para "
            f"{cap['member_name']} es {_mxn(cap['maximo'])}."
        )
    else:
        cap["aprobado"] = False
        if cap["limitante"] == "liquidez":
            cap["motivo"] = (
                f"No alcanza el efectivo de la caja: hay {_mxn(cap['liquidez'])} disponibles. "
                f"El máximo que se puede prestar es {_mxn(cap['maximo'])}."
            )
        elif cap["deuda_vigente"] > 0:
            cap["motivo"] = (
                f"Supera su respaldo: ahorro {_mxn(cap['ahorro'])} menos deuda vigente "
                f"{_mxn(cap['deuda_vigente'])}. El máximo es {_mxn(cap['maximo'])}."
            )
        else:
            cap["motivo"] = (
                f"Supera su ahorro acumulado de {_mxn(cap['ahorro'])}. "
                f"El máximo es {_mxn(cap['maximo'])}."
            )
    return cap


def assert_loan_within_capacity(db: Session, caja_id: int, member_id: int, monto: Decimal) -> None:
    """Revalida el límite dentro de la operación de préstamo (llamar tras `lock_for_write`)."""
    sim = simulate_loan(db, caja_id, member_id, monto)
    if not sim["aprobado"]:
        raise CierreError(sim["motivo"], 409, {"maximo": format(sim["maximo"], "f")})


# ── Confirmación del cierre ────────────────────────────────────────────────────

def confirm_cierre(db: Session, caja_id: int, idempotency_key: str, expected_hash: str) -> tuple[dict, bool]:
    """
    Confirma el cierre en una sola transacción: compensa ahorro ↔ deuda, guarda
    el corte, los movimientos y el reparto. Devuelve (resumen, creado).

    Reintentable: la misma `idempotency_key` devuelve el cierre ya guardado sin
    volver a aplicar cargos ni pagos.
    """
    if not idempotency_key or len(idempotency_key) > 64:
        raise CierreError("Llave de idempotencia inválida.", 422)

    lock_for_write(db)

    existing = get_cierre(db, caja_id)
    if existing:
        if existing.idempotency_key == idempotency_key:
            return build_resumen(db, caja_id), False
        raise CierreError("Esta caja ya tiene un cierre confirmado.", 409)

    caja = db.get(CajaConfig, caja_id)
    if not caja:
        raise CierreError("Caja no encontrada.", 404)

    # Revalidar saldos: se recalcula todo dentro del candado de escritura.
    resumen = _live_resumen(db, caja)
    if resumen["hash"] != expected_hash:
        raise CierreError(
            "Los saldos cambiaron desde que abriste la vista previa. Revísala de nuevo antes de confirmar.",
            409,
        )
    if resumen["alertas_ahorro"]:
        raise CierreError("Hay saldos de ahorro sin conciliar. Concilia antes de cerrar.", 422)
    if resumen["bloqueos"]:
        raise CierreError(
            "No se puede cerrar: todos los préstamos deben quedar liquidados.",
            422, {"bloqueos": resumen["bloqueos"]},
        )
    if resumen["reparto"]["errores"]:
        raise CierreError(resumen["reparto"]["errores"][0], 422)

    fecha_corte = date.fromisoformat(resumen["fecha_corte"])
    incluir = resumen["borrador"]["incluir_interes"]
    tot = resumen["totales"]

    cierre = CierreCaja(
        caja_id=caja_id,
        fecha_corte=fecha_corte,
        idempotency_key=idempotency_key,
        factor_sin_prestamo=Decimal(resumen["borrador"]["factor_sin_prestamo"]),
        incluir_interes=incluir,
        total_ahorro=Decimal(tot["ahorro_elegible"]),
        total_rendimiento=Decimal(tot["rendimiento_total"]),
        total_compensado=Decimal(tot["total_compensado"]),
        total_a_entregar=Decimal(tot["total_a_entregar"]),
        snapshot_json=json.dumps(resumen, ensure_ascii=False),
    )
    db.add(cierre)

    try:
        db.flush()

        compensar = {s["member_id"]: s for s in resumen["socios"] if Decimal(s["compensado"]) > 0}
        loans = {
            l.id: l for l in db.query(Loan).filter(
                Loan.caja_id == caja_id, Loan.status == "active",
                Loan.member_id.in_(compensar.keys()),
            )
        } if compensar else {}
        members = {
            m.id: m for m in db.query(Member).filter(Member.id.in_(compensar.keys()))
        } if compensar else {}

        for mid, s in compensar.items():
            aplicado = ZERO
            for l in s["loans"]:
                if l["status"] != "active":
                    continue
                loan = loans[l["id"]]
                interes = Decimal(l["interes_pendiente"]) if incluir else ZERO
                capital = Decimal(l["saldo"])
                # Los saldos no pudieron cambiar: se validó el hash bajo el candado.
                assert round_money(loan.outstanding_balance) == capital

                for tipo, ttype, monto in (
                    ("pago_interes", "interest_payment", interes),
                    ("pago_capital", "capital_payment", capital),
                ):
                    if monto <= 0:
                        continue
                    txn = Transaction(
                        loan_id=loan.id, member_id=mid, amount=monto,
                        transaction_date=fecha_corte, transaction_type=ttype,
                        notes=f"{NOTA_COMPENSACION} #{cierre.id} · liquidado con ahorro",
                    )
                    db.add(txn)
                    db.flush()
                    db.add(CierreMovimiento(
                        cierre_id=cierre.id, member_id=mid, loan_id=loan.id,
                        tipo=tipo, monto=monto, transaction_id=txn.id,
                    ))
                    aplicado += monto
                loan.outstanding_balance = ZERO
                loan.status = "paid"

            assert aplicado == Decimal(s["compensado"])
            apo = Aportacion(
                member_id=mid, caja_id=caja_id, monto=-aplicado,
                fecha_aportacion=fecha_corte, quincena=None,
                notas=f"{NOTA_COMPENSACION} #{cierre.id} · ahorro aplicado a préstamos",
            )
            db.add(apo)
            db.flush()
            db.add(CierreMovimiento(
                cierre_id=cierre.id, member_id=mid, loan_id=None,
                tipo="cargo_ahorro", monto=aplicado, aportacion_id=apo.id,
            ))
            sync_member_savings(db, members[mid])

        for s in resumen["socios"]:
            db.add(CierreSocio(
                cierre_id=cierre.id,
                member_id=s["member_id"],
                member_name=s["name"],
                ahorro_aportado=Decimal(s["ahorro"]),
                pidio_prestamo=s["pidio_prestamo"],
                monto_desembolsado=Decimal(s["monto_desembolsado"]),
                deuda_al_corte=Decimal(s["deuda_total"]),
                compensado=Decimal(s["compensado"]),
                ahorro_a_devolver=Decimal(s["ahorro_a_devolver"]),
                factor=Decimal(s["factor"]),
                porcentaje=Decimal(s["porcentaje"]),
                rendimiento=Decimal(s["rendimiento"]),
                total_a_entregar=Decimal(s["total_a_entregar"]),
            ))

        db.commit()
    except IntegrityError:
        # Otra petición confirmó primero: si fue la misma llave, es un reintento.
        db.rollback()
        existing = get_cierre(db, caja_id)
        if existing and existing.idempotency_key == idempotency_key:
            return build_resumen(db, caja_id), False
        raise CierreError("Esta caja ya tiene un cierre confirmado.", 409)
    except Exception:
        db.rollback()
        raise

    return build_resumen(db, caja_id), True
