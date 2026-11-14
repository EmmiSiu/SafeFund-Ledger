"""
Flujo completo de análisis → simulador → cierre, vía API.

Caja de prueba:
  Ana    ahorro 10,000 · préstamo 4,000 activo       → el ahorro cubre
  Beto   ahorro  3,000 · préstamos 2,000 + 2,500     → insuficiente (faltan 1,500)
  Caro   ahorro  5,000 · préstamo 5,000              → cobertura exacta
  Dany   ahorro  8,000 (2,000 saldo inicial importado) · nunca pidió
  Eli    cuota 0                                     → no elegible
  Xavi   externo · préstamo 6,000                    → bloquea el cierre
"""
from datetime import date
from decimal import Decimal

import pytest

from app.models.base import Aportacion, CierreMovimiento, Loan, Member, Transaction

D = Decimal
HOY = date.today().isoformat()
API = "/api/v1"


@pytest.fixture()
def caja(client):
    r = client.post(f"{API}/cajas/", json={
        "name": "PRUEBA", "interest_rate_internal": "0.05", "interest_rate_external": "0.08",
        "total_quincenas": 24, "quota_amount": "0", "start_date": "2026-01-01",
    })
    assert r.status_code in (200, 201), r.text
    caja_id = r.json()["id"]
    g = client.post(f"{API}/groups/", json={"name": "G1", "caja_id": caja_id})
    assert g.status_code in (200, 201), g.text
    gid = g.json()["id"]

    def socio(name, cuota, tipo="dentro"):
        r = client.post(f"{API}/members/", json={
            "name": name, "group_id": gid, "caja_id": caja_id,
            "member_type": tipo, "quota_personal": cuota,
        })
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def aporta(mid, monto):
        r = client.post(f"{API}/aportaciones/", json={
            "member_id": mid, "caja_id": caja_id, "monto": monto, "fecha_aportacion": HOY,
        })
        assert r.status_code == 201, r.text

    def presta(mid, monto, tipo="internal", **extra):
        r = client.post(f"{API}/loans/", json={
            "member_id": mid, "caja_id": caja_id, "initial_amount": monto,
            "loan_type": tipo, "start_date": HOY, **extra,
        })
        return r

    ids = {
        "ana": socio("Ana", "500"), "beto": socio("Beto", "500"), "caro": socio("Caro", "500"),
        "dany": socio("Dany", "500"), "eli": socio("Eli", "0"), "xavi": socio("Xavi", "0", "fuera"),
    }
    aporta(ids["ana"], "10000"); aporta(ids["beto"], "3000"); aporta(ids["caro"], "5000")
    aporta(ids["dany"], "6000")
    # Saldo inicial importado (transacción "savings", como el importador de Excel)
    r = client.post(f"{API}/transactions/savings", json={
        "member_id": ids["dany"], "amount": "2000", "transaction_date": HOY,
    })
    assert r.status_code == 201, r.text

    ids["l_ana"] = presta(ids["ana"], "4000").json()["id"]
    ids["l_beto1"] = presta(ids["beto"], "2000").json()["id"]
    ids["l_beto2"] = presta(ids["beto"], "2500").json()["id"]
    ids["l_caro"] = presta(ids["caro"], "5000").json()["id"]
    ids["l_xavi"] = presta(ids["xavi"], "6000", "external").json()["id"]
    # Interés cobrado: rendimiento a repartir
    r = client.post(f"{API}/loans/{ids['l_xavi']}/interes-extra", json={
        "monto": "1000.01", "fecha_pago": HOY, "motivo": "interés del ciclo",
    })
    assert r.status_code == 201, r.text
    ids["caja"] = caja_id
    ids["presta"] = presta
    return ids


def resumen(client, caja):
    r = client.get(f"{API}/cierre/resumen", params={"caja_id": caja["caja"]})
    assert r.status_code == 200, r.text
    return r.json()


def fila(res, name):
    return next(s for s in res["socios"] if s["name"] == name)


# ── Análisis ───────────────────────────────────────────────────────────────────

