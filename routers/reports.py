from html import escape
from io import BytesIO
from datetime import datetime
import re
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
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


@router.post("/export/xlsx")
def export_report_xlsx(
    payload: schemas.ReportExportRequest,
    _: object = Depends(require_permission("reports.view")),
):
    if not payload.table.columns:
        raise HTTPException(status_code=400, detail="La tabla del reporte no tiene columnas.")

    workbook = Workbook()
    try:
        workbook.calculation_properties.fullCalcOnLoad = True
    except AttributeError:
        pass

    sheet = workbook.active
    sheet.title = "Reporte"

    total_columns = max(len(payload.table.columns), 6)
    border_color = "D5DBE7"
    thin_border = Border(
        left=Side(style="thin", color=border_color),
        right=Side(style="thin", color=border_color),
        top=Side(style="thin", color=border_color),
        bottom=Side(style="thin", color=border_color),
    )

    row_idx = 1
    sheet.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=total_columns)
    title_cell = sheet.cell(row=row_idx, column=1, value=payload.title)
    title_cell.font = Font(name="Calibri", size=16, bold=True, color="0F172A")
    title_cell.alignment = Alignment(horizontal="left", vertical="center")
    row_idx += 1

    sheet.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=total_columns)
    generated_cell = sheet.cell(
        row=row_idx,
        column=1,
        value=f"Generado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    )
    generated_cell.font = Font(name="Calibri", size=10, color="475569")
    generated_cell.alignment = Alignment(horizontal="left", vertical="center")
    row_idx += 2

    sheet.cell(row=row_idx, column=1, value="Empresa").font = Font(bold=True, color="334155")
    sheet.cell(row=row_idx, column=2, value=payload.company.name or "N/A")
    row_idx += 1
    sheet.cell(row=row_idx, column=1, value="Direccion").font = Font(bold=True, color="334155")
    sheet.cell(row=row_idx, column=2, value=payload.company.address or "N/A")
    row_idx += 1
    sheet.cell(row=row_idx, column=1, value="Email").font = Font(bold=True, color="334155")
    sheet.cell(row=row_idx, column=2, value=payload.company.email or "N/A")
    row_idx += 1
    sheet.cell(row=row_idx, column=1, value="Telefono").font = Font(bold=True, color="334155")
    sheet.cell(row=row_idx, column=2, value=payload.company.phone or "N/A")
    row_idx += 2

    sheet.cell(row=row_idx, column=1, value="Filtros").font = Font(size=12, bold=True, color="0F172A")
    row_idx += 1
    filter_rows = [
        ("Desde", payload.filters.from_date),
        ("Hasta", payload.filters.to_date),
        ("POS", payload.filters.pos_filter),
        ("Metodo", payload.filters.method_filter),
        ("Vendedor", payload.filters.seller_filter),
    ]
    for label, value in filter_rows:
        sheet.cell(row=row_idx, column=1, value=label).font = Font(bold=True, color="334155")
        sheet.cell(row=row_idx, column=2, value=value or "Todos")
        row_idx += 1

    row_idx += 1
    if payload.summary:
        sheet.cell(row=row_idx, column=1, value="Resumen").font = Font(size=12, bold=True, color="0F172A")
        row_idx += 1
        for item in payload.summary:
            sheet.cell(row=row_idx, column=1, value=item.label).font = Font(bold=True, color="334155")
            sheet.cell(row=row_idx, column=2, value=item.value)
            row_idx += 1
        row_idx += 1

    table_header_row = row_idx
    header_fill = PatternFill(start_color="E2E8F0", end_color="E2E8F0", fill_type="solid")
    for col_idx, column_name in enumerate(payload.table.columns, start=1):
        cell = sheet.cell(row=table_header_row, column=col_idx, value=column_name)
        cell.font = Font(bold=True, color="0F172A")
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    row_idx += 1
    for raw_row in payload.table.rows:
        normalized_row = list(raw_row[: len(payload.table.columns)])
        if len(normalized_row) < len(payload.table.columns):
            normalized_row.extend([""] * (len(payload.table.columns) - len(normalized_row)))
        for col_idx, value in enumerate(normalized_row, start=1):
            cell = sheet.cell(row=row_idx, column=col_idx, value=value)
            cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
            cell.border = thin_border
        row_idx += 1

    if not payload.table.rows and payload.table.empty_message:
        sheet.merge_cells(
            start_row=row_idx,
            start_column=1,
            end_row=row_idx,
            end_column=len(payload.table.columns),
        )
        empty_cell = sheet.cell(row=row_idx, column=1, value=payload.table.empty_message)
        empty_cell.font = Font(italic=True, color="64748B")
        empty_cell.alignment = Alignment(horizontal="center", vertical="center")
        row_idx += 1

    if payload.table.rows:
        last_data_row = table_header_row + len(payload.table.rows)
        sheet.auto_filter.ref = f"A{table_header_row}:{get_column_letter(len(payload.table.columns))}{last_data_row}"
        sheet.freeze_panes = f"A{table_header_row + 1}"

    for col_idx in range(1, len(payload.table.columns) + 1):
        column_letter = get_column_letter(col_idx)
        max_len = 0
        for cell in sheet[column_letter]:
            if cell.value is None:
                continue
            max_len = max(max_len, len(str(cell.value)))
        sheet.column_dimensions[column_letter].width = min(max(12, max_len + 2), 42)

    output = BytesIO()
    workbook.save(output)
    output.seek(0)

    safe_id = re.sub(r"[^a-z0-9_-]+", "_", payload.preset_id.lower()).strip("_") or "reporte"
    filename = f"reporte_{safe_id}_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
