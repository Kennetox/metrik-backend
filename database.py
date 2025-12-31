from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Por ahora usamos SQLite local; luego cambiamos a PostgreSQL en la nube.
SQLALCHEMY_DATABASE_URL = "sqlite:///./kensar.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False}  # Necesario sólo para SQLite
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
