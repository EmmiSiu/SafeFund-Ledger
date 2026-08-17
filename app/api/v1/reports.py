from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services.pdf_generator import generate_estado_cuenta, generate_reporte_caja

router = APIRouter(prefix="/reports", tags=["Reports"])


_PDF_ENGINE_ERROR = (
    "No se pudo generar el PDF: faltan las librerías nativas que necesita WeasyPrint "
    "(Pango/GObject/Cairo) en este entorno. Si corres la app localmente en Windows, "
    "instala el runtime de GTK3, o levanta el proyecto con `docker compose up` "
    "(el Dockerfile ya incluye esas dependencias)."
)


@router.get("/estado-cuenta/{member_id}")
def estado_cuenta_pdf(member_id: int, db: Session = Depends(get_db)):
    try:
        pdf = generate_estado_cuenta(member_id, db)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except OSError:
        raise HTTPException(status_code=500, detail=_PDF_ENGINE_ERROR)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=estado_cuenta_{member_id}.pdf"},
    )


@router.get("/caja/{caja_id}")
def reporte_caja_pdf(caja_id: int, db: Session = Depends(get_db)):
    try:
        pdf = generate_reporte_caja(caja_id, db)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except OSError:
        raise HTTPException(status_code=500, detail=_PDF_ENGINE_ERROR)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=reporte_caja_{caja_id}.pdf"},
    )
