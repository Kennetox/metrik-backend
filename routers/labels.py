from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from io import BytesIO
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from openpyxl import Workbook

import schemas
from dependencies import require_permission


router = APIRouter(
    prefix="/labels",
    tags=["labels"],
)


def _format_price_text(value: float) -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Precio inválido")

    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    integer_part, fractional_part = f"{quantized:.2f}".split(".")
    integer_formatted = f"{int(integer_part):,}".replace(",", ".")
    if fractional_part == "00":
        return f"${integer_formatted}"
    return f"${integer_formatted},{fractional_part}"


def _resolve_sku_text(sku: Optional[str], product_id: int) -> str:
    value = sku.strip() if sku else ""
    code = value or str(product_id)
    return f"Code: {code}"


@router.post("/export/xlsx")
def export_labels_excel(
    payload: schemas.LabelExportRequest,
    _: object = Depends(require_permission("labels.export")),
):
    if not payload.items:
        raise HTTPException(status_code=400, detail="Debes enviar al menos un producto")

    workbook = Workbook()
    try:
        workbook.calculation_properties.fullCalcOnLoad = True
    except AttributeError:
        pass
    sheet = workbook.active
    sheet.title = "Etiquetas"
    headers = ["SKU", "Nombre", "Precio", "Código de barras"]
    sheet.append(headers)
    for cell in sheet[sheet.max_row]:
        cell.number_format = "@"
        cell.data_type = "s"
        if cell.value is not None:
            cell.value = str(cell.value)

    for item in payload.items:
        try:
            price_text = _format_price_text(item.price)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        repetitions = max(item.quantity, 1)
        for _ in range(repetitions):
            sheet.append(
                [
                    _resolve_sku_text(item.sku, item.product_id),
                    item.name,
                    price_text,
                    (item.barcode or ""),
                ]
            )
            for cell in sheet[sheet.max_row]:
                cell.number_format = "@"
                cell.data_type = "s"
                if cell.value is not None:
                    cell.value = str(cell.value)

    stream = BytesIO()
    workbook.save(stream)
    stream.seek(0)

    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": "attachment; filename=labels.xlsx",
        },
    )
