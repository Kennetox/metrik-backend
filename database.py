import os
from dotenv import load_dotenv

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Cargar variables de entorno desde .env
load_dotenv(override=False)

SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL")

if not SQLALCHEMY_DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL no está configurada; define la cadena de conexión a PostgreSQL."
    )

connect_args = {}
engine_kwargs = {}
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
else:
    def _env_int(name: str, default: int, *, min_value: int = 1, max_value: int = 100) -> int:
        raw = (os.getenv(name) or "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except Exception:
            return default
        return max(min_value, min(max_value, value))

    engine_kwargs = {
        "pool_size": _env_int("DB_POOL_SIZE", 10, min_value=1, max_value=50),
        "max_overflow": _env_int("DB_MAX_OVERFLOW", 20, min_value=0, max_value=100),
        "pool_timeout": _env_int("DB_POOL_TIMEOUT", 30, min_value=1, max_value=120),
        "pool_recycle": _env_int("DB_POOL_RECYCLE", 1800, min_value=60, max_value=24 * 60 * 60),
        "pool_pre_ping": True,
    }

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args=connect_args,
    **engine_kwargs,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


# Dependency para FastAPI (la usaremos en los endpoints)
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