def test_analisis_elegibles_y_cobertura(client, caja):
    res = resumen(client, caja)
    assert [s["name"] for s in res["socios"]] == ["Ana", "Beto", "Caro", "Dany"]  # alfabético, sin Eli ni Xavi

    ana, beto, caro, dany = (fila(res, n) for n in ("Ana", "Beto", "Caro", "Dany"))
    assert (ana["cubre"], D(ana["faltante"]), D(ana["devolver_si_compensa"])) == (True, D("0"), D("6000"))
    # El ahorro se aplica UNA vez al conjunto de préstamos: 4,500 − 3,000
    assert (beto["cubre"], D(beto["faltante"]), D(beto["devolver_si_compensa"])) == (False, D("1500"), D("0"))
    assert beto["n_activos"] == 2 and D(beto["monto_desembolsado"]) == D("4500")
    assert (caro["cubre"], D(caro["faltante"]), D(caro["devolver_si_compensa"])) == (True, D("0"), D("0"))
    # Saldo inicial importado + aportaciones, sin duplicar
    assert D(dany["ahorro"]) == D("8000") and dany["pidio_prestamo"] is False

    t = res["totales"]
    assert D(t["ahorro"]) == D("26000")
    assert D(t["deuda_capital"]) == D("19500")
    assert D(t["intereses_cobrados"]) == D("1000.01")
    assert D(t["liquidez"]) == D("26000") + D("1000.01") - D("19500")
    assert res["alertas_ahorro"] == []


def test_consulta_no_escribe(client, caja, db):
    m = db.get(Member, caja["dany"])
    m.savings_balance = D("1.00")  # saldo guardado descuadrado a propósito
    db.commit()
    client.get(f"/members?caja_id={caja['caja']}")
    res = resumen(client, caja)
    db.expire_all()
    assert db.get(Member, caja["dany"]).savings_balance == D("1.00")  # las vistas no corrigen
    assert len(res["alertas_ahorro"]) == 1 and res["puede_confirmar"] is False
    # La pantalla muestra el saldo real calculado desde movimientos
    assert D(fila(res, "Dany")["ahorro"]) == D("8000")

    r = client.post(f"{API}/cierre/reconciliar-ahorros", params={"caja_id": caja["caja"]})
    assert len(r.json()["corregidos"]) == 1
    assert resumen(client, caja)["alertas_ahorro"] == []


def test_interes_pendiente_entra_en_la_deuda(client, caja, db):
    loan = db.get(Loan, caja["l_ana"])
    loan.start_date = date(2026, 1, 1)
    db.commit()
    con = fila(resumen(client, caja), "Ana")
    assert D(con["interes_pendiente"]) > 0
    assert D(con["deuda_total"]) == D("4000") + D(con["interes_pendiente"])

    r = client.put(f"{API}/cierre/borrador", json={"caja_id": caja["caja"], "incluir_interes": False})
    sin = fila(r.json(), "Ana")
    assert D(sin["deuda_total"]) == D("4000")


# ── Simulador ──────────────────────────────────────────────────────────────────

def simular(client, caja, quien, monto):
    r = client.post(f"{API}/cierre/simular", json={
        "caja_id": caja["caja"], "member_id": caja[quien], "monto": monto,
    })
    assert r.status_code == 200, r.text
    return r.json()


def test_simulador_tope_por_respaldo(client, caja):
    s = simular(client, caja, "ana", "6000")  # ahorro 10,000 − deuda 4,000
    assert s["aprobado"] is True and D(s["maximo"]) == D("6000")
    s = simular(client, caja, "ana", "6000.01")
    assert s["aprobado"] is False and s["limitante"] == "respaldo" and "$6,000.00" in s["motivo"]
    s = simular(client, caja, "beto", "1")  # deuda mayor al ahorro
    assert s["aprobado"] is False and D(s["maximo"]) == D("0")


def test_simulador_tope_por_liquidez(client, caja):
    # Liquidez 7,500.01; Dany tiene respaldo de 8,000
    s = simular(client, caja, "dany", "8000")
    assert s["aprobado"] is False and s["limitante"] == "liquidez"
    assert D(s["maximo"]) == D("7500.01") and "efectivo" in s["motivo"]


def test_simulador_exige_socio_interno_con_cuota(client, caja):
    assert simular(client, caja, "eli", "100")["aprobado"] is False
    assert simular(client, caja, "xavi", "100")["elegible"] is False


def test_prestamo_revalida_limite_al_confirmar(client, caja):
    presta = caja["presta"]
    assert presta(caja["ana"], "6000.01", validar_respaldo=True).status_code == 409
    assert presta(caja["ana"], "6000", validar_respaldo=True).status_code == 201
    # El mismo respaldo no se puede usar dos veces
    r = presta(caja["ana"], "1", validar_respaldo=True)
    assert r.status_code == 409 and "máximo" in r.json()["detail"]
    # El alta normal de préstamos (sin simulador) conserva su comportamiento
    assert presta(caja["xavi"], "100", "external").status_code == 201


