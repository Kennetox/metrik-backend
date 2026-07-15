import logging
import os
import asyncio
import re
import time
from uuid import uuid4
from datetime import datetime, timedelta
from sqlalchemy import text

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from dotenv import load_dotenv
from database import Base, engine, SessionLocal
import models
import crud
from db_migrations import run_schema_upgrades
from services import storage
from services import monthly_report_email
from services.payments.sync import refresh_backoffice_order_payment_statuses
from routers import (
    uploads as uploads_router,
    labels as labels_router,
    products as products_router,
    pos as pos_router,
    dashboard as dashboard_router,
    auth as auth_router,
    product_groups as product_groups_router,
    reports as reports_router,
    separated_orders as separated_orders_router,
    inventory as inventory_router,
    receiving as receiving_router,
    stock_devices as stock_devices_router,
    manual_movements as manual_movements_router,
    hr as hr_router,
    web_catalog as web_catalog_router,
    web_customers as web_customers_router,
    web_cart as web_cart_router,
    web_orders as web_orders_router,
    web_payments as web_payments_router,
    web_payments_mercadopago as web_payments_mercadopago_router,
    web_payments_wompi as web_payments_wompi_router,
    comercio_web as comercio_web_router,
    platform as platform_router,
    investment as investment_router,
    kora as kora_router,
    legacy_imports as legacy_imports_router,
    documents as documents_router,
)

app = FastAPI(
    title="Kensar API",
    version="0.1.0",
    description="Backend inicial del sistema Kensar 2.0"
)

# Carga variables desde `.env` para entorno local/dev.
load_dotenv()

# Kill switch del modulo de horarios.
ENABLE_SCHEDULE_MODULE = True
_READYZ_CACHE: dict[str, tuple[datetime, int, dict[str, object]]] = {}


def _get_readyz_cache() -> tuple[int, dict[str, object]] | None:
    cache_entry = _READYZ_CACHE.get("readyz")
    if not cache_entry:
        return None
    expires_at, status_code, payload = cache_entry
    if expires_at <= datetime.utcnow():
        _READYZ_CACHE.pop("readyz", None)
        return None
    return status_code, payload


def _set_readyz_cache(status_code: int, payload: dict[str, object], ttl_seconds: int) -> None:
    _READYZ_CACHE["readyz"] = (
        datetime.utcnow() + timedelta(seconds=ttl_seconds),
        status_code,
        payload,
    )


@app.get("/health")
async def health_check():
    return {"status": "ok"}


def _flag_enabled(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "on", "yes"}


def _maintenance_enabled() -> bool:
    return _flag_enabled(os.getenv("MAINTENANCE_MODE"), default=False)


def _schema_bootstrap_enabled() -> bool:
    raw = os.getenv("BOOTSTRAP_SCHEMA_ON_STARTUP")
    if raw is not None:
        return _flag_enabled(raw, default=False)

    # En producción preferimos un arranque liviano; en SQLite/local conservamos
    # el bootstrap automático para no romper flujos de desarrollo.
    database_url = (os.getenv("DATABASE_URL") or "").strip().lower()
    return database_url.startswith("sqlite")


def _bootstrap_database_schema() -> None:
    bootstrap_schema = _schema_bootstrap_enabled()
    if bootstrap_schema:
        # En una instalación local completamente nueva todavía no existen las
        # tablas que los upgrades intentan alterar. Creamos primero el esquema
        # base y luego aplicamos migraciones/semillas idempotentes.
        Base.metadata.create_all(bind=engine)

    # Aplicamos upgrades idempotentes siempre. Esto corrige desfases de esquema
    # en producción sin habilitar el create_all costoso de forma implícita.
    run_schema_upgrades(engine)

    if not bootstrap_schema:
        return

    platform_owner_email = os.getenv("PLATFORM_OWNER_EMAIL")
    platform_owner_password = os.getenv("PLATFORM_OWNER_PASSWORD")
    platform_owner_name = os.getenv("PLATFORM_OWNER_NAME", "Metrik Platform Admin")
    if platform_owner_email and platform_owner_password:
        bootstrap_db = SessionLocal()
        try:
            crud.ensure_platform_user(
                bootstrap_db,
                email=platform_owner_email,
                password=platform_owner_password,
                name=platform_owner_name,
            )
        finally:
            bootstrap_db.close()


@app.get("/healthz")
async def healthz():
    # Liveness probe: solo confirma que el proceso HTTP está vivo.
    return {
        "status": "ok",
        "service": "kensar-backend",
        "maintenance": _maintenance_enabled(),
    }


