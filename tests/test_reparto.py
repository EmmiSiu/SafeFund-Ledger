"""Reparto de rendimientos: función pura, sin base de datos."""
from decimal import Decimal

from app.services.cierre import compute_reparto

D = Decimal


def socio(mid, name, ahorro, pidio):
    return {"member_id": mid, "name": name, "ahorro": D(ahorro), "pidio_prestamo": pidio}


SOCIOS = [
    socio(1, "Ana", "10000.00", True),
    socio(2, "Beto", "5000.00", True),
    socio(3, "Caro", "8000.00", False),
    socio(4, "Dany", "3333.33", False),
]


def test_formula_ahorro_por_factor():
    r = compute_reparto(SOCIOS, D("1000.00"), D("0.5"), {})
    # pesos: 10000, 5000, 4000, 1666.665 → Σ = 20666.665
    assert r["errores"] == []
    assert r["filas"][1]["porcentaje"] == (D("10000") / D("20666.665") * 100).quantize(D("0.000001"))
    assert r["filas"][3]["factor"] == D("0.5")
    assert r["filas"][1]["factor"] == D("1")


def test_montos_suman_exactamente_el_total():
    for total in ("1000.00", "142277.27", "0.01", "0.03", "99999.99"):
        r = compute_reparto(SOCIOS, D(total), D("0.5"), {})
        assert sum(f["rendimiento"] for f in r["filas"].values()) == D(total), total


def test_redondeo_determinista():
    a = compute_reparto(SOCIOS, D("100.01"), D("0.37"), {})
    b = compute_reparto(list(reversed(SOCIOS)), D("100.01"), D("0.37"), {})
    assert {k: v["rendimiento"] for k, v in a["filas"].items()} == {
        k: v["rendimiento"] for k, v in b["filas"].items()
    }


def test_centavo_sobrante_desempata_por_nombre():
    iguales = [socio(9, "Zoe", "100.00", True), socio(7, "Abel", "100.00", True), socio(8, "Mia", "100.00", True)]
    r = compute_reparto(iguales, D("0.01"), D("0.5"), {})
    assert r["filas"][7]["rendimiento"] == D("0.01")  # Abel va primero
    assert r["filas"][8]["rendimiento"] == D("0.00")
    assert r["filas"][9]["rendimiento"] == D("0.00")


def test_grupo_sin_prestamo_rinde_menos_por_peso():
    r = compute_reparto(SOCIOS, D("1000.00"), D("0.5"), {})
    assert r["sin_prestamo"]["rend_pct"] < r["con_prestamo"]["rend_pct"]
    assert r["errores"] == []


def test_factor_uno_viola_la_regla_de_rendimiento():
    r = compute_reparto(SOCIOS, D("1000.00"), D("1"), {})
    assert any("debe ser menor" in e for e in r["errores"])


def test_ajuste_manual_reparte_el_resto_hasta_100():
    r = compute_reparto(SOCIOS, D("1000.00"), D("0.5"), {1: D("60")})
    assert r["filas"][1]["porcentaje"] == D("60.000000")
    assert r["filas"][1]["ajustado"] is True
    assert abs(r["total_pct"] - 100) < D("0.0001")
    assert sum(f["rendimiento"] for f in r["filas"].values()) == D("1000.00")
    assert r["filas"][1]["rendimiento"] == D("600.00")


def test_ajustes_que_no_suman_100_se_rechazan():
    todos = {1: D("50"), 2: D("20"), 3: D("10"), 4: D("10")}  # 90 %
    r = compute_reparto(SOCIOS, D("1000.00"), D("0.5"), todos)
    assert any("deben sumar 100" in e for e in r["errores"])

    excedido = {1: D("80"), 2: D("40")}  # 120 %
    r = compute_reparto(SOCIOS, D("1000.00"), D("0.5"), excedido)
    assert any("deben sumar 100" in e for e in r["errores"])


def test_ajuste_que_favorece_a_quien_no_pidio_se_rechaza():
    r = compute_reparto(SOCIOS, D("1000.00"), D("0.5"), {3: D("70")})
    assert any("debe ser menor" in e for e in r["errores"])


def test_rendimiento_cero_no_exige_nada():
    r = compute_reparto(SOCIOS, D("0.00"), D("1"), {1: D("5")})
    assert r["errores"] == []
    assert all(f["rendimiento"] == D("0.00") for f in r["filas"].values())


def test_un_solo_grupo_no_aplica_regla_de_rendimiento():
    solo_con = [s for s in SOCIOS if s["pidio_prestamo"]]
    r = compute_reparto(solo_con, D("500.00"), D("1"), {})
    assert r["errores"] == []
    assert sum(f["rendimiento"] for f in r["filas"].values()) == D("500.00")


def test_rendimiento_sin_ahorro_es_error():
    vacios = [socio(1, "Ana", "0.00", True)]
    r = compute_reparto(vacios, D("10.00"), D("0.5"), {})
    assert r["errores"]