# ── Cierre ─────────────────────────────────────────────────────────────────────

def instruir(client, caja, quien, instr):
    r = client.put(f"{API}/cierre/instruccion", json={
        "caja_id": caja["caja"], "member_ids": [caja[q] for q in quien], "instruccion": instr,
    })
    assert r.status_code == 200, r.text
    return r.json()


def confirmar(client, caja, key, res):
    return client.post(f"{API}/cierre/confirmar", json={
        "caja_id": caja["caja"], "idempotency_key": key, "expected_hash": res["hash"],
    })


def abonar(client, loan_id, monto):
    r = client.post(f"{API}/loans/{loan_id}/abono-capital", json={"monto": monto, "fecha_abono": HOY})
    assert r.status_code == 201, r.text


def dejar_listo(client, caja):
    """Resuelve todos los pendientes: Beto paga su faltante, el externo liquida."""
    instruir(client, caja, ["ana", "beto", "caro"], "ahorro")
    abonar(client, caja["l_beto1"], "1500")
    abonar(client, caja["l_xavi"], "6000")
    return resumen(client, caja)


def test_cierre_bloqueado_mientras_haya_prestamos(client, caja):
    res = resumen(client, caja)
    tipos = sorted(b["tipo"] for b in res["bloqueos"])
    assert tipos == ["externo", "por_definir", "por_definir", "por_definir"]
    assert res["puede_confirmar"] is False

    res = instruir(client, caja, ["ana", "caro"], "ahorro")
    res = instruir(client, caja, ["beto"], "ahorro")
    assert sorted(b["tipo"] for b in res["bloqueos"]) == ["externo", "faltante"]
    r = confirmar(client, caja, "llave-0001", res)
    assert r.status_code == 422
    assert len(r.json()["detail"]["bloqueos"]) == 2

    res = instruir(client, caja, ["beto"], "separado")
    assert sorted(b["tipo"] for b in res["bloqueos"]) == ["externo", "separado"]


def test_cierre_atomico_e_idempotente(client, caja, db):
    res = dejar_listo(client, caja)
    assert res["bloqueos"] == [] and res["puede_confirmar"] is True
    t = res["totales"]
    # Ana 4,000 + Beto 3,000 (tras pagar 1,500) + Caro 5,000
    assert D(t["total_compensado"]) == D("12000")
    assert D(t["rendimiento_total"]) == D("1000.01")
    assert sum(D(s["rendimiento"]) for s in res["socios"]) == D("1000.01")
    # El reparto usa el ahorro ANTES de compensar
    assert D(fila(res, "Beto")["ahorro"]) == D("3000")

    n_txn = db.query(Transaction).count()
    r = confirmar(client, caja, "llave-0001", res)
    assert r.status_code == 200, r.text
    assert r.json()["creado"] is True
    db.expire_all()

    assert db.query(Loan).filter(Loan.status == "active").count() == 0
    assert db.get(Member, caja["ana"]).savings_balance == D("6000")
    assert db.get(Member, caja["beto"]).savings_balance == D("0")
    assert db.get(Member, caja["caro"]).savings_balance == D("0")
    assert db.get(Member, caja["dany"]).savings_balance == D("8000")
    # Movimientos auditables: 4 pagos de capital + 3 cargos de ahorro
    assert db.query(Transaction).count() == n_txn + 4
    assert db.query(CierreMovimiento).count() == 7
    cargos = db.query(Aportacion).filter(Aportacion.monto < 0).all()
    assert sorted(a.monto for a in cargos) == [D("-5000"), D("-4000"), D("-3000")]

    # Reintento con la misma llave: no duplica nada
    r2 = confirmar(client, caja, "llave-0001", res)
    assert r2.status_code == 200 and r2.json()["creado"] is False
    db.expire_all()
    assert db.query(Transaction).count() == n_txn + 4
    assert db.query(CierreMovimiento).count() == 7
    # Otra llave: la caja ya está cerrada
    assert confirmar(client, caja, "llave-0002", res).status_code == 409


def test_cierre_revalida_saldos(client, caja):
    res = dejar_listo(client, caja)
    # Entra un movimiento después de abrir la vista previa
    client.post(f"{API}/aportaciones/", json={
        "member_id": caja["dany"], "caja_id": caja["caja"], "monto": "500", "fecha_aportacion": HOY,
    })
    r = confirmar(client, caja, "llave-0001", res)
    assert r.status_code == 409 and "cambiaron" in r.json()["detail"]
    assert resumen(client, caja)["confirmado"] is False


