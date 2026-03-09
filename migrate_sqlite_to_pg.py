"""
Copia datos seleccionados desde un SQLite legacy hacia Postgres.
Incluye: product_groups, products, pos_settings (branding/email).

Cómo usar:
1) Ajusta SQLITE_PATH y PG_URL (o toma DATABASE_URL del entorno).
2) Activa el virtualenv.
3) Ejecuta: python3 migrate_sqlite_to_pg.py
"""

import os
from typing import Iterable

from sqlalchemy import MetaData, Table, create_engine, select, text

# Ruta al archivo SQLite legacy
SQLITE_PATH = os.getenv("SQLITE_PATH", "/Users/kennethjaramillo/Documents/kensar.db")
# URL de Postgres; usa la env si está definida
PG_URL = os.getenv("DATABASE_URL")
if not PG_URL:
    raise RuntimeError("Define DATABASE_URL apuntando a Postgres (postgresql+psycopg://...).")

# Tablas a migrar (ajusta si hace falta)
TABLES_TO_COPY = ["product_groups", "products", "pos_settings", "payment_methods"]

src_engine = create_engine(f"sqlite:///{SQLITE_PATH}", connect_args={"check_same_thread": False})
dst_engine = create_engine(PG_URL)


def reset_sequence(conn, table: str, pk: str = "id") -> None:
    """Sincroniza la secuencia de autoincremento con el valor máximo actual."""
    seq_name = conn.execute(
        text("SELECT pg_get_serial_sequence(:table, :pk)"),
        {"table": table, "pk": pk},
    ).scalar()
    if not seq_name:
        return
    conn.execute(
        text(f"SELECT setval(:seq, COALESCE((SELECT MAX({pk}) FROM {table}), 0), true)"),
        {"seq": seq_name},
    )


def copy_table(
    src_tables: dict[str, Table],
    dst_tables: dict[str, Table],
    name: str,
    *,
    delete_first: bool = True,
) -> int:
    s_tbl = src_tables[name]
    d_tbl = dst_tables[name]

    with src_engine.connect() as sconn:
        rows: Iterable[dict] = sconn.execute(select(s_tbl)).mappings().all()
    with dst_engine.begin() as conn:
        if delete_first:
            conn.execute(d_tbl.delete())
        if rows:
            conn.execute(d_tbl.insert(), rows)
        # Sincroniza la secuencia de autoincremento si aplica
        if "id" in d_tbl.c:
            reset_sequence(conn, name, "id")
    return len(rows)


def main() -> None:
    src_md = MetaData()
    dst_md = MetaData()
    src_md.reflect(src_engine, only=TABLES_TO_COPY)
    dst_md.reflect(dst_engine, only=TABLES_TO_COPY)

    for table in TABLES_TO_COPY:
        if table not in src_md.tables:
            raise RuntimeError(f"La tabla '{table}' no existe en SQLite.")
        if table not in dst_md.tables:
            raise RuntimeError(f"La tabla '{table}' no existe en Postgres.")

    copied = {}
    for name in TABLES_TO_COPY:
        count = copy_table(src_md.tables, dst_md.tables, name)
        copied[name] = count
        print(f"Copiada {name}: {count} filas")

    print("Migración completada:", copied)


if __name__ == "__main__":
    main()
