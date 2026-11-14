from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services.pdf_generator import (
    html_to_pdf,
    render_cierre_caja,
    render_estado_cuenta,
    render_reporte_caja,
)

router = APIRouter(prefix="/reports", tags=["Reports"])

# Barra que solo se ve en pantalla cuando el reporte se entrega como HTML
# (motor de PDF no disponible): permite imprimir o guardar como PDF desde el navegador.
_PRINT_BAR = """
<div class="__printbar" style="position:sticky;top:0;z-index:10;display:flex;gap:12px;align-items:center;
     justify-content:space-between;padding:10px 16px;margin-bottom:12px;background:#0f172a;color:#e2e8f0;
     font:13px/1.4 system-ui,sans-serif;border-radius:8px;">
  <span>Vista para imprimir — el motor de PDF no está instalado en este equipo.</span>
  <button onclick="window.print()" style="padding:6px 14px;border:0;border-radius:8px;background:#10b981;
          color:#fff;font-weight:600;cursor:pointer;">Imprimir / Guardar como PDF</button>
</div>
<style>@media print { .__printbar { display: none !important; } }</style>
"""


def _respond(render: Callable[[], str], filename: str, formato: str) -> Response:
    """
    Entrega el reporte como PDF. Si WeasyPrint no tiene sus librerías nativas
    (típico en Windows sin GTK) o se pide `formato=html`, entrega el mismo
    reporte como HTML imprimible en lugar de fallar con un error 500.
    """
    try:
        html = render()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if formato != "html":
        try:
            return Response(
                content=html_to_pdf(html),
                media_type="application/pdf",
                headers={"Content-Disposition": f"inline; filename={filename}.pdf"},
            )
        except (OSError, ImportError):
            pass  # sin motor de PDF: caer a HTML imprimible

    return HTMLResponse(html.replace("<body>", "<body>" + _PRINT_BAR, 1))


@router.get("/estado-cuenta/{member_id}")
def estado_cuenta_pdf(
    member_id: int,
    formato: str = Query("pdf", pattern="^(pdf|html)$"),
    db: Session = Depends(get_db),
):
    return _respond(lambda: render_estado_cuenta(member_id, db), f"estado_cuenta_{member_id}", formato)


@router.get("/caja/{caja_id}")
def reporte_caja_pdf(
    caja_id: int,
    formato: str = Query("pdf", pattern="^(pdf|html)$"),
    db: Session = Depends(get_db),
):
    return _respond(lambda: render_reporte_caja(caja_id, db), f"reporte_caja_{caja_id}", formato)


@router.get("/cierre/{caja_id}")
def cierre_caja_pdf(
    caja_id: int,
    formato: str = Query("pdf", pattern="^(pdf|html)$"),
    db: Session = Depends(get_db),
):
    """Reporte de cierre: preliminar antes de confirmar, definitivo desde el corte guardado."""
    return _respond(lambda: render_cierre_caja(caja_id, db), f"cierre_caja_{caja_id}", formato)
