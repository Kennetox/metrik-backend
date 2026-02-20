from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from io import BytesIO
from typing import Optional
import json
import urllib.error
import urllib.request
import ssl
import os
import threading

try:
    import certifi
except ModuleNotFoundError:
    certifi = None

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from openpyxl import Workbook

import schemas
from dependencies import require_permission


router = APIRouter(
    prefix="/labels",
    tags=["labels"],
)
DEFAULT_SATO_CLOUD_TIMEOUT_SECONDS = int(
    os.getenv("SATO_CLOUD_TIMEOUT_SECONDS", "20")
)
SATO_CLOUD_SKIP_TLS_VERIFY = (
    os.getenv("SATO_CLOUD_SKIP_TLS_VERIFY", "false").strip().lower()
    in {"1", "true", "yes", "on"}
)
SATO_CLOUD_CLIENT_KEY = os.getenv("SATO_CLOUD_CLIENT_KEY", "").strip()
SATO_CLOUD_BASE_URL = os.getenv(
    "SATO_CLOUD_BASE_URL",
    "https://satoeasyprint.com/api/v1/labels/print",
).strip().rstrip("/")


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


@router.post("/cloud/print/{serial}")
def print_label_via_sato_cloud(
    serial: str,
    payload: schemas.LabelCloudPrintRequest,
    _: object = Depends(require_permission("labels.export")),
):
    serial_value = serial.strip()
    if not serial_value:
        raise HTTPException(status_code=400, detail="Serial inválido")

    if not SATO_CLOUD_BASE_URL:
        raise HTTPException(
            status_code=500,
            detail="Falta configurar SATO_CLOUD_BASE_URL en el backend.",
        )
    if not SATO_CLOUD_CLIENT_KEY:
        raise HTTPException(
            status_code=500,
            detail="Falta configurar SATO_CLOUD_CLIENT_KEY en el backend.",
        )

    target_url = f"{SATO_CLOUD_BASE_URL}/{serial_value}"
    request_body = json.dumps([payload.payload.model_dump()]).encode("utf-8")
    request = urllib.request.Request(
        target_url,
        data=request_body,
        headers={
            "Content-Type": "application/json",
            "X-CLIENT-KEY": SATO_CLOUD_CLIENT_KEY,
        },
        method="POST",
    )

    if SATO_CLOUD_SKIP_TLS_VERIFY:
        tls_context = ssl._create_unverified_context()
    else:
        if certifi is not None:
            tls_context = ssl.create_default_context(cafile=certifi.where())
        else:
            tls_context = ssl.create_default_context()

    def _dispatch_request() -> None:
        try:
            with urllib.request.urlopen(
                request,
                timeout=DEFAULT_SATO_CLOUD_TIMEOUT_SECONDS,
                context=tls_context,
            ):
                return
        except Exception:
            # Flujo beta: no bloqueamos el request del cliente por fallos async.
            return

    if payload.fire_and_forget:
        threading.Thread(target=_dispatch_request, daemon=True).start()
        return {
            "ok": True,
            "accepted": True,
            "mode": "fire_and_forget",
            "upstream_url": target_url,
        }

    try:
        with urllib.request.urlopen(
            request,
            timeout=DEFAULT_SATO_CLOUD_TIMEOUT_SECONDS,
            context=tls_context,
        ) as upstream:
            response_body = upstream.read().decode("utf-8", errors="replace")
            return {
                "ok": True,
                "status": upstream.status,
                "upstream_url": target_url,
                "upstream_body": response_body,
            }
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(
            status_code=502,
            detail=f"SATO Cloud respondió {exc.code}: {detail or 'sin detalle'}",
        ) from exc
    except urllib.error.URLError as exc:
        reason_text = str(exc.reason)
        if "timed out" in reason_text.lower():
            raise HTTPException(
                status_code=504,
                detail="SATO Cloud tardó demasiado en responder.",
            ) from exc
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo conectar a SATO Cloud: {reason_text}",
        ) from exc
