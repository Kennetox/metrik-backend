from datetime import datetime
from html import escape
from io import BytesIO
import os
from pathlib import Path
import re
from typing import List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.drawing.image import Image as XlsxImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from sqlalchemy.orm import Session

import crud
import models
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


def _parse_money(raw_value: str) -> Optional[float]:
    cleaned = raw_value.strip()
    if not cleaned:
        return None
    negative = cleaned.startswith("-")
    cleaned = cleaned.replace("$", "").replace(" ", "")
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        numeric = float(cleaned)
    except ValueError:
        return None
    return -abs(numeric) if negative else numeric


def _parse_percent(raw_value: str) -> Optional[float]:
    cleaned = raw_value.strip().replace("%", "").replace(",", ".")
    if not cleaned:
        return None
    try:
        numeric = float(cleaned)
    except ValueError:
        return None
    return numeric / 100.0


def _parse_integer(raw_value: str) -> Optional[int]:
    cleaned = re.sub(r"[^\d-]", "", raw_value.strip())
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _parse_date(raw_value: str) -> Optional[datetime]:
    text = raw_value.strip()
    if not text:
        return None
    formats = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d/%m/%Y",
        "%d/%m/%y",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%y %H:%M",
    ]
    for pattern in formats:
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def _infer_cell_value(column_name: str, raw_value: str):
    normalized_col = column_name.lower().strip()
    text_value = (raw_value or "").strip()

    is_money_col = any(
        token in normalized_col
        for token in (
            "precio",
            "valor",
            "ventas",
            "total",
            "monto",
            "saldo",
            "recargo",
            "pagado",
            "ticket promedio",
        )
    )
    is_percent_col = "%" in normalized_col or any(
        token in normalized_col for token in ("participacion", "porcentaje")
    )
    is_count_col = any(
        token in normalized_col for token in ("cantidad", "tickets", "unidades")
    )
    is_date_col = normalized_col.startswith("fecha") or "ultima venta" in normalized_col

    if is_money_col:
        parsed = _parse_money(text_value)
        if parsed is not None:
            return parsed, '"$"#,##0', "right"
    if is_percent_col or "%" in text_value:
        parsed = _parse_percent(text_value)
        if parsed is not None:
            return parsed, "0.0%", "right"
    if is_count_col:
        parsed = _parse_integer(text_value)
        if parsed is not None:
            return parsed, "#,##0", "right"
    if is_date_col:
        parsed_date = _parse_date(text_value)
        if parsed_date is not None:
            if parsed_date.hour == 0 and parsed_date.minute == 0 and parsed_date.second == 0:
                return parsed_date, "DD/MM/YYYY", "center"
            return parsed_date, "DD/MM/YYYY HH:MM", "center"

    return text_value, None, "left"


def _resolve_ticket_logo_path(settings: Optional[models.PosSettings]) -> Optional[Path]:
    if settings is None:
        return None
    logo_url = (settings.ticket_logo_url or settings.logo_url or "").strip()
    if not logo_url:
        return None

    parsed = urlparse(logo_url)
    logo_path = parsed.path or logo_url
    logo_path = logo_path.strip()
    if not logo_path:
        return None

    # Evitamos SVG: openpyxl no lo inserta de forma nativa.
    lower_path = logo_path.lower()
    if lower_path.endswith(".svg"):
        return None

    logo_dir = Path(os.getenv("POS_LOGO_UPLOAD_DIR", "uploads/pos-logos"))
    public_prefix = os.getenv("POS_LOGO_PUBLIC_PATH", "/uploads/pos-logos").rstrip("/")

    filename: Optional[str] = None
    if logo_path.startswith(public_prefix + "/"):
        filename = logo_path[len(public_prefix) + 1 :]
    elif "/uploads/pos-logos/" in logo_path:
        filename = logo_path.split("/uploads/pos-logos/", 1)[1]
    elif "/" not in logo_path:
        filename = logo_path

    if not filename:
        return None

    candidate = (logo_dir / filename).resolve()
    try:
        candidate.relative_to(logo_dir.resolve())
    except ValueError:
        return None
    if not candidate.exists() or not candidate.is_file():
        return None
    return candidate