@app.get("/readyz")
async def readyz():
    cached = _get_readyz_cache()
    if cached is not None:
        status_code, payload = cached
        if status_code == 200:
            return payload
        return JSONResponse(status_code=status_code, content=payload)

    if _maintenance_enabled():
        payload = {
            "status": "maintenance",
            "service": "kensar-backend",
            "ready": False,
            "maintenance": True,
        }
        _set_readyz_cache(503, payload, ttl_seconds=15)
        return JSONResponse(status_code=503, content=payload)

    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        payload = {
            "status": "degraded",
            "service": "kensar-backend",
            "ready": False,
            "maintenance": False,
        }
        _set_readyz_cache(503, payload, ttl_seconds=15)
        return JSONResponse(status_code=503, content=payload)
    finally:
        db.close()

    payload = {
        "status": "ok",
        "service": "kensar-backend",
        "ready": True,
        "maintenance": False,
    }
    _set_readyz_cache(200, payload, ttl_seconds=10)
    return payload


cors_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://metrikpos.com",
    "https://www.metrikpos.com",
    "https://kensar-frontend-wyu3.vercel.app",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logger = logging.getLogger("kensar.validation")
http_logger = logging.getLogger("kensar.http")
scheduler_logger = logging.getLogger("kensar.scheduler")
_monthly_report_task: asyncio.Task | None = None
_payment_reconciliation_task: asyncio.Task | None = None

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _resolve_request_id(request: Request) -> str:
    candidate = (request.headers.get("x-request-id") or "").strip()
    if _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return uuid4().hex


@app.middleware("http")
async def request_observability_middleware(request: Request, call_next):
    """Añade correlación y registra solo fallos/lentitud, sin cuerpos ni query strings."""
    request_id = _resolve_request_id(request)
    request.state.request_id = request_id
    started_at = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = round((time.perf_counter() - started_at) * 1000)
        http_logger.exception(
            "request_failed request_id=%s method=%s path=%s duration_ms=%s",
            request_id,
            request.method,
            request.url.path,
            duration_ms,
        )
        raise

    duration_ms = round((time.perf_counter() - started_at) * 1000)
    response.headers["X-Request-ID"] = request_id
    response.headers["Server-Timing"] = f'app;dur={duration_ms}'
    is_pos_path = request.url.path.startswith(("/pos", "/separated-orders"))
    is_critical_sale_write = request.method == "POST" and request.url.path in {
        "/pos/sales",
        "/separated-orders",
    }
    if is_critical_sale_write:
        http_logger.info(
            "sale_request request_id=%s path=%s status=%s duration_ms=%s",
            request_id,
            request.url.path,
            response.status_code,
            duration_ms,
        )
    elif response.status_code >= 500 or (is_pos_path and duration_ms >= 1500):
        http_logger.warning(
            "request_observed request_id=%s method=%s path=%s status=%s duration_ms=%s",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
    return response


def _monthly_report_scheduler_enabled() -> bool:
    raw = os.getenv("MONTHLY_REPORT_SCHEDULER_ENABLED", "true").strip().lower()
    if raw in {"0", "false", "off", "no"}:
        return False
    # Evita ruido en ejecución de tests
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    return True


async def _monthly_report_scheduler_loop():
    await asyncio.sleep(8)
    while True:
        try:
            result = monthly_report_email.run_auto_monthly_dispatch()
            if result.get("status") != "idle":
                scheduler_logger.info("Monthly report scheduler: %s", result)
        except Exception:
            scheduler_logger.exception("Monthly report scheduler failed")
        await asyncio.sleep(15 * 60)


def _payment_reconciliation_enabled() -> bool:
    raw = os.getenv("WEB_PAYMENT_RECONCILIATION_ENABLED", "false").strip().lower()
    if raw in {"0", "false", "off", "no"}:
        return False
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    return True


def _payment_reconciliation_interval_seconds() -> int:
    try:
        value = int((os.getenv("WEB_PAYMENT_RECONCILIATION_INTERVAL_SECONDS") or "90").strip())
    except Exception:
        value = 90
    return max(20, value)


def _payment_reconciliation_batch_size() -> int:
    try:
        value = int((os.getenv("WEB_PAYMENT_RECONCILIATION_BATCH_SIZE") or "50").strip())
    except Exception:
        value = 50
    return max(1, min(200, value))


def _payment_reconciliation_lookback_hours() -> int:
    try:
        value = int((os.getenv("WEB_PAYMENT_RECONCILIATION_LOOKBACK_HOURS") or "48").strip())
    except Exception:
        value = 48
    return max(1, min(24 * 14, value))


def _run_payment_reconciliation_once() -> dict[str, int]:
    from routers import web_payments_mercadopago as mp_router

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=_payment_reconciliation_lookback_hours())
        batch_size = _payment_reconciliation_batch_size()
        expired = crud.expire_stale_web_orders_all_tenants(db, now=now)

        candidates = (
            db.query(models.WebOrder)
            .filter(models.WebOrder.sale_id.is_(None))
            .filter(models.WebOrder.status.in_(["pending_payment", "payment_failed", "paid", "processing"]))
            .filter(models.WebOrder.created_at >= cutoff)
            .order_by(models.WebOrder.updated_at.desc(), models.WebOrder.id.desc())
            .limit(batch_size)
            .all()
        )
        if not candidates:
            return {"checked": 0, "rescued": 0, "expired": expired}

        refreshed_orders = refresh_backoffice_order_payment_statuses(db, candidates)
        rescued = 0
        for order in refreshed_orders:
            if not order:
                continue
            if order.payment_status == "approved" and order.sale_id is None:
                try:
                    mp_router._run_web_order_post_approval_flow(db, order)
                    post = crud.get_backoffice_web_order(db, order.id, tenant_id=order.tenant_id)
                    if post and post.sale_id is not None:
                        rescued += 1
                except Exception:
                    scheduler_logger.exception(
                        "Payment reconciliation post-approval failed | order_id=%s",
                        getattr(order, "id", None),
                    )
        return {"checked": len(candidates), "rescued": rescued, "expired": expired}
    finally:
        db.close()