def test_cierre_confirmado_no_cambia_con_correcciones(client, caja):
    res = dejar_listo(client, caja)
    assert confirmar(client, caja, "llave-0001", res).status_code == 200
    antes = resumen(client, caja)
    assert antes["confirmado"] is True and antes["hash"] == res["hash"]

    # Corrección posterior al cierre
    client.post(f"{API}/aportaciones/", json={
        "member_id": caja["dany"], "caja_id": caja["caja"], "monto": "999", "fecha_aportacion": HOY,
    })
    despues = resumen(client, caja)
    assert despues["socios"] == antes["socios"] and despues["totales"] == antes["totales"]
    # El borrador queda congelado
    r = client.put(f"{API}/cierre/borrador", json={"caja_id": caja["caja"], "factor_sin_prestamo": "0.9"})
    assert r.status_code == 409


def test_cierre_con_rendimiento_cero(client, caja, db):
    db.query(Transaction).filter(Transaction.transaction_type == "interest_payment").delete()
    db.commit()
    res = dejar_listo(client, caja)
    assert D(res["totales"]["rendimiento_total"]) == D("0")
    assert res["reparto"]["errores"] == []
    assert confirmar(client, caja, "llave-0001", res).status_code == 200


def test_cierre_compensa_interes_pendiente_y_lo_reparte(client, caja, db):
    loan = db.get(Loan, caja["l_ana"])
    loan.start_date = date(2026, 1, 1)
    db.commit()
    res = dejar_listo(client, caja)
    pendiente = D(fila(res, "Ana")["interes_pendiente"])
    assert pendiente > 0
    assert D(res["totales"]["rendimiento_total"]) == D("1000.01") + pendiente
    assert confirmar(client, caja, "llave-0001", res).status_code == 200
    db.expire_all()
    assert db.get(Member, caja["ana"]).savings_balance == D("10000") - D("4000") - pendiente


def test_ajuste_manual_de_porcentajes(client, caja):
    dejar_listo(client, caja)
    r = client.put(f"{API}/cierre/borrador", json={
        "caja_id": caja["caja"], "overrides": {str(caja["ana"]): "50"},
    })
    res = r.json()
    assert D(fila(res, "Ana")["porcentaje"]) == D("50") and fila(res, "Ana")["ajustado"] is True
    assert sum(D(s["rendimiento"]) for s in res["socios"]) == D("1000.01")
    assert res["borrador"]["n_ajustes"] == 1

    # Favorecer a quien nunca pidió préstamo bloquea el cierre
    r = client.put(f"{API}/cierre/borrador", json={
        "caja_id": caja["caja"], "limpiar_overrides": True, "overrides": {str(caja["dany"]): "80"},
    })
    res = r.json()
    assert res["reparto"]["errores"] and res["puede_confirmar"] is False
    assert confirmar(client, caja, "llave-0001", res).status_code == 422


# ── Exportación ────────────────────────────────────────────────────────────────

def test_reporte_coincide_con_pantalla_y_cierre(client, caja):
    res = dejar_listo(client, caja)
    html = client.get(f"{API}/reports/cierre/{caja['caja']}", params={"formato": "html"}).text
    assert "PRELIMINAR" in html and res["hash"][:12] in html
    for s in res["socios"]:  # todos los elegibles, sin importar filtros de pantalla
        assert s["name"] in html
    assert f"${D(res['totales']['rendimiento_total']):,.2f}" in html

    assert confirmar(client, caja, "llave-0001", res).status_code == 200
    final = client.get(f"{API}/reports/cierre/{caja['caja']}", params={"formato": "html"}).text
    assert "DEFINITIVO" in final and "PRELIMINAR" not in final
    assert res["hash"][:12] in final  # mismo cálculo que la vista previa confirmada


def test_paginas_y_reportes_existentes_siguen_funcionando(client, caja):
    cid = caja["caja"]
    for url in ("/dashboard", "/loans", "/members", "/groups", "/quincenas", "/bi-dashboard", "/analisis"):
        assert client.get(url, params={"caja_id": cid}).status_code == 200, url
    assert client.get("/settings").status_code == 200
    for url in (f"{API}/reports/caja/{cid}", f"{API}/reports/estado-cuenta/{caja['ana']}"):
        assert client.get(url, params={"formato": "html"}).status_code == 200, url
