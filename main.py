import logging
import os
import asyncio
from datetime import datetime, timedelta

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
)

app = FastAPI(
    title="Kensar API",
    version="0.1.0",
    description="Backend inicial del sistema Kensar 2.0"
)

# Carga variables desde `.env` para entorno local/dev.
load_dotenv()

# Kill switch del modulo de horarios (queda completamente apagado).
ENABLE_SCHEDULE_MODULE = False


@app.get("/health")
async def health_check():
    return {"status": "ok"}


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

# Crear tablas en la BD
Base.metadata.create_all(bind=engine)
# Aplicamos parches de schema para SQLite y Postgres.
run_schema_upgrades(engine)

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

logger = logging.getLogger("kensar.validation")
scheduler_logger = logging.getLogger("kensar.scheduler")
_monthly_report_task: asyncio.Task | None = None
_payment_reconciliation_task: asyncio.Task | None = None


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
    raw = os.getenv("WEB_PAYMENT_RECONCILIATION_ENABLED", "true").strip().lower()
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
    raw_body = await request.body()
    body_text = raw_body.decode("utf-8") if raw_body else ""
    logger.error(
        "Validation error on %s %s | body=%s | errors=%s",
        request.method,
        request.url.path,
        body_text,
        exc.errors(),
    )
    return JSONResponse(
        status_code=422,
        content={
            "detail": exc.errors(),
            "body": body_text,
        },
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
if ENABLE_SCHEDULE_MODULE:
    from routers import schedule as schedule_router

    app.include_router(schedule_router.router)