@router.post("/export/xlsx")
def export_report_xlsx(
    payload: schemas.ReportExportRequest,
    db: Session = Depends(get_db),
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

    table_columns = len(payload.table.columns)
    total_columns = max(table_columns, 6)
    border_color = "D5DBE7"
    brand_primary = "0F766E"
    brand_primary_soft = "CCFBF1"
    table_header_fill = "ECFEFF"
    zebra_fill = "F8FAFC"

    thin_border = Border(
        left=Side(style="thin", color=border_color),
        right=Side(style="thin", color=border_color),
        top=Side(style="thin", color=border_color),
        bottom=Side(style="thin", color=border_color),
    )

    row_idx = 1
    sheet.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=total_columns)
    title_cell = sheet.cell(row=row_idx, column=1, value=payload.title)
    title_cell.font = Font(name="Calibri", size=17, bold=True, color="FFFFFF")
    title_cell.alignment = Alignment(horizontal="left", vertical="center")
    title_cell.fill = PatternFill(start_color=brand_primary, end_color=brand_primary, fill_type="solid")
    row_idx_height = 28
    sheet.row_dimensions[row_idx].height = row_idx_height
    row_idx += 1

    sheet.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=total_columns)
    generated_cell = sheet.cell(
        row=row_idx,
        column=1,
        value=f"Generado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    )
    generated_cell.font = Font(name="Calibri", size=10, color="475569")
    generated_cell.alignment = Alignment(horizontal="left", vertical="center")
    generated_cell.fill = PatternFill(
        start_color=brand_primary_soft, end_color=brand_primary_soft, fill_type="solid"
    )
    row_idx += 2

    settings = crud.get_pos_settings(db)
    logo_path = _resolve_ticket_logo_path(settings)
    if logo_path is not None:
        try:
            logo_image = XlsxImage(str(logo_path))
            max_logo_width = 180
            if logo_image.width and logo_image.width > max_logo_width:
                ratio = max_logo_width / float(logo_image.width)
                logo_image.width = int(logo_image.width * ratio)
                logo_image.height = int(logo_image.height * ratio)
            anchor_col = get_column_letter(max(1, total_columns - 1))
            logo_image.anchor = f"{anchor_col}1"
            sheet.add_image(logo_image)
        except Exception:
            # Si el formato no es compatible, el reporte igual se exporta.
            pass

    section_cell = sheet.cell(row=row_idx, column=1, value="Informacion general")
    section_cell.font = Font(size=12, bold=True, color="0F172A")
    row_idx += 1
    company_rows = [
        ("Empresa", payload.company.name or "N/A"),
        ("Direccion", payload.company.address or "N/A"),
        ("Email", payload.company.email or "N/A"),
        ("Telefono", payload.company.phone or "N/A"),
    ]
    for label, value in company_rows:
        sheet.cell(row=row_idx, column=1, value=label).font = Font(bold=True, color="334155")
        value_cell = sheet.cell(row=row_idx, column=2, value=value)
        value_cell.font = Font(color="0F172A")
        row_idx += 1

    row_idx += 2

    sheet.cell(row=row_idx, column=1, value="Filtros aplicados").font = Font(
        size=12, bold=True, color="0F172A"
    )
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

    row_idx += 2
    if payload.summary:
        sheet.cell(row=row_idx, column=1, value="Resumen ejecutivo").font = Font(
            size=12, bold=True, color="0F172A"
        )
        row_idx += 1
        summary_header_fill = PatternFill(
            start_color=brand_primary_soft, end_color=brand_primary_soft, fill_type="solid"
        )
        for item in payload.summary:
            label_cell = sheet.cell(row=row_idx, column=1, value=item.label)
            label_cell.font = Font(bold=True, color="134E4A")
            label_cell.fill = summary_header_fill
            label_cell.border = thin_border
            value_cell = sheet.cell(row=row_idx, column=2, value=item.value)
            value_cell.font = Font(bold=True, color="0F172A")
            value_cell.fill = summary_header_fill
            value_cell.border = thin_border
            row_idx += 1
        row_idx += 2

    table_header_row = row_idx
    header_fill = PatternFill(start_color=table_header_fill, end_color=table_header_fill, fill_type="solid")
    for col_idx, column_name in enumerate(payload.table.columns, start=1):
        cell = sheet.cell(row=table_header_row, column=col_idx, value=column_name)
        cell.font = Font(bold=True, color=brand_primary)
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    row_idx += 1
    for row_number, raw_row in enumerate(payload.table.rows, start=1):
        normalized_row = list(raw_row[: len(payload.table.columns)])
        if len(normalized_row) < len(payload.table.columns):
            normalized_row.extend([""] * (len(payload.table.columns) - len(normalized_row)))
        for col_idx, raw_value in enumerate(normalized_row, start=1):
            column_name = payload.table.columns[col_idx - 1]
            value, number_format, horizontal_align = _infer_cell_value(column_name, raw_value)
            cell = sheet.cell(row=row_idx, column=col_idx, value=value)
            if number_format:
                cell.number_format = number_format
            cell.alignment = Alignment(
                horizontal=horizontal_align, vertical="center", wrap_text=horizontal_align == "left"
            )
            cell.border = thin_border
            if row_number % 2 == 0:
                cell.fill = PatternFill(start_color=zebra_fill, end_color=zebra_fill, fill_type="solid")
        row_idx += 1

    if not payload.table.rows and payload.table.empty_message:
        sheet.merge_cells(
            start_row=row_idx,
            start_column=1,
            end_row=row_idx + 1,
            end_column=table_columns,
        )
        empty_cell = sheet.cell(row=row_idx, column=1, value=payload.table.empty_message)
        empty_cell.font = Font(italic=True, color="64748B")
        empty_cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        row_idx += 2

    if payload.table.rows:
        last_data_row = table_header_row + len(payload.table.rows)
        sheet.auto_filter.ref = f"A{table_header_row}:{get_column_letter(table_columns)}{last_data_row}"
        sheet.freeze_panes = f"A{table_header_row + 1}"

    for col_idx in range(1, table_columns + 1):
        column_letter = get_column_letter(col_idx)
        max_len = 0
        for cell in sheet[column_letter]:
            if cell.value is None:
                continue
            max_len = max(max_len, len(str(cell.value)))
        sheet.column_dimensions[column_letter].width = min(max(12, max_len + 2), 42)

    if table_columns >= 8:
        sheet.page_setup.orientation = "landscape"
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0

    meta_sheet = workbook.create_sheet("Metadata")
    meta_sheet.append(["Campo", "Valor"])
    meta_sheet["A1"].font = Font(bold=True, color="FFFFFF")
    meta_sheet["B1"].font = Font(bold=True, color="FFFFFF")
    meta_sheet["A1"].fill = PatternFill(start_color=brand_primary, end_color=brand_primary, fill_type="solid")
    meta_sheet["B1"].fill = PatternFill(start_color=brand_primary, end_color=brand_primary, fill_type="solid")
    metadata_rows = [
        ("Preset ID", payload.preset_id),
        ("Titulo", payload.title),
        ("Generado", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Desde", payload.filters.from_date),
        ("Hasta", payload.filters.to_date),
        ("POS", payload.filters.pos_filter),
        ("Metodo", payload.filters.method_filter),
        ("Vendedor", payload.filters.seller_filter),
        ("Filas de tabla", str(len(payload.table.rows))),
        ("Columnas de tabla", str(table_columns)),
    ]
    for row in metadata_rows:
        meta_sheet.append(list(row))
    meta_sheet.column_dimensions["A"].width = 22
    meta_sheet.column_dimensions["B"].width = 50
    meta_sheet.freeze_panes = "A2"

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
