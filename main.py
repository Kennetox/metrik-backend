import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from database import Base, engine, SessionLocal
import models
import crud
from db_migrations import run_schema_upgrades
from services import storage
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
    platform as platform_router,
    investment as investment_router,
)

app = FastAPI(
    title="Kensar API",
    version="0.1.0",
    description="Backend inicial del sistema Kensar 2.0"
)

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
app.include_router(platform_router.router)
app.include_router(investment_router.router)
if ENABLE_SCHEDULE_MODULE:
    from routers import schedule as schedule_router

    app.include_router(schedule_router.router)