async def _payment_reconciliation_loop():
    await asyncio.sleep(12)
    while True:
        try:
            result = _run_payment_reconciliation_once()
            if result.get("checked", 0) > 0 or result.get("expired", 0) > 0:
                scheduler_logger.info("Payment reconciliation: %s", result)
        except Exception:
            scheduler_logger.exception("Payment reconciliation scheduler failed")
        await asyncio.sleep(_payment_reconciliation_interval_seconds())


@app.on_event("startup")
async def _start_monthly_report_scheduler():
    global _monthly_report_task, _payment_reconciliation_task
    _bootstrap_database_schema()
    if not _monthly_report_scheduler_enabled():
        _monthly_report_task = None
    else:
        if not (_monthly_report_task and not _monthly_report_task.done()):
            _monthly_report_task = asyncio.create_task(_monthly_report_scheduler_loop())

    if not _payment_reconciliation_enabled():
        _payment_reconciliation_task = None
    else:
        if not (_payment_reconciliation_task and not _payment_reconciliation_task.done()):
            _payment_reconciliation_task = asyncio.create_task(_payment_reconciliation_loop())


@app.on_event("shutdown")
async def _stop_monthly_report_scheduler():
    global _monthly_report_task, _payment_reconciliation_task
    if _monthly_report_task is None:
        pass
    else:
        _monthly_report_task.cancel()
        try:
            await _monthly_report_task
        except asyncio.CancelledError:
            pass
        _monthly_report_task = None

    if _payment_reconciliation_task is None:
        return
    _payment_reconciliation_task.cancel()
    try:
        await _payment_reconciliation_task
    except asyncio.CancelledError:
        pass
    _payment_reconciliation_task = None

app.include_router(uploads_router.router)
app.include_router(labels_router.router)

uploads_root_dir = storage.get_uploads_root_dir()
uploads_root_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(uploads_root_dir)), name="uploads")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
):
    request_id = getattr(request.state, "request_id", uuid4().hex)
    safe_errors = [
        {
            "type": error.get("type"),
            "loc": error.get("loc"),
            "msg": error.get("msg"),
        }
        for error in exc.errors()
    ]
    logger.error(
        "validation_failed request_id=%s method=%s path=%s errors=%s",
        request_id,
        request.method,
        request.url.path,
        safe_errors,
    )
    return JSONResponse(
        status_code=422,
        content={
            "detail": safe_errors,
            "request_id": request_id,
        },
        headers={"X-Request-ID": request_id},
    )


# Registrar routers
app.include_router(products_router.router)
app.include_router(product_groups_router.router)
app.include_router(reports_router.router)
app.include_router(separated_orders_router.router)
app.include_router(pos_router.router)
app.include_router(dashboard_router.router)
app.include_router(auth_router.router)
app.include_router(inventory_router.router)
app.include_router(receiving_router.router)
app.include_router(stock_devices_router.router)
app.include_router(manual_movements_router.router)
app.include_router(hr_router.router)
app.include_router(web_catalog_router.router)
app.include_router(web_customers_router.router)
app.include_router(web_cart_router.router)
app.include_router(web_orders_router.router)
app.include_router(web_payments_router.router)
app.include_router(web_payments_mercadopago_router.router)
app.include_router(web_payments_wompi_router.router)
app.include_router(comercio_web_router.router)
app.include_router(platform_router.router)
app.include_router(investment_router.router)
app.include_router(kora_router.router)
app.include_router(legacy_imports_router.router)
app.include_router(documents_router.router)
if ENABLE_SCHEDULE_MODULE:
    from routers import schedule as schedule_router

    app.include_router(schedule_router.router)
