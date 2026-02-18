from html import escape
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import crud
import schemas
from database import get_db
from dependencies import require_permission
from services import email as email_service
from services import pdf_utils

router = APIRouter(
    prefix="/reports",
    tags=["reports"],
)


@router.post("/email")
def send_report_email(
    payload: schemas.ReportEmailRequest,
    db: Session = Depends(get_db),
    _: object = Depends(require_permission("reports.view")),
):
    if not payload.recipients:
        raise HTTPException(status_code=400, detail="Debe indicar al menos un destinatario")
    if not payload.document_html:
        raise HTTPException(status_code=400, detail="El HTML del reporte es requerido")

    settings = crud.get_pos_settings(db)

    if payload.message:
        html_body = f"<p>{escape(payload.message)}</p>"
    else:
        html_body = "<p>Adjuntamos su reporte generado desde Kensar.</p>"

    attachments = []
    if payload.attach_pdf:
        if not pdf_utils.can_render_html_pdf():
            raise HTTPException(
                status_code=503,
                detail=(
                    "El servidor no tiene habilitada la generacion de PDF HTML "
                    "(dependencias de WeasyPrint faltantes)."
                ),
            )
        pdf_bytes = pdf_utils.build_pdf_from_html(payload.subject or "Reporte Kensar", payload.document_html)
        attachments.append(
            (
                "reporte_kensar.pdf",
                pdf_bytes,
                "application/pdf",
            )
        )

    try:
        email_service.send_email(
            recipients=payload.recipients,
            subject=payload.subject or "Reporte Kensar",
            html_body=html_body,
            attachments=attachments,
            smtp_config=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except email_service.EmailDeliveryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"status": "sent"}
