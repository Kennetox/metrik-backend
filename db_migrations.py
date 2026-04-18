"""Ad-hoc helpers to evolve the SQLite schema without Alembic."""

from sqlalchemy import text
from sqlalchemy.engine import Engine
from security import hash_password


def _column_exists(connection, table: str, column: str) -> bool:
    result = connection.execute(text(f"PRAGMA table_info({table})"))
    for row in result.mappings():
        if row.get("name") == column:
            return True
    return False


def _rename_column_if_exists(
    connection, table: str, current_name: str, new_name: str
) -> bool:
    """Renames a column if it exists under an unexpected name."""

    if not _column_exists(connection, table, current_name):
        return False

    connection.execute(
        text(f"ALTER TABLE {table} RENAME COLUMN {current_name} TO {new_name}")
    )
    return True


def _ensure_column(connection, table: str, column: str, ddl: str) -> None:
    if not _column_exists(connection, table, column):
        # Corrige intentos previos donde se añadió una columna sin nombre (FLOAT)
        if _rename_column_if_exists(connection, table, "FLOAT", column):
            return

        connection.execute(
            text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        )


def _table_exists(connection, table: str) -> bool:
    result = connection.execute(
        text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = :table"
        ),
        {"table": table},
    ).first()
    return result is not None


def _ensure_column_postgres(
    connection, table: str, column: str, ddl: str
) -> None:
    connection.execute(
        text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}")
    )


def _ensure_web_discount_code_schema(connection, backend: str) -> None:
    table = "web_discount_codes"
    if backend == "postgresql":
        if not _table_exists_postgres(connection, table):
            return
        _ensure_column_postgres(connection, table, "max_uses", "INTEGER")
        _ensure_column_postgres(connection, table, "uses_count", "INTEGER NOT NULL DEFAULT 0")
        return

    if not _table_exists(connection, table):
        return
    _ensure_column(connection, table, "max_uses", "INTEGER")
    _ensure_column(connection, table, "uses_count", "INTEGER NOT NULL DEFAULT 0")


def _ensure_web_cart_coupon_schema(connection, backend: str) -> None:
    table = "web_carts"
    if backend == "postgresql":
        if not _table_exists_postgres(connection, table):
            return
        _ensure_column_postgres(connection, table, "coupon_code", "VARCHAR(64)")
        _ensure_column_postgres(connection, table, "coupon_discount_percent", "FLOAT NOT NULL DEFAULT 0")
        _ensure_column_postgres(connection, table, "coupon_discount_code_id", "INTEGER")
        return

    if not _table_exists(connection, table):
        return
    _ensure_column(connection, table, "coupon_code", "TEXT")
    _ensure_column(connection, table, "coupon_discount_percent", "FLOAT NOT NULL DEFAULT 0")
    _ensure_column(connection, table, "coupon_discount_code_id", "INTEGER")


def _ensure_web_catalog_category_home_schema(connection, backend: str) -> None:
    table = "web_catalog_categories"
    if backend == "postgresql":
        if not _table_exists_postgres(connection, table):
            return
        _ensure_column_postgres(
            connection,
            table,
            "home_featured",
            "BOOLEAN NOT NULL DEFAULT FALSE",
        )
        _ensure_column_postgres(
            connection,
            table,
            "home_featured_order",
            "INTEGER NOT NULL DEFAULT 0",
        )
        return

    if not _table_exists(connection, table):
        return
    _ensure_column(connection, table, "home_featured", "BOOLEAN NOT NULL DEFAULT 0")
    _ensure_column(connection, table, "home_featured_order", "INTEGER NOT NULL DEFAULT 0")


def _table_exists_postgres(connection, table: str) -> bool:
    row = connection.execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = :table
            ) AS exists
            """
        ),
        {"table": table},
    ).mappings().first()
    return bool(row and row.get("exists"))


def _column_exists_postgres(connection, table: str, column: str) -> bool:
    row = connection.execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = :table
                  AND column_name = :column
            ) AS exists
            """
        ),
        {"table": table, "column": column},
    ).mappings().first()
    return bool(row and row.get("exists"))


def _has_duplicate_products_code_per_tenant(
    connection,
    backend: str,
    column: str,
) -> bool:
    trim_fn = "btrim" if backend == "postgresql" else "trim"
    row = connection.execute(
        text(
            f"""
            SELECT EXISTS (
              SELECT 1
              FROM products
              WHERE tenant_id IS NOT NULL
                AND {column} IS NOT NULL
                AND {trim_fn}({column}) <> ''
              GROUP BY tenant_id, {trim_fn}({column})
              HAVING COUNT(*) > 1
            ) AS has_dup
            """
        )
    ).mappings().first()
    return bool(row and row.get("has_dup"))


def _ensure_products_tenant_scoped_unique_indexes(connection, backend: str) -> None:
    if backend == "postgresql":
        # Remove legacy global uniqueness that breaks multitenant.
        connection.execute(text("DROP INDEX IF EXISTS ix_products_sku"))
        connection.execute(text("DROP INDEX IF EXISTS products_barcode_unique_idx"))
        connection.execute(
            text("ALTER TABLE IF EXISTS products DROP CONSTRAINT IF EXISTS products_sku_key")
        )
    else:
        connection.execute(text("DROP INDEX IF EXISTS ix_products_sku"))
        connection.execute(text("DROP INDEX IF EXISTS products_barcode_unique_idx"))

    has_dup_sku = _has_duplicate_products_code_per_tenant(connection, backend, "sku")
    has_dup_barcode = _has_duplicate_products_code_per_tenant(connection, backend, "barcode")

    if has_dup_sku:
        print(
            "[schema-upgrade] No se creó índice único (tenant_id, sku): "
            "hay duplicados dentro del mismo tenant."
        )
    else:
        trim_fn = "btrim" if backend == "postgresql" else "trim"
        connection.execute(
            text(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS products_tenant_sku_unique_idx
                ON products (tenant_id, sku)
                WHERE tenant_id IS NOT NULL
                  AND sku IS NOT NULL
                  AND {trim_fn}(sku) <> ''
                """
            )
        )

    if has_dup_barcode:
        print(
            "[schema-upgrade] No se creó índice único (tenant_id, barcode): "
            "hay duplicados dentro del mismo tenant."
        )
    else:
        trim_fn = "btrim" if backend == "postgresql" else "trim"
        connection.execute(
            text(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS products_tenant_barcode_unique_idx
                ON products (tenant_id, barcode)
                WHERE tenant_id IS NOT NULL
                  AND barcode IS NOT NULL
                  AND {trim_fn}(barcode) <> ''
                """
            )
        )


def _ensure_payment_methods_tenant_scoped_unique_indexes(connection, backend: str) -> None:
    if backend == "postgresql":
        # Legacy global uniqueness on slug breaks multitenant isolation.
        connection.execute(text("DROP INDEX IF EXISTS ix_payment_methods_slug"))
        connection.execute(
            text(
                "ALTER TABLE IF EXISTS payment_methods "
                "DROP CONSTRAINT IF EXISTS payment_methods_slug_key"
            )
        )
    else:
        connection.execute(text("DROP INDEX IF EXISTS ix_payment_methods_slug"))

    if _has_duplicate_tenant_text(connection, "payment_methods", "slug", backend):
        print(
            "[schema-upgrade] No se creó índice único (tenant_id, slug) en payment_methods: "
            "hay duplicados dentro del mismo tenant."
        )
        return

    trim_fn = "btrim" if backend == "postgresql" else "trim"
    connection.execute(
        text(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS payment_methods_tenant_slug_unique_idx
            ON payment_methods (tenant_id, slug)
            WHERE tenant_id IS NOT NULL
              AND slug IS NOT NULL
              AND {trim_fn}(slug) <> ''
            """
        )
    )


def _has_duplicate_web_order_payment_provider_reference(connection, backend: str) -> bool:
    trim_fn = "btrim" if backend == "postgresql" else "trim"
    row = connection.execute(
        text(
            f"""
            SELECT EXISTS (
              SELECT 1
              FROM web_order_payments
              WHERE provider IS NOT NULL
                AND provider_reference IS NOT NULL
                AND {trim_fn}(provider) <> ''
                AND {trim_fn}(provider_reference) <> ''
              GROUP BY tenant_id, {trim_fn}(provider), {trim_fn}(provider_reference)
              HAVING COUNT(*) > 1
            ) AS has_dup
            """
        )
    ).mappings().first()
    return bool(row and row.get("has_dup"))


def _ensure_web_order_payments_provider_reference_unique_index(connection, backend: str) -> None:
    if _has_duplicate_web_order_payment_provider_reference(connection, backend):
        print(
            "[schema-upgrade] No se creó índice único para web_order_payments "
            "(tenant_id, provider, provider_reference): hay duplicados."
        )
        return

    trim_fn = "btrim" if backend == "postgresql" else "trim"
    connection.execute(
        text(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS web_order_payments_provider_reference_unique_idx
            ON web_order_payments (tenant_id, provider, provider_reference)
            WHERE provider IS NOT NULL
              AND provider_reference IS NOT NULL
              AND {trim_fn}(provider) <> ''
              AND {trim_fn}(provider_reference) <> ''
            """
        )
    )


def _has_duplicate_tenant_number(
    connection,
    table: str,
    column: str,
) -> bool:
    row = connection.execute(
        text(
            f"""
            SELECT EXISTS (
              SELECT 1
              FROM {table}
              WHERE tenant_id IS NOT NULL
                AND {column} IS NOT NULL
              GROUP BY tenant_id, {column}
              HAVING COUNT(*) > 1
            ) AS has_dup
            """
        )
    ).mappings().first()
    return bool(row and row.get("has_dup"))


def _has_duplicate_tenant_text(
    connection,
    table: str,
    column: str,
    backend: str,
) -> bool:
    trim_fn = "btrim" if backend == "postgresql" else "trim"
    row = connection.execute(
        text(
            f"""
            SELECT EXISTS (
              SELECT 1
              FROM {table}
              WHERE tenant_id IS NOT NULL
                AND {column} IS NOT NULL
                AND {trim_fn}({column}) <> ''
              GROUP BY tenant_id, {trim_fn}({column})
              HAVING COUNT(*) > 1
            ) AS has_dup
            """
        )
    ).mappings().first()
    return bool(row and row.get("has_dup"))


def _ensure_pos_document_tenant_scoped_unique_indexes(connection, backend: str) -> None:
    if backend == "postgresql":
        # Remove legacy global unique indexes that break multitenant.
        connection.execute(text("DROP INDEX IF EXISTS ix_sales_sale_number"))
        connection.execute(text("DROP INDEX IF EXISTS ix_sales_document_number"))
        connection.execute(text("DROP INDEX IF EXISTS ix_sale_number_reservations_sale_number"))
        connection.execute(text("DROP INDEX IF EXISTS ix_sale_number_reservations_document_number"))
        connection.execute(text("DROP INDEX IF EXISTS ix_sale_returns_document_number"))
        connection.execute(text("DROP INDEX IF EXISTS ix_sale_changes_document_number"))
        connection.execute(text("DROP INDEX IF EXISTS ix_pos_closures_consecutive"))
        connection.execute(
            text(
                "ALTER TABLE IF EXISTS sales "
                "DROP CONSTRAINT IF EXISTS sales_document_number_key"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE IF EXISTS sale_number_reservations "
                "DROP CONSTRAINT IF EXISTS sale_number_reservations_sale_number_key"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE IF EXISTS sale_number_reservations "
                "DROP CONSTRAINT IF EXISTS sale_number_reservations_document_number_key"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE IF EXISTS sale_returns "
                "DROP CONSTRAINT IF EXISTS sale_returns_document_number_key"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE IF EXISTS sale_changes "
                "DROP CONSTRAINT IF EXISTS sale_changes_document_number_key"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE IF EXISTS pos_closures "
                "DROP CONSTRAINT IF EXISTS pos_closures_consecutive_key"
            )
        )
    else:
        connection.execute(text("DROP INDEX IF EXISTS ix_sales_sale_number"))
        connection.execute(text("DROP INDEX IF EXISTS ix_sales_document_number"))
        connection.execute(text("DROP INDEX IF EXISTS ix_sale_number_reservations_sale_number"))
        connection.execute(text("DROP INDEX IF EXISTS ix_sale_number_reservations_document_number"))
        connection.execute(text("DROP INDEX IF EXISTS ix_sale_returns_document_number"))
        connection.execute(text("DROP INDEX IF EXISTS ix_sale_changes_document_number"))
        connection.execute(text("DROP INDEX IF EXISTS ix_pos_closures_consecutive"))

    trim_fn = "btrim" if backend == "postgresql" else "trim"

    if _has_duplicate_tenant_number(connection, "sales", "sale_number"):
        print(
            "[schema-upgrade] No se creó índice único (tenant_id, sale_number) en sales: "
            "hay duplicados dentro del mismo tenant."
        )
    else:
        connection.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS sales_tenant_sale_number_unique_idx
                ON sales (tenant_id, sale_number)
                WHERE tenant_id IS NOT NULL
                  AND sale_number IS NOT NULL
                """
            )
        )

    if _has_duplicate_tenant_text(connection, "sales", "document_number", backend):
        print(
            "[schema-upgrade] No se creó índice único (tenant_id, document_number) en sales: "
            "hay duplicados dentro del mismo tenant."
        )
    else:
        connection.execute(
            text(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS sales_tenant_document_number_unique_idx
                ON sales (tenant_id, document_number)
                WHERE tenant_id IS NOT NULL
                  AND document_number IS NOT NULL
                  AND {trim_fn}(document_number) <> ''
                """
            )
        )

    if _has_duplicate_tenant_number(connection, "sale_number_reservations", "sale_number"):
        print(
            "[schema-upgrade] No se creó índice único de reservas por tenant para sale_number: "
            "hay duplicados dentro del mismo tenant."
        )
    else:
        connection.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS sale_number_reservations_tenant_sale_number_unique_idx
                ON sale_number_reservations (tenant_id, sale_number)
                WHERE tenant_id IS NOT NULL
                  AND sale_number IS NOT NULL
                """
            )
        )

    if _has_duplicate_tenant_text(connection, "sale_number_reservations", "document_number", backend):
        print(
            "[schema-upgrade] No se creó índice único de reservas por tenant para document_number: "
            "hay duplicados dentro del mismo tenant."
        )
    else:
        connection.execute(
            text(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS sale_number_reservations_tenant_document_number_unique_idx
                ON sale_number_reservations (tenant_id, document_number)
                WHERE tenant_id IS NOT NULL
                  AND document_number IS NOT NULL
                  AND {trim_fn}(document_number) <> ''
                """
            )
        )

    if _has_duplicate_tenant_text(connection, "sale_returns", "document_number", backend):
        print(
            "[schema-upgrade] No se creó índice único (tenant_id, document_number) en sale_returns: "
            "hay duplicados dentro del mismo tenant."
        )
    else:
        connection.execute(
            text(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS sale_returns_tenant_document_number_unique_idx
                ON sale_returns (tenant_id, document_number)
                WHERE tenant_id IS NOT NULL
                  AND document_number IS NOT NULL
                  AND {trim_fn}(document_number) <> ''
                """
            )
        )

    if _has_duplicate_tenant_text(connection, "sale_changes", "document_number", backend):
        print(
            "[schema-upgrade] No se creó índice único (tenant_id, document_number) en sale_changes: "
            "hay duplicados dentro del mismo tenant."
        )
    else:
        connection.execute(
            text(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS sale_changes_tenant_document_number_unique_idx
                ON sale_changes (tenant_id, document_number)
                WHERE tenant_id IS NOT NULL
                  AND document_number IS NOT NULL
                  AND {trim_fn}(document_number) <> ''
                """
            )
        )

    if _has_duplicate_tenant_text(connection, "pos_closures", "consecutive", backend):
        print(
            "[schema-upgrade] No se creó índice único (tenant_id, consecutive) en pos_closures: "
            "hay duplicados dentro del mismo tenant."
        )
    else:
        connection.execute(
            text(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS pos_closures_tenant_consecutive_unique_idx
                ON pos_closures (tenant_id, consecutive)
                WHERE tenant_id IS NOT NULL
                  AND consecutive IS NOT NULL
                  AND {trim_fn}(consecutive) <> ''
                """
            )
        )


def run_schema_upgrades(engine: Engine) -> None:
    """Adds missing columns if they don't exist yet."""

    backend = engine.url.get_backend_name()
    if backend not in {"sqlite", "postgresql"}:
        return

    with engine.connect() as connection:
        with connection.begin():
            if backend == "postgresql":
                _ensure_table_tenants_postgres(connection)
                _seed_default_tenant_postgres(connection)
                _ensure_pos_settings_id_sequence_postgres(connection)
                _ensure_table_document_adjustments_postgres(connection)
                _ensure_table_product_audit_logs_postgres(connection)
                _ensure_table_sale_changes_postgres(connection)
                _ensure_table_pos_sessions_postgres(connection)
                _ensure_table_platform_login_2fa_challenges_postgres(connection)
                _ensure_table_platform_trusted_devices_postgres(connection)
                _ensure_table_user_documents_postgres(connection)
                _ensure_table_hr_employees_postgres(connection)
                _ensure_table_hr_employee_documents_postgres(connection)
                _ensure_table_schedule_templates_postgres(connection)
                _ensure_table_schedule_weeks_postgres(connection)
                _ensure_table_schedule_shifts_postgres(connection)
                _ensure_table_sale_number_reservations_postgres(connection)
                _ensure_table_pos_station_notices_postgres(connection)
                _ensure_table_stock_devices_postgres(connection)
                _ensure_table_demo_signup_audits_postgres(connection)
                _ensure_column_postgres(
                    connection,
                    "sales",
                    "status",
                    "TEXT DEFAULT 'active'",
                )
                _ensure_column_postgres(connection, "tenants", "lifecycle_stage", "TEXT DEFAULT 'active'")
                _ensure_column_postgres(connection, "tenants", "trial_started_at", "TIMESTAMP")
                _ensure_column_postgres(connection, "tenants", "trial_ends_at", "TIMESTAMP")
                _ensure_column_postgres(connection, "tenants", "converted_at", "TIMESTAMP")
                _ensure_column_postgres(connection, "tenants", "enabled_modules", "JSONB")
                _ensure_column_postgres(connection, "tenants", "module_user_access", "JSONB")
                _ensure_column_postgres(
                    connection,
                    "products",
                    "is_investment",
                    "BOOLEAN DEFAULT FALSE",
                )
                _ensure_column_postgres(
                    connection,
                    "products",
                    "investment_enabled_at",
                    "TIMESTAMP",
                )
                _ensure_column_postgres(
                    connection,
                    "products",
                    "investment_disabled_at",
                    "TIMESTAMP",
                )
                _ensure_column_postgres(
                    connection,
                    "products",
                    "investment_status",
                    "TEXT DEFAULT 'active'",
                )
                _ensure_column_postgres(connection, "products", "web_slug", "VARCHAR(160)")
                _ensure_column_postgres(connection, "products", "web_name", "VARCHAR(255)")
                _ensure_column_postgres(
                    connection,
                    "products",
                    "web_published",
                    "BOOLEAN DEFAULT FALSE",
                )
                _ensure_column_postgres(
                    connection,
                    "products",
                    "web_published_at",
                    "TIMESTAMP",
                )
                _ensure_column_postgres(
                    connection,
                    "products",
                    "web_featured",
                    "BOOLEAN DEFAULT FALSE",
                )
                _ensure_column_postgres(
                    connection,
                    "products",
                    "web_short_description",
                    "VARCHAR(280)",
                )
                _ensure_column_postgres(
                    connection,
                    "products",
                    "web_long_description",
                    "TEXT",
                )
                _ensure_column_postgres(
                    connection,
                    "products",
                    "web_compare_price",
                    "DOUBLE PRECISION",
                )
                _ensure_column_postgres(
                    connection,
                    "products",
                    "web_price_source",
                    "TEXT DEFAULT 'base'",
                )
                _ensure_column_postgres(
                    connection,
                    "products",
                    "web_price_value",
                    "DOUBLE PRECISION",
                )
                _ensure_column_postgres(
                    connection,
                    "products",
                    "web_badge_text",
                    "VARCHAR(80)",
                )
                _ensure_column_postgres(
                    connection,
                    "products",
                    "web_sort_order",
                    "INTEGER DEFAULT 0",
                )
                _ensure_column_postgres(
                    connection,
                    "products",
                    "web_visible_when_out_of_stock",
                    "BOOLEAN DEFAULT TRUE",
                )
                _ensure_column_postgres(
                    connection,
                    "products",
                    "web_price_mode",
                    "TEXT DEFAULT 'visible'",
                )
                _ensure_column_postgres(
                    connection,
                    "products",
                    "web_whatsapp_message",
                    "TEXT",
                )
                _ensure_column_postgres(
                    connection,
                    "products",
                    "web_warranty_text",
                    "VARCHAR(160)",
                )
                _ensure_column_postgres(
                    connection,
                    "products",
                    "web_gallery_urls",
                    "TEXT",
                )
                _ensure_column_postgres(
                    connection,
                    "products",
                    "web_category_key",
                    "VARCHAR(64)",
                )
                connection.execute(
                    text(
                        """
                        UPDATE products
                        SET web_published = FALSE
                        WHERE web_published IS NULL
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        UPDATE products
                        SET web_published_at = updated_at
                        WHERE web_published = TRUE
                          AND web_published_at IS NULL
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        UPDATE products
                        SET web_featured = FALSE
                        WHERE web_featured IS NULL
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        UPDATE products
                        SET web_sort_order = 0
                        WHERE web_sort_order IS NULL
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        UPDATE products
                        SET web_visible_when_out_of_stock = TRUE
                        WHERE web_visible_when_out_of_stock IS NULL
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        UPDATE products
                        SET web_price_mode = 'visible'
                        WHERE web_price_mode IS NULL OR btrim(web_price_mode) = ''
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        UPDATE products
                        SET web_price_source = 'base'
                        WHERE web_price_source IS NULL OR btrim(web_price_source) = ''
                        """
                    )
                )
                if _table_exists_postgres(connection, "investment_participants"):
                    _ensure_column_postgres(
                        connection,
                        "investment_participants",
                        "profit_share_percent",
                        "FLOAT DEFAULT 0",
                    )
                    _ensure_column_postgres(
                        connection,
                        "investment_participants",
                        "capital_share_percent",
                        "FLOAT DEFAULT 0",
                    )
                if _table_exists_postgres(connection, "investment_cut_allocations"):
                    _ensure_column_postgres(
                        connection,
                        "investment_cut_allocations",
                        "profit_share_percent",
                        "FLOAT DEFAULT 0",
                    )
                    _ensure_column_postgres(
                        connection,
                        "investment_cut_allocations",
                        "capital_share_percent",
                        "FLOAT DEFAULT 0",
                    )
                    _ensure_column_postgres(
                        connection,
                        "investment_cut_allocations",
                        "profit_amount",
                        "FLOAT DEFAULT 0",
                    )
                    _ensure_column_postgres(
                        connection,
                        "investment_cut_allocations",
                        "capital_amount",
                        "FLOAT DEFAULT 0",
                    )
                if _table_exists_postgres(connection, "investment_cuts"):
                    _ensure_column_postgres(
                        connection,
                        "investment_cuts",
                        "reconciled",
                        "BOOLEAN DEFAULT FALSE",
                    )
                    _ensure_column_postgres(
                        connection,
                        "investment_cuts",
                        "reconciled_at",
                        "TIMESTAMP",
                    )
                    _ensure_column_postgres(
                        connection,
                        "investment_cuts",
                        "reconciled_by_user_id",
                        "INTEGER",
                    )
                _ensure_column_postgres(
                    connection,
                    "sales",
                    "voided_at",
                    "TIMESTAMP",
                )
                _ensure_column_postgres(
                    connection,
                    "sales",
                    "voided_by_user_id",
                    "INTEGER",
                )
                _ensure_column_postgres(
                    connection,
                    "sales",
                    "void_reason",
                    "TEXT",
                )
                _ensure_column_postgres(
                    connection,
                    "sales",
                    "adjustment_reference",
                    "TEXT",
                )
                _ensure_column_postgres(
                    connection,
                    "pos_closures",
                    "change_extra_total",
                    "FLOAT DEFAULT 0",
                )
                _ensure_column_postgres(
                    connection,
                    "pos_closures",
                    "change_refund_total",
                    "FLOAT DEFAULT 0",
                )
                _ensure_column_postgres(
                    connection,
                    "pos_closures",
                    "change_count",
                    "INTEGER DEFAULT 0",
                )
                _ensure_column_postgres(connection, "pos_users", "phone", "TEXT")
                _ensure_column_postgres(connection, "pos_users", "pin_hash", "TEXT")
                _ensure_column_postgres(connection, "pos_users", "position", "TEXT")
                _ensure_column_postgres(connection, "pos_users", "notes", "TEXT")
                _ensure_column_postgres(connection, "pos_users", "avatar_url", "TEXT")
                _ensure_column_postgres(connection, "pos_users", "birth_date", "DATE")
                _ensure_column_postgres(connection, "pos_users", "location", "TEXT")
                _ensure_column_postgres(connection, "pos_users", "bio", "TEXT")
                _ensure_column_postgres(connection, "pos_users", "employee_id", "INTEGER")
                _ensure_column_postgres(connection, "pos_users", "invited_at", "TIMESTAMP")
                _ensure_column_postgres(connection, "pos_users", "accepted_at", "TIMESTAMP")
                _backfill_hr_employees_from_users(connection, backend="postgresql")
                _ensure_column_postgres(connection, "hr_employees", "payroll_frequency", "TEXT")
                _ensure_column_postgres(connection, "hr_employees", "payroll_amount", "FLOAT")
                _ensure_column_postgres(connection, "hr_employees", "payroll_currency", "TEXT")
                _ensure_column_postgres(connection, "hr_employees", "payroll_payment_method", "TEXT")
                _ensure_column_postgres(connection, "hr_employees", "payroll_day_of_week", "TEXT")
                _ensure_column_postgres(connection, "hr_employees", "payroll_day_of_month", "INTEGER")
                _ensure_column_postgres(connection, "hr_employees", "payroll_last_paid_at", "DATE")
                _ensure_column_postgres(connection, "hr_employees", "payroll_next_due_at", "DATE")
                _ensure_column_postgres(connection, "hr_employees", "payroll_reference", "TEXT")
                _ensure_column_postgres(connection, "hr_employees", "payroll_notes", "TEXT")
                _ensure_column_postgres(
                    connection,
                    "pos_stations",
                    "station_email",
                    "TEXT",
                )
                _ensure_column_postgres(
                    connection,
                    "pos_stations",
                    "station_type",
                    "TEXT DEFAULT 'desktop'",
                )
                _ensure_column_postgres(
                    connection,
                    "pos_stations",
                    "parent_station_id",
                    "TEXT",
                )
                _ensure_column_postgres(
                    connection,
                    "pos_stations",
                    "station_password_hash",
                    "TEXT",
                )
                connection.execute(
                    text(
                        "ALTER TABLE IF EXISTS pos_stations ALTER COLUMN pos_user_id DROP NOT NULL"
                    )
                )
                connection.execute(
                    text(
                        "ALTER TABLE IF EXISTS pos_stations ALTER COLUMN pin_hash DROP NOT NULL"
                    )
                )
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS pos_stations_station_email_idx ON pos_stations (station_email)"
                    )
                )
                _ensure_column_postgres(
                    connection,
                    "pos_settings",
                    "web_pos_send_closure_email",
                    "BOOLEAN DEFAULT TRUE",
                )
                _ensure_column_postgres(
                    connection,
                    "pos_settings",
                    "station_closure_email_overrides",
                    "JSONB DEFAULT '{}'::jsonb",
                )
                _ensure_column_postgres(
                    connection,
                    "pos_stations",
                    "bound_device_id",
                    "TEXT",
                )
                _ensure_column_postgres(
                    connection,
                    "pos_stations",
                    "bound_device_label",
                    "TEXT",
                )
                _ensure_column_postgres(
                    connection,
                    "pos_stations",
                    "bound_at",
                    "TIMESTAMP",
                )
                _ensure_column_postgres(
                    connection,
                    "pos_stations",
                    "bound_by_user_id",
                    "INTEGER",
                )
                _ensure_column_postgres(
                    connection,
                    "pos_stations",
                    "bound_by_user_name",
                    "TEXT",
                )
                _ensure_column_postgres(
                    connection,
                    "pos_stations",
                    "printer_mode",
                    "TEXT",
                )
                _ensure_column_postgres(
                    connection,
                    "pos_stations",
                    "printer_name",
                    "TEXT",
                )
                _ensure_column_postgres(
                    connection,
                    "pos_stations",
                    "printer_width",
                    "TEXT",
                )
                _ensure_column_postgres(
                    connection,
                    "pos_stations",
                    "printer_auto_open_drawer",
                    "BOOLEAN",
                )
                _ensure_column_postgres(
                    connection,
                    "pos_stations",
                    "printer_show_drawer_button",
                    "BOOLEAN",
                )
                _ensure_column_postgres(
                    connection,
                    "pos_closures",
                    "station_breakdown",
                    "JSONB",
                )
                _ensure_column_postgres(
                    connection,
                    "sale_returns",
                    "closure_id",
                    "INTEGER",
                )
                _ensure_column_postgres(
                    connection,
                    "sale_returns",
                    "voided_at",
                    "TIMESTAMP",
                )
                _ensure_column_postgres(
                    connection,
                    "sale_returns",
                    "voided_by_user_id",
                    "INTEGER",
                )
                _ensure_column_postgres(
                    connection,
                    "sale_returns",
                    "void_reason",
                    "TEXT",
                )
                _ensure_column_postgres(
                    connection,
                    "sale_returns",
                    "adjustment_reference",
                    "TEXT",
                )
                _ensure_column_postgres(
                    connection,
                    "sale_changes",
                    "voided_at",
                    "TIMESTAMP",
                )
                _ensure_column_postgres(
                    connection,
                    "sale_changes",
                    "voided_by_user_id",
                    "INTEGER",
                )
                _ensure_column_postgres(
                    connection,
                    "sale_changes",
                    "void_reason",
                    "TEXT",
                )
                _ensure_column_postgres(
                    connection,
                    "sale_changes",
                    "adjustment_reference",
                    "TEXT",
                )
                _ensure_column_postgres(
                    connection,
                    "separated_order_payments",
                    "status",
                    "TEXT DEFAULT 'active'",
                )
                _ensure_column_postgres(
                    connection,
                    "separated_order_payments",
                    "voided_at",
                    "TIMESTAMP",
                )
                _ensure_column_postgres(
                    connection,
                    "separated_order_payments",
                    "voided_by_user_id",
                    "INTEGER",
                )
                _ensure_column_postgres(
                    connection,
                    "separated_order_payments",
                    "void_reason",
                    "TEXT",
                )
                _ensure_column_postgres(
                    connection,
                    "separated_order_payments",
                    "adjustment_reference",
                    "TEXT",
                )
                _ensure_column_postgres(connection, "receiving_lots", "supplier_name", "TEXT")
                _ensure_column_postgres(connection, "receiving_lots", "invoice_reference", "TEXT")
                _ensure_column_postgres(connection, "receiving_lots", "source_reference", "TEXT")
                _ensure_column_postgres(connection, "receiving_lots", "stock_device_id", "TEXT")
                _ensure_column_postgres(connection, "receiving_lots", "stock_device_name", "TEXT")
                _ensure_column_postgres(connection, "receiving_lots", "notes", "TEXT")
                _ensure_column_postgres(connection, "receiving_lots", "support_file_name", "TEXT")
                _ensure_column_postgres(connection, "receiving_lots", "support_file_url", "TEXT")
                _ensure_column_postgres(connection, "receiving_lots", "support_file_size", "INTEGER")
                _ensure_column_postgres(
                    connection,
                    "receiving_lot_items",
                    "labels_printed_qty",
                    "INTEGER NOT NULL DEFAULT 0",
                )
                if _table_exists_postgres(connection, "inventory_recounts"):
                    _ensure_column_postgres(
                        connection,
                        "inventory_recounts",
                        "source",
                        "TEXT NOT NULL DEFAULT 'web'",
                    )
                    _ensure_column_postgres(
                        connection,
                        "inventory_recounts",
                        "stock_device_id",
                        "TEXT",
                    )
                    _ensure_column_postgres(
                        connection,
                        "inventory_recounts",
                        "stock_device_name",
                        "TEXT",
                    )
                    connection.execute(
                        text(
                            "UPDATE inventory_recounts SET source = 'web' "
                            "WHERE source IS NULL OR btrim(source) = ''"
                        )
                    )
                _ensure_column_postgres(connection, "products", "tenant_id", "INTEGER")
                _ensure_column_postgres(
                    connection,
                    "products",
                    "label_format",
                    "VARCHAR(64)",
                )
                connection.execute(
                    text(
                        """
                        UPDATE products
                        SET label_format = CASE
                            WHEN lower(coalesce(group_name, '')) LIKE '%cables%' THEN 'Cables_1'
                            ELSE 'Kensar1'
                        END
                        WHERE label_format IS NULL OR btrim(label_format) = ''
                        """
                    )
                )
                _ensure_column_postgres(connection, "product_groups", "tenant_id", "INTEGER")
                _ensure_column_postgres(connection, "payment_methods", "tenant_id", "INTEGER")
                _ensure_column_postgres(connection, "pos_settings", "tenant_id", "INTEGER")
                _ensure_column_postgres(connection, "pos_customers", "tenant_id", "INTEGER")
                _ensure_column_postgres(connection, "pos_users", "tenant_id", "INTEGER")
                _ensure_column_postgres(connection, "pos_stations", "tenant_id", "INTEGER")
                shared_tenant_tables = [
                    "product_audit_logs",
                    "document_adjustments",
                    "sale_number_reservations",
                    "hr_employees",
                    "schedule_templates",
                    "schedule_weeks",
                    "schedule_shifts",
                    "pos_sessions",
                    "pos_user_documents",
                    "hr_employee_documents",
                    "password_resets",
                    "pos_station_notices",
                    "stock_devices",
                    "sale_return_items",
                    "sale_return_payments",
                    "sale_change_return_items",
                    "sale_change_new_items",
                    "sale_change_payments",
                ]
                transactional_tenant_tables = [
                    "inventory_movements",
                    "receiving_lots",
                    "receiving_lot_items",
                    "sales",
                    "sale_items",
                    "sale_payments",
                    "sale_returns",
                    "sale_changes",
                    "pos_closures",
                    "separated_orders",
                    "separated_order_payments",
                ]
                for table in transactional_tenant_tables:
                    _ensure_column_postgres(connection, table, "tenant_id", "INTEGER")
                _ensure_column_postgres(
                    connection,
                    "receiving_lot_items",
                    "label_format_snapshot",
                    "VARCHAR(64)",
                )
                connection.execute(
                    text(
                        """
                        UPDATE receiving_lot_items
                        SET label_format_snapshot = 'Kensar1'
                        WHERE label_format_snapshot IS NULL OR btrim(label_format_snapshot) = ''
                        """
                    )
                )
                for table in shared_tenant_tables:
                    _ensure_column_postgres(connection, table, "tenant_id", "INTEGER")
                _ensure_tenant_fk_postgres(connection, "products")
                _ensure_tenant_fk_postgres(connection, "product_groups")
                _ensure_tenant_fk_postgres(connection, "payment_methods")
                _ensure_tenant_fk_postgres(connection, "pos_settings")
                _ensure_tenant_fk_postgres(connection, "pos_customers")
                _ensure_tenant_fk_postgres(connection, "pos_users")
                _ensure_tenant_fk_postgres(connection, "pos_stations")
                for table in transactional_tenant_tables:
                    _ensure_tenant_fk_postgres(connection, table)
                for table in shared_tenant_tables:
                    _ensure_tenant_fk_postgres(connection, table)
                _ensure_tenant_index_postgres(connection, "products")
                _ensure_tenant_index_postgres(connection, "product_groups")
                _ensure_tenant_index_postgres(connection, "payment_methods")
                _ensure_tenant_index_postgres(connection, "pos_settings")
                _ensure_tenant_index_postgres(connection, "pos_customers")
                _ensure_tenant_index_postgres(connection, "pos_users")
                _ensure_tenant_index_postgres(connection, "pos_stations")
                for table in transactional_tenant_tables:
                    _ensure_tenant_index_postgres(connection, table)
                for table in shared_tenant_tables:
                    _ensure_tenant_index_postgres(connection, table)
                _ensure_column_postgres(connection, "web_orders", "customer_approval_email_sent_at", "TIMESTAMP")
                _ensure_column_postgres(connection, "web_orders", "customer_approval_email_last_error", "TEXT")
                _ensure_column_postgres(connection, "web_orders", "internal_approval_email_sent_at", "TIMESTAMP")
                _ensure_column_postgres(connection, "web_orders", "internal_approval_email_last_error", "TEXT")
                _ensure_products_tenant_scoped_unique_indexes(connection, backend="postgresql")
                _ensure_payment_methods_tenant_scoped_unique_indexes(connection, backend="postgresql")
                _ensure_pos_document_tenant_scoped_unique_indexes(connection, backend="postgresql")
                _ensure_web_order_payments_provider_reference_unique_index(
                    connection,
                    backend="postgresql",
                )
                _backfill_legacy_users_to_default_tenant_postgres(connection)
                _backfill_company_name_from_tenant_postgres(connection)
                _ensure_web_discount_code_schema(connection, backend="postgresql")
                _ensure_web_cart_coupon_schema(connection, backend="postgresql")
                _ensure_web_catalog_category_home_schema(connection, backend="postgresql")
                return
            if backend == "sqlite":
                _ensure_table_tenants(connection)
                _ensure_table_demo_signup_audits(connection)
                _seed_default_tenant_sqlite(connection)
                _ensure_column(
                    connection,
                    "sales",
                    "status",
                    "TEXT DEFAULT 'active'",
                )
                _ensure_column(connection, "tenants", "lifecycle_stage", "TEXT DEFAULT 'active'")
                _ensure_column(connection, "tenants", "trial_started_at", "TIMESTAMP")
                _ensure_column(connection, "tenants", "trial_ends_at", "TIMESTAMP")
                _ensure_column(connection, "tenants", "converted_at", "TIMESTAMP")
                _ensure_column(connection, "tenants", "enabled_modules", "TEXT")
                _ensure_column(connection, "tenants", "module_user_access", "TEXT")
                _ensure_column(
                    connection,
                    "products",
                    "is_investment",
                    "INTEGER NOT NULL DEFAULT 0",
                )
                _ensure_column(
                    connection,
                    "products",
                    "investment_enabled_at",
                    "TIMESTAMP",
                )
                _ensure_column(
                    connection,
                    "products",
                    "investment_disabled_at",
                    "TIMESTAMP",
                )
                _ensure_column(
                    connection,
                    "products",
                    "investment_status",
                    "TEXT DEFAULT 'active'",
                )
                if _table_exists(connection, "investment_participants"):
                    _ensure_column(
                        connection,
                        "investment_participants",
                        "profit_share_percent",
                        "FLOAT DEFAULT 0",
                    )
                    _ensure_column(
                        connection,
                        "investment_participants",
                        "capital_share_percent",
                        "FLOAT DEFAULT 0",
                    )
                if _table_exists(connection, "investment_cut_allocations"):
                    _ensure_column(
                        connection,
                        "investment_cut_allocations",
                        "profit_share_percent",
                        "FLOAT DEFAULT 0",
                    )
                    _ensure_column(
                        connection,
                        "investment_cut_allocations",
                        "capital_share_percent",
                        "FLOAT DEFAULT 0",
                    )
                    _ensure_column(
                        connection,
                        "investment_cut_allocations",
                        "profit_amount",
                        "FLOAT DEFAULT 0",
                    )
                    _ensure_column(
                        connection,
                        "investment_cut_allocations",
                        "capital_amount",
                        "FLOAT DEFAULT 0",
                    )
                if _table_exists(connection, "investment_cuts"):
                    _ensure_column(
                        connection,
                        "investment_cuts",
                        "reconciled",
                        "INTEGER NOT NULL DEFAULT 0",
                    )
                    _ensure_column(
                        connection,
                        "investment_cuts",
                        "reconciled_at",
                        "TIMESTAMP",
                    )
                    _ensure_column(
                        connection,
                        "investment_cuts",
                        "reconciled_by_user_id",
                        "INTEGER",
                    )
                _ensure_column(
                    connection,
                    "sales",
                    "voided_at",
                    "TIMESTAMP",
                )
                _ensure_column(
                    connection,
                    "sales",
                    "voided_by_user_id",
                    "INTEGER",
                )
                _ensure_column(
                    connection,
                    "sales",
                    "void_reason",
                    "TEXT",
                )
                _ensure_column(
                    connection,
                    "sales",
                    "adjustment_reference",
                    "TEXT",
                )
                _ensure_column(
                    connection,
                    "sales",
                    "cart_discount_value",
                    "FLOAT DEFAULT 0",
                )
                _ensure_column(
                    connection,
                    "sales",
                    "cart_discount_percent",
                    "FLOAT DEFAULT 0",
                )
                _ensure_column(
                    connection,
                    "sales",
                    "surcharge_amount",
                    "FLOAT DEFAULT 0",
                )
                _ensure_column(
                    connection,
                    "sales",
                    "surcharge_label",
                    "VARCHAR(60)",
                )
                _ensure_column(
                    connection,
                    "products",
                    "image_url",
                    "TEXT",
                )
                _ensure_column(
                    connection,
                    "products",
                    "image_thumb_url",
                    "TEXT",
                )
                _ensure_column(
                    connection,
                    "products",
                    "label_format",
                    "VARCHAR(64)",
                )
                connection.execute(
                    text(
                        """
                        UPDATE products
                        SET label_format = CASE
                            WHEN lower(coalesce(group_name, '')) LIKE '%cables%' THEN 'Cables_1'
                            ELSE 'Kensar1'
                        END
                        WHERE label_format IS NULL OR trim(label_format) = ''
                        """
                    )
                )
                _ensure_column(
                    connection,
                    "sale_returns",
                    "closure_id",
                    "INTEGER",
                )
                _ensure_column(
                    connection,
                    "sale_returns",
                    "voided_at",
                    "TIMESTAMP",
                )
                _ensure_column(
                    connection,
                    "sale_returns",
                    "voided_by_user_id",
                    "INTEGER",
                )
                _ensure_column(
                    connection,
                    "sale_returns",
                    "void_reason",
                    "TEXT",
                )
                _ensure_column(
                    connection,
                    "sale_returns",
                    "adjustment_reference",
                    "TEXT",
                )
                _ensure_column(
                    connection,
                    "products",
                    "tile_color",
                    "VARCHAR(7)",
                )
                _ensure_column(
                    connection,
                    "products",
                    "updated_at",
                    "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
                )
                if _table_exists(connection, "receiving_lot_items"):
                    _ensure_column(
                        connection,
                        "receiving_lot_items",
                        "labels_printed_qty",
                        "INTEGER NOT NULL DEFAULT 0",
                    )
                    _ensure_column(
                        connection,
                        "receiving_lot_items",
                        "label_format_snapshot",
                        "VARCHAR(64)",
                    )
                    connection.execute(
                        text(
                            """
                            UPDATE receiving_lot_items
                            SET label_format_snapshot = 'Kensar1'
                            WHERE label_format_snapshot IS NULL OR trim(label_format_snapshot) = ''
                            """
                        )
                    )
                if _table_exists(connection, "products"):
                    _ensure_column(connection, "products", "tenant_id", "INTEGER")
                    _ensure_column(connection, "products", "web_slug", "TEXT")
                    _ensure_column(connection, "products", "web_name", "TEXT")
                    _ensure_column(connection, "products", "web_published", "BOOLEAN DEFAULT 0")
                    _ensure_column(connection, "products", "web_published_at", "TIMESTAMP")
                    _ensure_column(connection, "products", "web_featured", "BOOLEAN DEFAULT 0")
                    _ensure_column(connection, "products", "web_short_description", "TEXT")
                    _ensure_column(connection, "products", "web_long_description", "TEXT")
                    _ensure_column(connection, "products", "web_compare_price", "REAL")
                    _ensure_column(connection, "products", "web_price_source", "TEXT DEFAULT 'base'")
                    _ensure_column(connection, "products", "web_price_value", "REAL")
                    _ensure_column(connection, "products", "web_badge_text", "TEXT")
                    _ensure_column(connection, "products", "web_sort_order", "INTEGER DEFAULT 0")
                    _ensure_column(
                        connection,
                        "products",
                        "web_visible_when_out_of_stock",
                        "BOOLEAN DEFAULT 1",
                    )
                    _ensure_column(connection, "products", "web_price_mode", "TEXT DEFAULT 'visible'")
                    _ensure_column(connection, "products", "web_whatsapp_message", "TEXT")
                    _ensure_column(connection, "products", "web_warranty_text", "TEXT")
                    _ensure_column(connection, "products", "web_gallery_urls", "TEXT")
                    _ensure_column(connection, "products", "web_category_key", "TEXT")
                    connection.execute(
                        text(
                            """
                            UPDATE products
                            SET web_published = 0
                            WHERE web_published IS NULL
                            """
                        )
                    )
                    connection.execute(
                        text(
                            """
                            UPDATE products
                            SET web_published_at = updated_at
                            WHERE web_published = 1
                              AND web_published_at IS NULL
                            """
                        )
                    )
                    connection.execute(
                        text(
                            """
                            UPDATE products
                            SET web_featured = 0
                            WHERE web_featured IS NULL
                            """
                        )
                    )
                    connection.execute(
                        text(
                            """
                            UPDATE products
                            SET web_sort_order = 0
                            WHERE web_sort_order IS NULL
                            """
                        )
                    )
                    connection.execute(
                        text(
                            """
                            UPDATE products
                            SET web_visible_when_out_of_stock = 1
                            WHERE web_visible_when_out_of_stock IS NULL
                            """
                        )
                    )
                    connection.execute(
                        text(
                            """
                            UPDATE products
                            SET web_price_mode = 'visible'
                            WHERE web_price_mode IS NULL OR trim(web_price_mode) = ''
                            """
                        )
                    )
                    connection.execute(
                        text(
                            """
                            UPDATE products
                            SET web_price_source = 'base'
                            WHERE web_price_source IS NULL OR trim(web_price_source) = ''
                            """
                        )
                    )
                if _table_exists(connection, "product_groups"):
                    _ensure_column(connection, "product_groups", "tenant_id", "INTEGER")
                if _table_exists(connection, "payment_methods"):
                    _ensure_column(connection, "payment_methods", "tenant_id", "INTEGER")
                if _table_exists(connection, "pos_settings"):
                    _ensure_column(connection, "pos_settings", "tenant_id", "INTEGER")
                if _table_exists(connection, "pos_customers"):
                    _ensure_column(connection, "pos_customers", "tenant_id", "INTEGER")
                if _table_exists(connection, "pos_users"):
                    _ensure_column(connection, "pos_users", "tenant_id", "INTEGER")
                    _backfill_legacy_users_to_default_tenant_sqlite(connection)
                if _table_exists(connection, "pos_stations"):
                    _ensure_column(connection, "pos_stations", "tenant_id", "INTEGER")
                shared_tenant_tables = [
                    "product_audit_logs",
                    "document_adjustments",
                    "sale_number_reservations",
                    "hr_employees",
                    "schedule_templates",
                    "schedule_weeks",
                    "schedule_shifts",
                    "pos_sessions",
                    "pos_user_documents",
                    "hr_employee_documents",
                    "password_resets",
                    "pos_station_notices",
                    "stock_devices",
                    "sale_return_items",
                    "sale_return_payments",
                    "sale_change_return_items",
                    "sale_change_new_items",
                    "sale_change_payments",
                ]
                transactional_tenant_tables = [
                    "inventory_movements",
                    "receiving_lots",
                    "receiving_lot_items",
                    "sales",
                    "sale_items",
                    "sale_payments",
                    "sale_returns",
                    "sale_changes",
                    "pos_closures",
                    "separated_orders",
                    "separated_order_payments",
                ]
                for table in transactional_tenant_tables:
                    if _table_exists(connection, table):
                        _ensure_column(connection, table, "tenant_id", "INTEGER")
                for table in shared_tenant_tables:
                    if _table_exists(connection, table):
                        _ensure_column(connection, table, "tenant_id", "INTEGER")
                _ensure_table_password_resets(connection)
                _ensure_table_pos_sessions(connection)
                _ensure_table_platform_login_2fa_challenges(connection)
                _ensure_table_platform_trusted_devices(connection)
                _ensure_table_user_documents(connection)
                _ensure_table_hr_employees(connection)
                _ensure_table_hr_employee_documents(connection)
                _ensure_table_schedule_templates(connection)
                _ensure_table_schedule_weeks(connection)
                _ensure_table_schedule_shifts(connection)
                # Second pass after ensure_table_* calls so fresh DBs also get tenant_id.
                for table in transactional_tenant_tables:
                    if _table_exists(connection, table):
                        _ensure_column(connection, table, "tenant_id", "INTEGER")
                for table in shared_tenant_tables:
                    if _table_exists(connection, table):
                        _ensure_column(connection, table, "tenant_id", "INTEGER")
                _ensure_table_payment_methods(connection)
                _seed_default_payment_methods(connection)
                _ensure_table_document_adjustments(connection)
                _ensure_table_product_audit_logs(connection)
                _ensure_table_sale_number_reservations(connection)
                _ensure_column(
                    connection,
                    "sales",
                    "customer_id",
                    "INTEGER",
                )
                _ensure_column(
                    connection,
                    "sales",
                    "pos_name",
                    "TEXT",
                )
                _ensure_column(
                    connection,
                    "sales",
                    "station_id",
                    "TEXT",
                )
                _ensure_column(
                    connection,
                    "sales",
                    "vendor_name",
                    "TEXT",
                )
                _ensure_column(
                    connection,
                    "sales",
                    "customer_phone",
                    "TEXT",
                )
                _ensure_column(
                    connection,
                    "sales",
                    "customer_email",
                    "TEXT",
                )
                _ensure_column(
                    connection,
                    "sales",
                    "customer_tax_id",
                    "TEXT",
                )
                _ensure_column(
                    connection,
                    "sales",
                    "customer_address",
                    "TEXT",
                )
                _ensure_table_pos_stations(connection)
                _ensure_table_pos_station_notices(connection)
                _ensure_table_stock_devices(connection)
                _relax_pos_station_schema_sqlite(connection)
                _ensure_table_sale_changes(connection)
                _ensure_column(connection, "sale_changes", "voided_at", "DATETIME")
                _ensure_column(connection, "sale_changes", "voided_by_user_id", "INTEGER")
                _ensure_column(connection, "sale_changes", "void_reason", "TEXT")
                _ensure_column(connection, "sale_changes", "adjustment_reference", "TEXT")
                _backfill_company_name_from_tenant_sqlite(connection)
                _ensure_column(
                    connection,
                    "separated_order_payments",
                    "status",
                    "TEXT DEFAULT 'active'",
                )
                _ensure_column(
                    connection,
                    "separated_order_payments",
                    "voided_at",
                    "DATETIME",
                )
                _ensure_column(
                    connection,
                    "separated_order_payments",
                    "voided_by_user_id",
                    "INTEGER",
                )
                _ensure_column(
                    connection,
                    "separated_order_payments",
                    "void_reason",
                    "TEXT",
                )
                _ensure_column(
                    connection,
                    "separated_order_payments",
                    "adjustment_reference",
                    "TEXT",
                )
                if _table_exists(connection, "receiving_lots"):
                    _ensure_column(connection, "receiving_lots", "supplier_name", "TEXT")
                    _ensure_column(connection, "receiving_lots", "invoice_reference", "TEXT")
                    _ensure_column(connection, "receiving_lots", "source_reference", "TEXT")
                    _ensure_column(connection, "receiving_lots", "stock_device_id", "TEXT")
                    _ensure_column(connection, "receiving_lots", "stock_device_name", "TEXT")
                    _ensure_column(connection, "receiving_lots", "notes", "TEXT")
                    _ensure_column(connection, "receiving_lots", "support_file_name", "TEXT")
                    _ensure_column(connection, "receiving_lots", "support_file_url", "TEXT")
                    _ensure_column(connection, "receiving_lots", "support_file_size", "INTEGER")
                if _table_exists(connection, "inventory_recounts"):
                    _ensure_column(
                        connection,
                        "inventory_recounts",
                        "source",
                        "TEXT NOT NULL DEFAULT 'web'",
                    )
                    _ensure_column(connection, "inventory_recounts", "stock_device_id", "TEXT")
                    _ensure_column(connection, "inventory_recounts", "stock_device_name", "TEXT")
                    connection.execute(
                        text(
                            "UPDATE inventory_recounts SET source = 'web' "
                            "WHERE source IS NULL OR trim(source) = ''"
                        )
                    )
                _ensure_column(connection, "pos_users", "phone", "TEXT")
                _ensure_column(connection, "pos_users", "pin_hash", "TEXT")
                _ensure_column(connection, "pos_users", "position", "TEXT")
                _ensure_column(connection, "pos_users", "notes", "TEXT")
                _ensure_column(connection, "pos_users", "avatar_url", "TEXT")
                _ensure_column(connection, "pos_users", "birth_date", "DATE")
                _ensure_column(connection, "pos_users", "location", "TEXT")
                _ensure_column(connection, "pos_users", "bio", "TEXT")
                _ensure_column(
                    connection,
                    "pos_stations",
                    "station_type",
                    "TEXT DEFAULT 'desktop'",
                )
                _ensure_column(connection, "pos_users", "employee_id", "INTEGER")
                _ensure_column(connection, "pos_users", "invited_at", "DATETIME")
                _ensure_column(connection, "pos_users", "accepted_at", "DATETIME")
                _backfill_hr_employees_from_users(connection, backend="sqlite")
                _ensure_column(connection, "hr_employees", "payroll_frequency", "TEXT")
                _ensure_column(connection, "hr_employees", "payroll_amount", "FLOAT")
                _ensure_column(connection, "hr_employees", "payroll_currency", "TEXT")
                _ensure_column(connection, "hr_employees", "payroll_payment_method", "TEXT")
                _ensure_column(connection, "hr_employees", "payroll_day_of_week", "TEXT")
                _ensure_column(connection, "hr_employees", "payroll_day_of_month", "INTEGER")
                _ensure_column(connection, "hr_employees", "payroll_last_paid_at", "DATE")
                _ensure_column(connection, "hr_employees", "payroll_next_due_at", "DATE")
                _ensure_column(connection, "hr_employees", "payroll_reference", "TEXT")
                _ensure_column(connection, "hr_employees", "payroll_notes", "TEXT")
                if _table_exists(connection, "pos_closures"):
                    _ensure_column(
                        connection,
                        "pos_closures",
                        "change_extra_total",
                        "FLOAT DEFAULT 0",
                    )
                    _ensure_column(
                        connection,
                        "pos_closures",
                        "change_refund_total",
                        "FLOAT DEFAULT 0",
                    )
                    _ensure_column(
                        connection,
                        "pos_closures",
                        "change_count",
                        "INTEGER DEFAULT 0",
                    )
                if _table_exists(connection, "pos_settings"):
                    _ensure_column(
                        connection,
                        "pos_settings",
                        "ticket_logo_url",
                        "TEXT",
                    )
                    _ensure_column(
                        connection,
                        "pos_settings",
                        "closure_email_recipients",
                        "TEXT DEFAULT '[]'",
                    )
                    _ensure_column(
                        connection,
                        "pos_settings",
                        "ticket_email_cc",
                        "TEXT DEFAULT '[]'",
                    )
                    _ensure_column(
                        connection,
                        "pos_settings",
                        "smtp_host",
                        "TEXT DEFAULT ''",
                    )
                    _ensure_column(
                        connection,
                        "pos_settings",
                        "smtp_port",
                        "INTEGER DEFAULT 0",
                    )
                    _ensure_column(
                        connection,
                        "pos_settings",
                        "smtp_user",
                        "TEXT DEFAULT ''",
                    )
                    _ensure_column(
                        connection,
                        "pos_settings",
                        "smtp_password",
                        "TEXT DEFAULT ''",
                    )
                    _ensure_column(
                        connection,
                        "pos_settings",
                        "smtp_use_tls",
                        "BOOLEAN DEFAULT 1",
                    )
                    _ensure_column(
                        connection,
                        "pos_settings",
                        "email_from",
                        "TEXT DEFAULT ''",
                    )
                    _ensure_column(
                        connection,
                        "pos_settings",
                        "role_permissions",
                        "TEXT",
                    )
                    _ensure_column(
                        connection,
                        "pos_settings",
                        "web_pos_send_closure_email",
                        "BOOLEAN DEFAULT 1",
                    )
                    _ensure_column(
                        connection,
                        "pos_settings",
                        "station_closure_email_overrides",
                        "TEXT DEFAULT '{}'",
                    )
            if not _table_exists(connection, "pos_closures"):
                connection.execute(
                    text(
                        """
                        CREATE TABLE pos_closures (
                            id INTEGER PRIMARY KEY,
                            pos_name TEXT,
                            pos_identifier TEXT,
                            station_id TEXT,
                            closed_by_user_id INTEGER NOT NULL,
                            closed_by_user_name TEXT NOT NULL,
                            opened_at DATETIME,
                            closed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            consecutive TEXT,
                            total_amount REAL DEFAULT 0,
                            total_cash REAL DEFAULT 0,
                            total_card REAL DEFAULT 0,
                            total_qr REAL DEFAULT 0,
                            total_nequi REAL DEFAULT 0,
                            total_daviplata REAL DEFAULT 0,
                            total_credit REAL DEFAULT 0,
                            total_refunds REAL DEFAULT 0,
                            net_amount REAL DEFAULT 0,
                            counted_cash REAL DEFAULT 0,
                            difference REAL DEFAULT 0,
                            notes TEXT,
                            station_breakdown TEXT,
                            FOREIGN KEY(closed_by_user_id) REFERENCES pos_users(id)
                        )
                        """
                    )
                )
            _ensure_column(
                connection,
                "pos_closures",
                "station_breakdown",
                "TEXT",
            )
            _ensure_column(
                connection,
                "sales",
                "refunded_total",
                "FLOAT DEFAULT 0",
            )
            _ensure_column(
                connection,
                "sales",
                "refund_count",
                "INTEGER DEFAULT 0",
            )
            _ensure_column(
                connection,
                "sales",
                "closure_id",
                "INTEGER",
            )
            _ensure_column(
                connection,
                "sale_items",
                "unit_price_original",
                "FLOAT DEFAULT 0",
            )
            _ensure_column(
                connection,
                "sale_items",
                "line_discount_value",
                "FLOAT DEFAULT 0",
            )

            if not _table_exists(connection, "pos_customers"):
                connection.execute(
                    text(
                        """
                        CREATE TABLE pos_customers (
                            id INTEGER PRIMARY KEY,
                            name TEXT NOT NULL,
                            phone TEXT,
                            email TEXT,
                            tax_id TEXT,
                            address TEXT,
                            is_active BOOLEAN NOT NULL DEFAULT 1,
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                )

            if not _table_exists(connection, "product_groups"):
                connection.execute(
                    text(
                        """
                        CREATE TABLE product_groups (
                            id INTEGER PRIMARY KEY,
                            path TEXT UNIQUE NOT NULL,
                            display_name TEXT NOT NULL,
                            parent_path TEXT,
                            image_url TEXT,
                            image_thumb_url TEXT,
                            tile_color VARCHAR(7),
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                )
            else:
                _ensure_column(
                    connection,
                    "product_groups",
                    "tile_color",
                    "VARCHAR(7)",
                )

            # Backfill to keep legacy registros consistentes.
            connection.execute(
                text(
                    """
                    UPDATE sale_items
                    SET unit_price_original = unit_price
                    WHERE unit_price_original IS NULL OR unit_price_original = 0
                    """
                )
            )
            if _table_exists(connection, "pos_closures"):
                _ensure_column(
                    connection,
                    "pos_closures",
                    "station_id",
                    "TEXT",
                )
                _ensure_column(
                    connection,
                    "pos_closures",
                    "sales_count",
                    "INTEGER DEFAULT 0",
                )
                _ensure_column(
                    connection,
                    "pos_closures",
                    "total_surcharge",
                    "FLOAT DEFAULT 0",
                )
            connection.execute(
                text(
                    """
                    UPDATE sale_items
                    SET line_discount_value = discount
                    WHERE line_discount_value IS NULL OR line_discount_value = 0
                    """
                )
            )

            connection.execute(
                text(
                    """
                    UPDATE sales
                    SET cart_discount_value = 0
                    WHERE cart_discount_value IS NULL
                    """
                )
            )
            connection.execute(
                text(
                    """
                    UPDATE sales
                    SET cart_discount_percent = 0
                    WHERE cart_discount_percent IS NULL
                    """
                )
            )
            connection.execute(
                text(
                    """
                    UPDATE sales
                    SET refunded_total = 0
                    WHERE refunded_total IS NULL
                    """
                )
            )
            connection.execute(
                text(
                    """
                    UPDATE sales
                    SET refund_count = 0
                    WHERE refund_count IS NULL
                    """
                )
            )

            # Ajuste de registros previos: si el total guardado fue el subtotal sin
            # descuento de carrito, forzamos total = paid_amount (cuando no hubo cambio).
            connection.execute(
                text(
                    """
                    UPDATE sales
                    SET total = paid_amount
                    WHERE total > paid_amount + 0.01
                      AND change_amount = 0
                    """
                )
            )

            # Rellenar cart_discount_value a partir de la diferencia entre los
            # totales de ítems y el total cobrado.
            connection.execute(
                text(
                    """
                    WITH line_totals AS (
                        SELECT sale_id, SUM(total) AS line_total
                        FROM sale_items
                        GROUP BY sale_id
                    )
                    UPDATE sales
                    SET cart_discount_value = (
                        SELECT line_total - sales.total
                        FROM line_totals
                        WHERE sale_id = sales.id
                    )
                    WHERE (cart_discount_value IS NULL OR cart_discount_value = 0)
                      AND EXISTS (
                          SELECT 1 FROM line_totals lt
                          WHERE lt.sale_id = sales.id
                            AND lt.line_total > sales.total + 0.01
                    )
                    """
                )
            )

            if _table_exists(connection, "pos_settings"):
                connection.execute(
                    text(
                        """
                        INSERT INTO pos_settings (
                            id,
                            company_name,
                            tax_id,
                            address,
                            contact_email,
                            contact_phone,
                            theme_mode,
                            accent_color,
                            ticket_footer,
                            auto_close_ticket,
                            low_stock_alert,
                            require_seller_pin,
                            notifications,
                            logo_url
                        )
                        SELECT 1,
                               'Mi Negocio',
                               NULL,
                               NULL,
                               NULL,
                               NULL,
                               'light',
                               '#0A84FF',
                               NULL,
                               0,
                               1,
                               0,
                               '{"daily_summary_email": false, "cash_alert_email": false, "cash_alert_sms": false, "monthly_report_email": false}',
                               NULL
                        WHERE NOT EXISTS (SELECT 1 FROM pos_settings WHERE id = 1)
                        """
                )
            )

            if not _table_exists(connection, "separated_orders"):
                connection.execute(
                    text(
                        """
                        CREATE TABLE separated_orders (
                            id INTEGER PRIMARY KEY,
                            sale_id INTEGER NOT NULL UNIQUE,
                            customer_id INTEGER,
                            customer_name TEXT,
                            customer_phone TEXT,
                            customer_email TEXT,
                            total_amount REAL NOT NULL DEFAULT 0,
                            initial_payment REAL NOT NULL DEFAULT 0,
                            balance REAL NOT NULL DEFAULT 0,
                            due_date DATETIME,
                            status TEXT NOT NULL DEFAULT 'reservado',
                            sale_document_number TEXT NOT NULL,
                            sale_number INTEGER,
                            barcode TEXT,
                            notes TEXT,
                            surcharge_amount REAL NOT NULL DEFAULT 0,
                            surcharge_label VARCHAR(60),
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            completed_at DATETIME,
                            cancelled_at DATETIME,
                            FOREIGN KEY(sale_id) REFERENCES sales(id) ON DELETE CASCADE,
                            FOREIGN KEY(customer_id) REFERENCES pos_customers(id)
                        )
                        """
                    )
                )
            else:
                _ensure_column(connection, "separated_orders", "customer_phone", "TEXT")
                _ensure_column(connection, "separated_orders", "customer_email", "TEXT")
                _ensure_column(
                    connection,
                    "separated_orders",
                    "sale_document_number",
                    "TEXT",
                )
                _ensure_column(
                    connection,
                    "separated_orders",
                    "sale_number",
                    "INTEGER",
                )
                _ensure_column(
                    connection,
                    "separated_orders",
                    "barcode",
                    "TEXT",
                )
                _ensure_column(connection, "separated_orders", "notes", "TEXT")
                _ensure_column(
                    connection,
                    "separated_orders",
                    "surcharge_amount",
                    "FLOAT DEFAULT 0",
                )
                _ensure_column(
                    connection,
                    "separated_orders",
                    "surcharge_label",
                    "VARCHAR(60)",
                )
                _ensure_column(
                    connection,
                    "separated_orders",
                    "completed_at",
                    "DATETIME",
                )
                _ensure_column(
                    connection,
                    "separated_orders",
                    "cancelled_at",
                    "DATETIME",
                )

            if not _table_exists(connection, "separated_order_payments"):
                connection.execute(
                    text(
                        """
                        CREATE TABLE separated_order_payments (
                            id INTEGER PRIMARY KEY,
                            separated_order_id INTEGER NOT NULL,
                            method TEXT NOT NULL,
                            amount REAL NOT NULL DEFAULT 0,
                            paid_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            reference TEXT,
                            note TEXT,
                            closure_id INTEGER,
                            station_id TEXT,
                            FOREIGN KEY(separated_order_id) REFERENCES separated_orders(id) ON DELETE CASCADE,
                            FOREIGN KEY(station_id) REFERENCES pos_stations(id),
                            FOREIGN KEY(closure_id) REFERENCES pos_closures(id)
                        )
                        """
                    )
                )
            else:
                _ensure_column(
                    connection,
                    "separated_order_payments",
                    "reference",
                    "TEXT",
                )
                _ensure_column(
                    connection,
                    "separated_order_payments",
                    "note",
                    "TEXT",
                )
                _ensure_column(
                    connection,
                    "separated_order_payments",
                    "closure_id",
                    "INTEGER",
                )
                _ensure_column(
                    connection,
                    "separated_order_payments",
                    "station_id",
                    "TEXT",
                )

            if _table_exists(connection, "pos_users"):
                master_hash = hash_password("2301")
                _ensure_column(
                    connection,
                    "pos_users",
                    "tenant_id",
                    "INTEGER",
                )
                _ensure_column(
                    connection,
                    "pos_users",
                    "password_hash",
                    "TEXT",
                )
                _ensure_column(
                    connection,
                    "pos_users",
                    "is_active",
                    "BOOLEAN DEFAULT 1",
                )
                _ensure_column(
                    connection,
                    "pos_users",
                    "last_login",
                    "DATETIME",
                )

                connection.execute(
                    text(
                        """
                        UPDATE pos_users
                        SET password_hash = :default_hash
                        WHERE password_hash IS NULL OR password_hash = ''
                        """
                    ),
                    {"default_hash": hash_password("changeme")},
                )

                connection.execute(
                    text(
                        """
                        INSERT INTO pos_users (
                            name, email, role, status, is_active, tenant_id, password_hash, created_at
                        )
                        SELECT
                            :name,
                            :email,
                            'Administrador',
                            'Activo',
                            1,
                            (SELECT id FROM tenants WHERE slug = 'kensar' LIMIT 1),
                            :hash,
                            CURRENT_TIMESTAMP
                        WHERE NOT EXISTS (
                            SELECT 1 FROM pos_users WHERE email = :email
                        )
                        """
                    ),
                    {
                        "name": "Kenneth Jaramillo",
                        "email": "master@kensar.com",
                        "hash": master_hash,
                    },
                )
                _backfill_legacy_users_to_default_tenant_sqlite(connection)
                _ensure_web_discount_code_schema(connection, backend="sqlite")
                _ensure_web_cart_coupon_schema(connection, backend="sqlite")
                _ensure_web_catalog_category_home_schema(connection, backend="sqlite")


def _ensure_table_password_resets(connection) -> None:
    if not _table_exists(connection, "password_resets"):
        connection.execute(
            text(
                """
                CREATE TABLE password_resets (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    expires_at DATETIME NOT NULL,
                    used_at DATETIME,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES pos_users(id)
                )
                """
            )
        )
    else:
        _ensure_column(connection, "password_resets", "user_id", "INTEGER")
        _ensure_column(connection, "password_resets", "token_hash", "TEXT")
        _ensure_column(connection, "password_resets", "expires_at", "DATETIME")
        _ensure_column(connection, "password_resets", "used_at", "DATETIME")
        _ensure_column(connection, "password_resets", "created_at", "DATETIME")


def _ensure_table_pos_sessions(connection) -> None:
    if not _table_exists(connection, "pos_sessions"):
        connection.execute(
            text(
                """
                CREATE TABLE pos_sessions (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    session_type TEXT NOT NULL,
                    station_id TEXT,
                    device_id TEXT,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at DATETIME,
                    expires_at DATETIME NOT NULL,
                    revoked_at DATETIME,
                    revoked_reason TEXT,
                    FOREIGN KEY(user_id) REFERENCES pos_users(id)
                )
                """
            )
        )
    else:
        _ensure_column(connection, "pos_sessions", "user_id", "INTEGER")
        _ensure_column(connection, "pos_sessions", "token_hash", "TEXT")
        _ensure_column(connection, "pos_sessions", "session_type", "TEXT")
        _ensure_column(connection, "pos_sessions", "station_id", "TEXT")
        _ensure_column(connection, "pos_sessions", "device_id", "TEXT")
        _ensure_column(connection, "pos_sessions", "created_at", "DATETIME")
        _ensure_column(connection, "pos_sessions", "last_seen_at", "DATETIME")
        _ensure_column(connection, "pos_sessions", "expires_at", "DATETIME")
        _ensure_column(connection, "pos_sessions", "revoked_at", "DATETIME")
        _ensure_column(connection, "pos_sessions", "revoked_reason", "TEXT")


def _ensure_table_platform_login_2fa_challenges(connection) -> None:
    if not _table_exists(connection, "platform_login_2fa_challenges"):
        connection.execute(
            text(
                """
                CREATE TABLE platform_login_2fa_challenges (
                    id INTEGER PRIMARY KEY,
                    platform_user_id INTEGER NOT NULL,
                    code_hash TEXT NOT NULL,
                    expires_at DATETIME NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    consumed_at DATETIME,
                    user_agent TEXT,
                    ip_address TEXT,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(platform_user_id) REFERENCES platform_users(id)
                )
                """
            )
        )
    else:
        _ensure_column(connection, "platform_login_2fa_challenges", "platform_user_id", "INTEGER")
        _ensure_column(connection, "platform_login_2fa_challenges", "code_hash", "TEXT")
        _ensure_column(connection, "platform_login_2fa_challenges", "expires_at", "DATETIME")
        _ensure_column(connection, "platform_login_2fa_challenges", "attempts", "INTEGER DEFAULT 0")
        _ensure_column(connection, "platform_login_2fa_challenges", "consumed_at", "DATETIME")
        _ensure_column(connection, "platform_login_2fa_challenges", "user_agent", "TEXT")
        _ensure_column(connection, "platform_login_2fa_challenges", "ip_address", "TEXT")
        _ensure_column(connection, "platform_login_2fa_challenges", "created_at", "DATETIME")


def _ensure_table_platform_trusted_devices(connection) -> None:
    if not _table_exists(connection, "platform_trusted_devices"):
        connection.execute(
            text(
                """
                CREATE TABLE platform_trusted_devices (
                    id INTEGER PRIMARY KEY,
                    platform_user_id INTEGER NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    device_label TEXT,
                    user_agent TEXT,
                    last_ip TEXT,
                    expires_at DATETIME NOT NULL,
                    revoked_at DATETIME,
                    last_used_at DATETIME,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(platform_user_id) REFERENCES platform_users(id)
                )
                """
            )
        )
    else:
        _ensure_column(connection, "platform_trusted_devices", "platform_user_id", "INTEGER")
        _ensure_column(connection, "platform_trusted_devices", "token_hash", "TEXT")
        _ensure_column(connection, "platform_trusted_devices", "device_label", "TEXT")
        _ensure_column(connection, "platform_trusted_devices", "user_agent", "TEXT")
        _ensure_column(connection, "platform_trusted_devices", "last_ip", "TEXT")
        _ensure_column(connection, "platform_trusted_devices", "expires_at", "DATETIME")
        _ensure_column(connection, "platform_trusted_devices", "revoked_at", "DATETIME")
        _ensure_column(connection, "platform_trusted_devices", "last_used_at", "DATETIME")
        _ensure_column(connection, "platform_trusted_devices", "created_at", "DATETIME")


def _ensure_table_user_documents(connection) -> None:
    if not _table_exists(connection, "pos_user_documents"):
        connection.execute(
            text(
                """
                CREATE TABLE pos_user_documents (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    file_name TEXT NOT NULL,
                    file_url TEXT NOT NULL,
                    file_size INTEGER NOT NULL DEFAULT 0,
                    note TEXT,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES pos_users(id)
                )
                """
            )
        )
    else:
        _ensure_column(connection, "pos_user_documents", "user_id", "INTEGER")
        _ensure_column(connection, "pos_user_documents", "file_name", "TEXT")
        _ensure_column(connection, "pos_user_documents", "file_url", "TEXT")
        _ensure_column(connection, "pos_user_documents", "file_size", "INTEGER")
        _ensure_column(connection, "pos_user_documents", "note", "TEXT")
        _ensure_column(connection, "pos_user_documents", "created_at", "DATETIME")


def _ensure_table_hr_employees(connection) -> None:
    if not _table_exists(connection, "hr_employees"):
        connection.execute(
            text(
                """
                CREATE TABLE hr_employees (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    email TEXT,
                    status TEXT NOT NULL DEFAULT 'Activo',
                    phone TEXT,
                    position TEXT,
                    notes TEXT,
                    avatar_url TEXT,
                    birth_date DATE,
                    location TEXT,
                    bio TEXT,
                    payroll_frequency TEXT,
                    payroll_amount FLOAT,
                    payroll_currency TEXT,
                    payroll_payment_method TEXT,
                    payroll_day_of_week TEXT,
                    payroll_day_of_month INTEGER,
                    payroll_last_paid_at DATE,
                    payroll_next_due_at DATE,
                    payroll_reference TEXT,
                    payroll_notes TEXT,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS hr_employees_name_idx ON hr_employees(name)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS hr_employees_email_idx ON hr_employees(email)"
            )
        )
    else:
        _ensure_column(connection, "hr_employees", "name", "TEXT")
        _ensure_column(connection, "hr_employees", "email", "TEXT")
        _ensure_column(connection, "hr_employees", "status", "TEXT DEFAULT 'Activo'")
        _ensure_column(connection, "hr_employees", "phone", "TEXT")
        _ensure_column(connection, "hr_employees", "position", "TEXT")
        _ensure_column(connection, "hr_employees", "notes", "TEXT")
        _ensure_column(connection, "hr_employees", "avatar_url", "TEXT")
        _ensure_column(connection, "hr_employees", "birth_date", "DATE")
        _ensure_column(connection, "hr_employees", "location", "TEXT")
        _ensure_column(connection, "hr_employees", "bio", "TEXT")
        _ensure_column(connection, "hr_employees", "payroll_frequency", "TEXT")
        _ensure_column(connection, "hr_employees", "payroll_amount", "FLOAT")
        _ensure_column(connection, "hr_employees", "payroll_currency", "TEXT")
        _ensure_column(connection, "hr_employees", "payroll_payment_method", "TEXT")
        _ensure_column(connection, "hr_employees", "payroll_day_of_week", "TEXT")
        _ensure_column(connection, "hr_employees", "payroll_day_of_month", "INTEGER")
        _ensure_column(connection, "hr_employees", "payroll_last_paid_at", "DATE")
        _ensure_column(connection, "hr_employees", "payroll_next_due_at", "DATE")
        _ensure_column(connection, "hr_employees", "payroll_reference", "TEXT")
        _ensure_column(connection, "hr_employees", "payroll_notes", "TEXT")
        _ensure_column(connection, "hr_employees", "created_at", "DATETIME")
        _ensure_column(connection, "hr_employees", "updated_at", "DATETIME")


def _ensure_table_hr_employee_documents(connection) -> None:
    if not _table_exists(connection, "hr_employee_documents"):
        connection.execute(
            text(
                """
                CREATE TABLE hr_employee_documents (
                    id INTEGER PRIMARY KEY,
                    employee_id INTEGER NOT NULL,
                    file_name TEXT NOT NULL,
                    file_url TEXT NOT NULL,
                    file_size INTEGER NOT NULL DEFAULT 0,
                    note TEXT,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(employee_id) REFERENCES hr_employees(id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS hr_employee_documents_employee_idx
                ON hr_employee_documents(employee_id)
                """
            )
        )
    else:
        _ensure_column(connection, "hr_employee_documents", "employee_id", "INTEGER")
        _ensure_column(connection, "hr_employee_documents", "file_name", "TEXT")
        _ensure_column(connection, "hr_employee_documents", "file_url", "TEXT")
        _ensure_column(connection, "hr_employee_documents", "file_size", "INTEGER")
        _ensure_column(connection, "hr_employee_documents", "note", "TEXT")
        _ensure_column(connection, "hr_employee_documents", "created_at", "DATETIME")


def _ensure_table_schedule_templates(connection) -> None:
    if not _table_exists(connection, "schedule_templates"):
        connection.execute(
            text(
                """
                CREATE TABLE schedule_templates (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    break_minutes INTEGER NOT NULL DEFAULT 0,
                    color TEXT,
                    position TEXT,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    order_index INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
    else:
        _ensure_column(connection, "schedule_templates", "name", "TEXT")
        _ensure_column(connection, "schedule_templates", "start_time", "TEXT")
        _ensure_column(connection, "schedule_templates", "end_time", "TEXT")
        _ensure_column(
            connection,
            "schedule_templates",
            "break_minutes",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(connection, "schedule_templates", "color", "TEXT")
        _ensure_column(connection, "schedule_templates", "position", "TEXT")
        _ensure_column(
            connection,
            "schedule_templates",
            "is_active",
            "BOOLEAN NOT NULL DEFAULT 1",
        )
        _ensure_column(
            connection,
            "schedule_templates",
            "order_index",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(connection, "schedule_templates", "created_at", "DATETIME")
        _ensure_column(connection, "schedule_templates", "updated_at", "DATETIME")


def _ensure_table_schedule_weeks(connection) -> None:
    if not _table_exists(connection, "schedule_weeks"):
        connection.execute(
            text(
                """
                CREATE TABLE schedule_weeks (
                    id INTEGER PRIMARY KEY,
                    week_start DATE NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'draft',
                    notes TEXT,
                    published_at DATETIME,
                    published_by_user_id INTEGER,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(published_by_user_id) REFERENCES pos_users(id)
                )
                """
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS schedule_weeks_week_start_idx ON schedule_weeks(week_start)"
            )
        )
    else:
        _ensure_column(connection, "schedule_weeks", "week_start", "DATE")
        _ensure_column(connection, "schedule_weeks", "status", "TEXT DEFAULT 'draft'")
        _ensure_column(connection, "schedule_weeks", "notes", "TEXT")
        _ensure_column(connection, "schedule_weeks", "published_at", "DATETIME")
        _ensure_column(connection, "schedule_weeks", "published_by_user_id", "INTEGER")
        _ensure_column(connection, "schedule_weeks", "created_at", "DATETIME")
        _ensure_column(connection, "schedule_weeks", "updated_at", "DATETIME")


def _ensure_table_schedule_shifts(connection) -> None:
    if not _table_exists(connection, "schedule_shifts"):
        connection.execute(
            text(
                """
                CREATE TABLE schedule_shifts (
                    id INTEGER PRIMARY KEY,
                    week_id INTEGER NOT NULL,
                    employee_id INTEGER NOT NULL,
                    shift_date DATE NOT NULL,
                    start_time TEXT,
                    end_time TEXT,
                    break_minutes INTEGER NOT NULL DEFAULT 0,
                    position TEXT,
                    color TEXT,
                    note TEXT,
                    is_time_off BOOLEAN NOT NULL DEFAULT 0,
                    source_template_id INTEGER,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(week_id) REFERENCES schedule_weeks(id),
                    FOREIGN KEY(employee_id) REFERENCES hr_employees(id),
                    FOREIGN KEY(source_template_id) REFERENCES schedule_templates(id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS schedule_shifts_cell_idx
                ON schedule_shifts(week_id, employee_id, shift_date)
                """
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS schedule_shifts_week_idx ON schedule_shifts(week_id)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS schedule_shifts_employee_idx ON schedule_shifts(employee_id)"
            )
        )
    else:
        _ensure_column(connection, "schedule_shifts", "week_id", "INTEGER")
        _ensure_column(connection, "schedule_shifts", "employee_id", "INTEGER")
        _ensure_column(connection, "schedule_shifts", "shift_date", "DATE")
        _ensure_column(connection, "schedule_shifts", "start_time", "TEXT")
        _ensure_column(connection, "schedule_shifts", "end_time", "TEXT")
        _ensure_column(
            connection,
            "schedule_shifts",
            "break_minutes",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(connection, "schedule_shifts", "position", "TEXT")
        _ensure_column(connection, "schedule_shifts", "color", "TEXT")
        _ensure_column(connection, "schedule_shifts", "note", "TEXT")
        _ensure_column(
            connection,
            "schedule_shifts",
            "is_time_off",
            "BOOLEAN NOT NULL DEFAULT 0",
        )
        _ensure_column(connection, "schedule_shifts", "source_template_id", "INTEGER")
        _ensure_column(connection, "schedule_shifts", "created_at", "DATETIME")
        _ensure_column(connection, "schedule_shifts", "updated_at", "DATETIME")
        connection.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS schedule_shifts_cell_idx
                ON schedule_shifts(week_id, employee_id, shift_date)
                """
            )
        )


def _backfill_hr_employees_from_users(connection, backend: str) -> None:
    rows = connection.execute(
        text(
            """
            SELECT id, name, email, status, phone, position, notes, avatar_url, birth_date, location, bio, created_at
            FROM pos_users
            WHERE employee_id IS NULL
            """
        )
    ).mappings().all()
    for row in rows:
        connection.execute(
            text(
                """
                INSERT INTO hr_employees (
                    id, name, email, status, phone, position, notes, avatar_url, birth_date, location, bio, created_at, updated_at
                )
                SELECT :id, :name, :email, :status, :phone, :position, :notes, :avatar_url, :birth_date, :location, :bio, :created_at, CURRENT_TIMESTAMP
                WHERE NOT EXISTS (SELECT 1 FROM hr_employees WHERE id = :id)
                """
            ),
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "email": row.get("email"),
                "status": row.get("status") or "Activo",
                "phone": row.get("phone"),
                "position": row.get("position"),
                "notes": row.get("notes"),
                "avatar_url": row.get("avatar_url"),
                "birth_date": row.get("birth_date"),
                "location": row.get("location"),
                "bio": row.get("bio"),
                "created_at": row.get("created_at"),
            },
        )
        connection.execute(
            text("UPDATE pos_users SET employee_id = :id WHERE id = :id"),
            {"id": row.get("id")},
        )
    if backend == "postgresql":
        connection.execute(
            text(
                """
                SELECT setval(
                    pg_get_serial_sequence('hr_employees', 'id'),
                    COALESCE((SELECT MAX(id) FROM hr_employees), 1),
                    true
                )
                """
            )
        )


def _ensure_table_payment_methods(connection) -> None:
    if not _table_exists(connection, "payment_methods"):
        connection.execute(
            text(
                """
                CREATE TABLE payment_methods (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    description TEXT,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    allow_change BOOLEAN NOT NULL DEFAULT 0,
                    order_index INTEGER NOT NULL DEFAULT 0,
                    color TEXT,
                    icon TEXT,
                    deleted_at DATETIME
                )
                """
            )
        )
    else:
        _ensure_column(connection, "payment_methods", "description", "TEXT")
        _ensure_column(
            connection,
            "payment_methods",
            "is_active",
            "BOOLEAN NOT NULL DEFAULT 1",
        )
        _ensure_column(
            connection,
            "payment_methods",
            "allow_change",
            "BOOLEAN NOT NULL DEFAULT 0",
        )
        _ensure_column(
            connection,
            "payment_methods",
            "order_index",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(connection, "payment_methods", "color", "TEXT")
        _ensure_column(connection, "payment_methods", "icon", "TEXT")
        _ensure_column(connection, "payment_methods", "deleted_at", "DATETIME")


def _ensure_table_document_adjustments(connection) -> None:
    if not _table_exists(connection, "document_adjustments"):
        connection.execute(
            text(
                """
                CREATE TABLE document_adjustments (
                    id INTEGER PRIMARY KEY,
                    tenant_id INTEGER,
                    doc_type TEXT NOT NULL,
                    doc_id INTEGER NOT NULL,
                    adjustment_type TEXT NOT NULL,
                    reason TEXT,
                    payload JSON,
                    total_delta FLOAT NOT NULL DEFAULT 0,
                    payment_delta FLOAT NOT NULL DEFAULT 0,
                    is_post_closure BOOLEAN NOT NULL DEFAULT 0,
                    original_closure_id INTEGER,
                    created_by_user_id INTEGER,
                    created_by_user_name TEXT,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(original_closure_id) REFERENCES pos_closures(id),
                    FOREIGN KEY(created_by_user_id) REFERENCES pos_users(id)
                )
                """
            )
        )
        connection.execute(
            text(
                "CREATE INDEX document_adjustments_doc_idx "
                "ON document_adjustments (doc_type, doc_id)"
            )
        )
    else:
        _ensure_column(connection, "document_adjustments", "tenant_id", "INTEGER")
        _ensure_column(connection, "document_adjustments", "doc_type", "TEXT")
        _ensure_column(connection, "document_adjustments", "doc_id", "INTEGER")
        _ensure_column(connection, "document_adjustments", "adjustment_type", "TEXT")
        _ensure_column(connection, "document_adjustments", "reason", "TEXT")
        _ensure_column(connection, "document_adjustments", "payload", "JSON")
        _ensure_column(
            connection,
            "document_adjustments",
            "total_delta",
            "FLOAT DEFAULT 0",
        )
        _ensure_column(
            connection,
            "document_adjustments",
            "payment_delta",
            "FLOAT DEFAULT 0",
        )
        _ensure_column(
            connection,
            "document_adjustments",
            "is_post_closure",
            "BOOLEAN DEFAULT 0",
        )
        _ensure_column(
            connection,
            "document_adjustments",
            "original_closure_id",
            "INTEGER",
        )
        _ensure_column(
            connection,
            "document_adjustments",
            "created_by_user_id",
            "INTEGER",
        )
        _ensure_column(
            connection,
            "document_adjustments",
            "created_by_user_name",
            "TEXT",
        )
        _ensure_column(
            connection,
            "document_adjustments",
            "created_at",
            "DATETIME",
        )
        _ensure_column(
            connection,
            "web_orders",
            "customer_approval_email_sent_at",
            "DATETIME",
        )
        _ensure_column(
            connection,
            "web_orders",
            "customer_approval_email_last_error",
            "TEXT",
        )
        _ensure_column(
            connection,
            "web_orders",
            "internal_approval_email_sent_at",
            "DATETIME",
        )
        _ensure_column(
            connection,
            "web_orders",
            "internal_approval_email_last_error",
            "TEXT",
        )
        _ensure_products_tenant_scoped_unique_indexes(connection, backend="sqlite")
        _ensure_payment_methods_tenant_scoped_unique_indexes(connection, backend="sqlite")
        _ensure_pos_document_tenant_scoped_unique_indexes(connection, backend="sqlite")
        _ensure_web_order_payments_provider_reference_unique_index(
            connection,
            backend="sqlite",
        )


def _ensure_table_product_audit_logs(connection) -> None:
    if not _table_exists(connection, "product_audit_logs"):
        connection.execute(
            text(
                """
                CREATE TABLE product_audit_logs (
                    id INTEGER PRIMARY KEY,
                    product_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor_user_id INTEGER,
                    actor_name TEXT,
                    actor_email TEXT,
                    changes JSON,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(actor_user_id) REFERENCES pos_users(id)
                )
                """
            )
        )
        connection.execute(
            text(
                "CREATE INDEX product_audit_logs_product_idx "
                "ON product_audit_logs (product_id, created_at)"
            )
        )
    else:
        _ensure_column(connection, "product_audit_logs", "product_id", "INTEGER")
        _ensure_column(connection, "product_audit_logs", "action", "TEXT")
        _ensure_column(connection, "product_audit_logs", "actor_user_id", "INTEGER")
        _ensure_column(connection, "product_audit_logs", "actor_name", "TEXT")
        _ensure_column(connection, "product_audit_logs", "actor_email", "TEXT")
        _ensure_column(connection, "product_audit_logs", "changes", "JSON")
        _ensure_column(connection, "product_audit_logs", "created_at", "DATETIME")


def _ensure_table_stock_devices(connection) -> None:
    if not _table_exists(connection, "stock_devices"):
        connection.execute(
            text(
                """
                CREATE TABLE stock_devices (
                    id TEXT PRIMARY KEY,
                    tenant_id INTEGER,
                    name TEXT NOT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    bound_device_id TEXT,
                    bound_device_label TEXT,
                    created_by_user_id INTEGER,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at DATETIME,
                    FOREIGN KEY(tenant_id) REFERENCES tenants(id),
                    FOREIGN KEY(created_by_user_id) REFERENCES pos_users(id)
                )
                """
            )
        )
    else:
        _ensure_column(connection, "stock_devices", "tenant_id", "INTEGER")
        _ensure_column(connection, "stock_devices", "name", "TEXT")
        _ensure_column(connection, "stock_devices", "is_active", "BOOLEAN NOT NULL DEFAULT 1")
        _ensure_column(connection, "stock_devices", "bound_device_id", "TEXT")
        _ensure_column(connection, "stock_devices", "bound_device_label", "TEXT")
        _ensure_column(connection, "stock_devices", "created_by_user_id", "INTEGER")
        _ensure_column(connection, "stock_devices", "created_at", "DATETIME")
        _ensure_column(connection, "stock_devices", "updated_at", "DATETIME")
        _ensure_column(connection, "stock_devices", "last_seen_at", "DATETIME")

    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS stock_devices_tenant_idx
            ON stock_devices (tenant_id)
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS stock_devices_tenant_active_idx
            ON stock_devices (tenant_id, is_active)
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS stock_devices_tenant_name_unique_idx
            ON stock_devices (tenant_id, name)
            """
        )
    )


def _ensure_table_pos_stations(connection) -> None:
    if not _table_exists(connection, "pos_stations"):
        connection.execute(
            text(
                """
                CREATE TABLE pos_stations (
                    id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    pos_user_id INTEGER,
                    station_email TEXT,
                    station_password_hash TEXT,
                    pin_hash TEXT,
                    parent_station_id TEXT,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    last_login_at DATETIME,
                    last_failed_at DATETIME,
                    bound_device_id TEXT,
                    bound_device_label TEXT,
                    bound_at DATETIME,
                    bound_by_user_id INTEGER,
                    bound_by_user_name TEXT,
                    printer_mode TEXT,
                    printer_name TEXT,
                    printer_width TEXT,
                    printer_auto_open_drawer BOOLEAN,
                    printer_show_drawer_button BOOLEAN,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(pos_user_id) REFERENCES pos_users(id),
                    FOREIGN KEY(parent_station_id) REFERENCES pos_stations(id),
                    FOREIGN KEY(bound_by_user_id) REFERENCES pos_users(id)
                )
                """
            )
        )
    else:
        _ensure_column(connection, "pos_stations", "label", "TEXT")
        _ensure_column(connection, "pos_stations", "pos_user_id", "INTEGER")
        _ensure_column(connection, "pos_stations", "station_email", "TEXT")
        _ensure_column(connection, "pos_stations", "station_password_hash", "TEXT")
        _ensure_column(connection, "pos_stations", "pin_hash", "TEXT")
        _ensure_column(connection, "pos_stations", "parent_station_id", "TEXT")
        _ensure_column(connection, "pos_stations", "is_active", "BOOLEAN NOT NULL DEFAULT 1")
        _ensure_column(connection, "pos_stations", "failed_attempts", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "pos_stations", "last_login_at", "DATETIME")
        _ensure_column(connection, "pos_stations", "last_failed_at", "DATETIME")
        _ensure_column(connection, "pos_stations", "bound_device_id", "TEXT")
        _ensure_column(connection, "pos_stations", "bound_device_label", "TEXT")
        _ensure_column(connection, "pos_stations", "bound_at", "DATETIME")
        _ensure_column(connection, "pos_stations", "bound_by_user_id", "INTEGER")
        _ensure_column(connection, "pos_stations", "bound_by_user_name", "TEXT")
        _ensure_column(connection, "pos_stations", "printer_mode", "TEXT")
        _ensure_column(connection, "pos_stations", "printer_name", "TEXT")
        _ensure_column(connection, "pos_stations", "printer_width", "TEXT")
        _ensure_column(connection, "pos_stations", "printer_auto_open_drawer", "BOOLEAN")
        _ensure_column(connection, "pos_stations", "printer_show_drawer_button", "BOOLEAN")
        _ensure_column(connection, "pos_stations", "created_at", "DATETIME")
        _ensure_column(connection, "pos_stations", "updated_at", "DATETIME")


def _ensure_table_pos_station_notices(connection) -> None:
    if not _table_exists(connection, "pos_station_notices"):
        connection.execute(
            text(
                """
                CREATE TABLE pos_station_notices (
                    id INTEGER PRIMARY KEY,
                    station_id TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    created_by_user_id INTEGER,
                    dismissed_at DATETIME,
                    dismissed_by_user_id INTEGER,
                    FOREIGN KEY(station_id) REFERENCES pos_stations(id),
                    FOREIGN KEY(created_by_user_id) REFERENCES pos_users(id),
                    FOREIGN KEY(dismissed_by_user_id) REFERENCES pos_users(id)
                )
                """
            )
        )


def _relax_pos_station_schema_sqlite(connection) -> None:
    if not _table_exists(connection, "pos_stations"):
        return

    info = list(connection.execute(text("PRAGMA table_info(pos_stations)")).mappings())
    pos_user_info = next((row for row in info if row.get("name") == "pos_user_id"), None)
    pin_info = next((row for row in info if row.get("name") == "pin_hash"), None)
    email_exists = any(row.get("name") == "station_email" for row in info)
    password_exists = any(row.get("name") == "station_password_hash" for row in info)
    needs_relax = bool(pos_user_info and pos_user_info.get("notnull"))
    needs_relax = needs_relax or bool(pin_info and pin_info.get("notnull"))
    needs_relax = needs_relax or not email_exists or not password_exists
    if not needs_relax:
        return

    existing_cols = [row.get("name") for row in info if row.get("name")]
    desired_cols = [
        "id",
        "label",
        "pos_user_id",
        "station_email",
        "station_password_hash",
        "pin_hash",
        "parent_station_id",
        "is_active",
        "failed_attempts",
        "last_login_at",
        "last_failed_at",
        "bound_device_id",
        "bound_device_label",
        "bound_at",
        "bound_by_user_id",
        "bound_by_user_name",
        "printer_mode",
        "printer_name",
        "printer_width",
        "printer_auto_open_drawer",
        "printer_show_drawer_button",
        "created_at",
        "updated_at",
    ]
    copy_cols = [col for col in desired_cols if col in existing_cols]
    column_list = ", ".join(copy_cols)

    connection.execute(text("ALTER TABLE pos_stations RENAME TO pos_stations_old"))
    connection.execute(
        text(
            """
            CREATE TABLE pos_stations (
                id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                pos_user_id INTEGER,
                station_email TEXT,
                station_password_hash TEXT,
                pin_hash TEXT,
                parent_station_id TEXT,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                last_login_at DATETIME,
                last_failed_at DATETIME,
                bound_device_id TEXT,
                bound_device_label TEXT,
                bound_at DATETIME,
                bound_by_user_id INTEGER,
                bound_by_user_name TEXT,
                printer_mode TEXT,
                printer_name TEXT,
                printer_width TEXT,
                printer_auto_open_drawer BOOLEAN,
                printer_show_drawer_button BOOLEAN,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(pos_user_id) REFERENCES pos_users(id),
                FOREIGN KEY(parent_station_id) REFERENCES pos_stations(id),
                FOREIGN KEY(bound_by_user_id) REFERENCES pos_users(id)
            )
            """
        )
    )
    if column_list:
        connection.execute(
            text(
                f"""
                INSERT INTO pos_stations ({column_list})
                SELECT {column_list} FROM pos_stations_old
                """
            )
        )
    connection.execute(text("DROP TABLE pos_stations_old"))


def _ensure_table_sale_changes(connection) -> None:
    if not _table_exists(connection, "sale_changes"):
        connection.execute(
            text(
                """
                CREATE TABLE sale_changes (
                    id INTEGER PRIMARY KEY,
                    sale_id INTEGER NOT NULL,
                    closure_id INTEGER,
                    document_number TEXT,
                    status TEXT NOT NULL DEFAULT 'confirmed',
                    notes TEXT,
                    created_by TEXT,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    pos_name TEXT,
                    seller_name TEXT,
                    station_id TEXT,
                    total_credit FLOAT NOT NULL DEFAULT 0,
                    total_new FLOAT NOT NULL DEFAULT 0,
                    net_total FLOAT NOT NULL DEFAULT 0,
                    extra_payment FLOAT NOT NULL DEFAULT 0,
                    refund_due FLOAT NOT NULL DEFAULT 0,
                    FOREIGN KEY(sale_id) REFERENCES sales(id),
                    FOREIGN KEY(closure_id) REFERENCES pos_closures(id),
                    FOREIGN KEY(station_id) REFERENCES pos_stations(id)
                )
                """
            )
        )
    else:
        _ensure_column(connection, "sale_changes", "sale_id", "INTEGER")
        _ensure_column(connection, "sale_changes", "closure_id", "INTEGER")
        _ensure_column(connection, "sale_changes", "document_number", "TEXT")
        _ensure_column(connection, "sale_changes", "status", "TEXT")
        _ensure_column(connection, "sale_changes", "notes", "TEXT")
        _ensure_column(connection, "sale_changes", "created_by", "TEXT")
        _ensure_column(connection, "sale_changes", "created_at", "DATETIME")
        _ensure_column(connection, "sale_changes", "pos_name", "TEXT")
        _ensure_column(connection, "sale_changes", "seller_name", "TEXT")
        _ensure_column(connection, "sale_changes", "station_id", "TEXT")
        _ensure_column(connection, "sale_changes", "total_credit", "FLOAT DEFAULT 0")
        _ensure_column(connection, "sale_changes", "total_new", "FLOAT DEFAULT 0")
        _ensure_column(connection, "sale_changes", "net_total", "FLOAT DEFAULT 0")
        _ensure_column(connection, "sale_changes", "extra_payment", "FLOAT DEFAULT 0")
        _ensure_column(connection, "sale_changes", "refund_due", "FLOAT DEFAULT 0")

    if not _table_exists(connection, "sale_change_return_items"):
        connection.execute(
            text(
                """
                CREATE TABLE sale_change_return_items (
                    id INTEGER PRIMARY KEY,
                    change_id INTEGER NOT NULL,
                    sale_item_id INTEGER NOT NULL,
                    product_id INTEGER NOT NULL,
                    product_name TEXT NOT NULL,
                    product_sku TEXT,
                    product_barcode TEXT,
                    reason TEXT,
                    quantity FLOAT NOT NULL DEFAULT 0,
                    unit_price_original FLOAT NOT NULL DEFAULT 0,
                    unit_price_net FLOAT NOT NULL DEFAULT 0,
                    line_discount_value FLOAT NOT NULL DEFAULT 0,
                    cart_discount_share FLOAT NOT NULL DEFAULT 0,
                    total_credit FLOAT NOT NULL DEFAULT 0,
                    FOREIGN KEY(change_id) REFERENCES sale_changes(id),
                    FOREIGN KEY(sale_item_id) REFERENCES sale_items(id)
                )
                """
            )
        )

    if not _table_exists(connection, "sale_change_new_items"):
        connection.execute(
            text(
                """
                CREATE TABLE sale_change_new_items (
                    id INTEGER PRIMARY KEY,
                    change_id INTEGER NOT NULL,
                    product_id INTEGER NOT NULL,
                    product_name TEXT NOT NULL,
                    product_sku TEXT,
                    product_barcode TEXT,
                    quantity FLOAT NOT NULL DEFAULT 0,
                    unit_price FLOAT NOT NULL DEFAULT 0,
                    total FLOAT NOT NULL DEFAULT 0,
                    FOREIGN KEY(change_id) REFERENCES sale_changes(id)
                )
                """
            )
        )

    if not _table_exists(connection, "sale_change_payments"):
        connection.execute(
            text(
                """
                CREATE TABLE sale_change_payments (
                    id INTEGER PRIMARY KEY,
                    change_id INTEGER NOT NULL,
                    method TEXT NOT NULL,
                    amount FLOAT NOT NULL DEFAULT 0,
                    FOREIGN KEY(change_id) REFERENCES sale_changes(id)
                )
                """
            )
        )


def _ensure_table_sale_number_reservations(connection) -> None:
    if _table_exists(connection, "sale_number_reservations"):
        return
    connection.execute(
        text(
            """
            CREATE TABLE sale_number_reservations (
                id INTEGER PRIMARY KEY,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'reserved',
                sale_number INTEGER NOT NULL,
                document_number TEXT NOT NULL,
                pos_name TEXT,
                station_id TEXT,
                reserved_by_user_id INTEGER,
                sale_id INTEGER,
                FOREIGN KEY(reserved_by_user_id) REFERENCES pos_users(id),
                FOREIGN KEY(sale_id) REFERENCES sales(id),
                FOREIGN KEY(station_id) REFERENCES pos_stations(id)
            )
            """
        )
    )


def _ensure_table_sale_changes_postgres(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS sale_changes (
                id SERIAL PRIMARY KEY,
                sale_id INTEGER NOT NULL REFERENCES sales(id),
                closure_id INTEGER REFERENCES pos_closures(id),
                document_number TEXT,
                status TEXT NOT NULL DEFAULT 'confirmed',
                notes TEXT,
                created_by TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                pos_name TEXT,
                seller_name TEXT,
                station_id TEXT REFERENCES pos_stations(id),
                total_credit FLOAT NOT NULL DEFAULT 0,
                total_new FLOAT NOT NULL DEFAULT 0,
                net_total FLOAT NOT NULL DEFAULT 0,
                extra_payment FLOAT NOT NULL DEFAULT 0,
                refund_due FLOAT NOT NULL DEFAULT 0
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS sale_change_return_items (
                id SERIAL PRIMARY KEY,
                change_id INTEGER NOT NULL REFERENCES sale_changes(id),
                sale_item_id INTEGER NOT NULL REFERENCES sale_items(id),
                product_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                product_sku TEXT,
                product_barcode TEXT,
                reason TEXT,
                quantity FLOAT NOT NULL DEFAULT 0,
                unit_price_original FLOAT NOT NULL DEFAULT 0,
                unit_price_net FLOAT NOT NULL DEFAULT 0,
                line_discount_value FLOAT NOT NULL DEFAULT 0,
                cart_discount_share FLOAT NOT NULL DEFAULT 0,
                total_credit FLOAT NOT NULL DEFAULT 0
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS sale_change_new_items (
                id SERIAL PRIMARY KEY,
                change_id INTEGER NOT NULL REFERENCES sale_changes(id),
                product_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                product_sku TEXT,
                product_barcode TEXT,
                quantity FLOAT NOT NULL DEFAULT 0,
                unit_price FLOAT NOT NULL DEFAULT 0,
                total FLOAT NOT NULL DEFAULT 0
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS sale_change_payments (
                id SERIAL PRIMARY KEY,
                change_id INTEGER NOT NULL REFERENCES sale_changes(id),
                method TEXT NOT NULL,
                amount FLOAT NOT NULL DEFAULT 0
            )
            """
        )
    )


def _ensure_table_document_adjustments_postgres(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS document_adjustments (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER REFERENCES tenants(id),
                doc_type TEXT NOT NULL,
                doc_id INTEGER NOT NULL,
                adjustment_type TEXT NOT NULL,
                reason TEXT,
                payload JSONB,
                total_delta FLOAT NOT NULL DEFAULT 0,
                payment_delta FLOAT NOT NULL DEFAULT 0,
                is_post_closure BOOLEAN NOT NULL DEFAULT FALSE,
                original_closure_id INTEGER REFERENCES pos_closures(id),
                created_by_user_id INTEGER REFERENCES pos_users(id),
                created_by_user_name TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS document_adjustments_doc_idx
            ON document_adjustments (doc_type, doc_id)
            """
        )
    )
    _ensure_column_postgres(connection, "document_adjustments", "tenant_id", "INTEGER")


def _ensure_table_product_audit_logs_postgres(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS product_audit_logs (
                id SERIAL PRIMARY KEY,
                product_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                actor_user_id INTEGER REFERENCES pos_users(id),
                actor_name TEXT,
                actor_email TEXT,
                changes JSONB,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS product_audit_logs_product_idx
            ON product_audit_logs (product_id, created_at)
            """
        )
    )
    # El historial de productos debe sobrevivir aunque el producto sea eliminado.
    # Si existe una FK antigua hacia products.id, la retiramos para evitar bloqueos
    # al eliminar productos con auditoría.
    connection.execute(
        text(
            """
            ALTER TABLE product_audit_logs
            DROP CONSTRAINT IF EXISTS product_audit_logs_product_id_fkey
            """
        )
    )


def _ensure_table_sale_number_reservations_postgres(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS sale_number_reservations (
                id SERIAL PRIMARY KEY,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'reserved',
                sale_number INTEGER NOT NULL,
                document_number TEXT NOT NULL,
                pos_name TEXT,
                station_id TEXT REFERENCES pos_stations(id),
                reserved_by_user_id INTEGER REFERENCES pos_users(id),
                sale_id INTEGER REFERENCES sales(id)
            )
            """
        )
    )


def _ensure_table_pos_sessions_postgres(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS pos_sessions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES pos_users(id),
                token_hash TEXT NOT NULL UNIQUE,
                session_type TEXT NOT NULL,
                station_id TEXT,
                device_id TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                revoked_at TIMESTAMP,
                revoked_reason TEXT
            )
            """
        )
    )


def _ensure_table_platform_login_2fa_challenges_postgres(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS platform_login_2fa_challenges (
                id SERIAL PRIMARY KEY,
                platform_user_id INTEGER NOT NULL REFERENCES platform_users(id),
                code_hash TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                consumed_at TIMESTAMP,
                user_agent TEXT,
                ip_address TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )


def _ensure_table_platform_trusted_devices_postgres(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS platform_trusted_devices (
                id SERIAL PRIMARY KEY,
                platform_user_id INTEGER NOT NULL REFERENCES platform_users(id),
                token_hash TEXT NOT NULL UNIQUE,
                device_label TEXT,
                user_agent TEXT,
                last_ip TEXT,
                expires_at TIMESTAMP NOT NULL,
                revoked_at TIMESTAMP,
                last_used_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )


def _ensure_table_pos_station_notices_postgres(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS pos_station_notices (
                id SERIAL PRIMARY KEY,
                station_id TEXT NOT NULL REFERENCES pos_stations(id),
                message TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_by_user_id INTEGER REFERENCES pos_users(id),
                dismissed_at TIMESTAMP,
                dismissed_by_user_id INTEGER REFERENCES pos_users(id)
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS pos_station_notices_station_idx
            ON pos_station_notices (station_id)
            """
        )
    )


def _ensure_table_stock_devices_postgres(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS stock_devices (
                id TEXT PRIMARY KEY,
                tenant_id INTEGER REFERENCES tenants(id),
                name TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                bound_device_id TEXT,
                bound_device_label TEXT,
                created_by_user_id INTEGER REFERENCES pos_users(id),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMP
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS stock_devices_tenant_idx
            ON stock_devices (tenant_id)
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS stock_devices_tenant_active_idx
            ON stock_devices (tenant_id, is_active)
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS stock_devices_tenant_name_unique_idx
            ON stock_devices (tenant_id, name)
            """
        )
    )


def _ensure_table_user_documents_postgres(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS pos_user_documents (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES pos_users(id),
                file_name TEXT NOT NULL,
                file_url TEXT NOT NULL,
                file_size INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )


def _ensure_table_hr_employees_postgres(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS hr_employees (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT,
                status TEXT NOT NULL DEFAULT 'Activo',
                phone TEXT,
                position TEXT,
                notes TEXT,
                avatar_url TEXT,
                birth_date DATE,
                location TEXT,
                bio TEXT,
                payroll_frequency TEXT,
                payroll_amount FLOAT,
                payroll_currency TEXT,
                payroll_payment_method TEXT,
                payroll_day_of_week TEXT,
                payroll_day_of_month INTEGER,
                payroll_last_paid_at DATE,
                payroll_next_due_at DATE,
                payroll_reference TEXT,
                payroll_notes TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS hr_employees_name_idx ON hr_employees(name)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS hr_employees_email_idx ON hr_employees(email)"
        )
    )


def _ensure_table_hr_employee_documents_postgres(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS hr_employee_documents (
                id SERIAL PRIMARY KEY,
                employee_id INTEGER NOT NULL REFERENCES hr_employees(id),
                file_name TEXT NOT NULL,
                file_url TEXT NOT NULL,
                file_size INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS hr_employee_documents_employee_idx
            ON hr_employee_documents (employee_id)
            """
        )
    )


def _ensure_table_schedule_templates_postgres(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS schedule_templates (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                break_minutes INTEGER NOT NULL DEFAULT 0,
                color TEXT,
                position TEXT,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                order_index INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )


def _ensure_table_schedule_weeks_postgres(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS schedule_weeks (
                id SERIAL PRIMARY KEY,
                week_start DATE NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'draft',
                notes TEXT,
                published_at TIMESTAMP,
                published_by_user_id INTEGER REFERENCES pos_users(id),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS schedule_weeks_week_start_idx ON schedule_weeks(week_start)"
        )
    )


def _ensure_table_schedule_shifts_postgres(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS schedule_shifts (
                id SERIAL PRIMARY KEY,
                week_id INTEGER NOT NULL REFERENCES schedule_weeks(id) ON DELETE CASCADE,
                employee_id INTEGER NOT NULL REFERENCES hr_employees(id) ON DELETE CASCADE,
                shift_date DATE NOT NULL,
                start_time TEXT,
                end_time TEXT,
                break_minutes INTEGER NOT NULL DEFAULT 0,
                position TEXT,
                color TEXT,
                note TEXT,
                is_time_off BOOLEAN NOT NULL DEFAULT FALSE,
                source_template_id INTEGER REFERENCES schedule_templates(id),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS schedule_shifts_cell_idx
            ON schedule_shifts(week_id, employee_id, shift_date)
            """
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS schedule_shifts_week_idx ON schedule_shifts(week_id)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS schedule_shifts_employee_idx ON schedule_shifts(employee_id)"
        )
    )


def _ensure_table_tenants(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                id INTEGER PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                lifecycle_stage TEXT NOT NULL DEFAULT 'active',
                trial_started_at TIMESTAMP,
                trial_ends_at TIMESTAMP,
                converted_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )


def _ensure_table_tenants_postgres(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                id SERIAL PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                lifecycle_stage TEXT NOT NULL DEFAULT 'active',
                trial_started_at TIMESTAMP,
                trial_ends_at TIMESTAMP,
                converted_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )


def _seed_default_tenant_sqlite(connection) -> None:
    connection.execute(
        text(
            """
            INSERT INTO tenants (
                slug, name, is_active, lifecycle_stage, created_at, updated_at
            )
            VALUES (
                'kensar', 'Kensar Electronic', 1, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            ON CONFLICT(slug) DO NOTHING
            """
        )
    )


def _seed_default_tenant_postgres(connection) -> None:
    connection.execute(
        text(
            """
            INSERT INTO tenants (
                slug, name, is_active, lifecycle_stage, created_at, updated_at
            )
            VALUES (
                'kensar', 'Kensar Electronic', TRUE, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            ON CONFLICT (slug) DO NOTHING
            """
        )
    )


def _backfill_legacy_users_to_default_tenant_sqlite(connection) -> None:
    connection.execute(
        text(
            """
            UPDATE pos_users
            SET tenant_id = (SELECT id FROM tenants WHERE slug = 'kensar' LIMIT 1)
            WHERE tenant_id IS NULL
              AND EXISTS (SELECT 1 FROM tenants WHERE slug = 'kensar')
            """
        )
    )


def _backfill_legacy_users_to_default_tenant_postgres(connection) -> None:
    connection.execute(
        text(
            """
            UPDATE pos_users
            SET tenant_id = t.id
            FROM tenants t
            WHERE pos_users.tenant_id IS NULL
              AND t.slug = 'kensar'
            """
        )
    )


def _ensure_table_demo_signup_audits(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS demo_signup_audits (
                id INTEGER PRIMARY KEY,
                tenant_id INTEGER,
                email TEXT NOT NULL,
                ip_address TEXT,
                user_agent TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )


def _ensure_table_demo_signup_audits_postgres(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS demo_signup_audits (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER,
                email TEXT NOT NULL,
                ip_address TEXT,
                user_agent TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )


def _backfill_company_name_from_tenant_sqlite(connection) -> None:
    if not _table_exists(connection, "pos_settings"):
        return

    # Legacy safety net:
    # - keep personalized branding untouched
    # - only replace placeholders/empty values with the tenant name
    if _column_exists(connection, "pos_settings", "tenant_id"):
        connection.execute(
            text(
                """
                UPDATE pos_settings
                SET company_name = (
                    SELECT trim(t.name)
                    FROM tenants t
                    WHERE t.id = pos_settings.tenant_id
                      AND t.name IS NOT NULL
                      AND trim(t.name) <> ''
                    LIMIT 1
                )
                WHERE pos_settings.tenant_id IS NOT NULL
                  AND (
                    pos_settings.company_name IS NULL
                    OR trim(pos_settings.company_name) = ''
                    OR lower(trim(pos_settings.company_name)) IN ('mi negocio', 'mi empresa')
                  )
                  AND EXISTS (
                    SELECT 1
                    FROM tenants t
                    WHERE t.id = pos_settings.tenant_id
                      AND t.name IS NOT NULL
                      AND trim(t.name) <> ''
                  )
                """
            )
        )
        return

    connection.execute(
        text(
            """
            UPDATE pos_settings
            SET company_name = (
                SELECT trim(t.name)
                FROM tenants t
                WHERE t.slug = 'kensar'
                  AND t.name IS NOT NULL
                  AND trim(t.name) <> ''
                LIMIT 1
            )
            WHERE (
                company_name IS NULL
                OR trim(company_name) = ''
                OR lower(trim(company_name)) IN ('mi negocio', 'mi empresa')
            )
              AND EXISTS (
                SELECT 1
                FROM tenants t
                WHERE t.slug = 'kensar'
                  AND t.name IS NOT NULL
                  AND trim(t.name) <> ''
              )
            """
        )
    )


def _backfill_company_name_from_tenant_postgres(connection) -> None:
    if not _table_exists_postgres(connection, "pos_settings"):
        return

    # Legacy safety net:
    # - keep personalized branding untouched
    # - only replace placeholders/empty values with the tenant name
    if _column_exists_postgres(connection, "pos_settings", "tenant_id"):
        connection.execute(
            text(
                """
                UPDATE pos_settings ps
                SET company_name = btrim(t.name)
                FROM tenants t
                WHERE ps.tenant_id = t.id
                  AND t.name IS NOT NULL
                  AND btrim(t.name) <> ''
                  AND (
                    ps.company_name IS NULL
                    OR btrim(ps.company_name) = ''
                    OR lower(btrim(ps.company_name)) IN ('mi negocio', 'mi empresa')
                  )
                """
            )
        )
        return

    connection.execute(
        text(
            """
            UPDATE pos_settings ps
            SET company_name = btrim(t.name)
            FROM tenants t
            WHERE t.slug = 'kensar'
              AND t.name IS NOT NULL
              AND btrim(t.name) <> ''
              AND (
                ps.company_name IS NULL
                OR btrim(ps.company_name) = ''
                OR lower(btrim(ps.company_name)) IN ('mi negocio', 'mi empresa')
              )
            """
        )
    )


def _ensure_tenant_fk_postgres(connection, table: str) -> None:
    connection.execute(
        text(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM information_schema.table_constraints
                    WHERE constraint_type = 'FOREIGN KEY'
                      AND constraint_name = '{table}_tenant_id_fkey'
                ) THEN
                    ALTER TABLE {table}
                    ADD CONSTRAINT {table}_tenant_id_fkey
                    FOREIGN KEY (tenant_id) REFERENCES tenants(id);
                END IF;
            END $$;
            """
        )
    )


def _ensure_tenant_index_postgres(connection, table: str) -> None:
    connection.execute(
        text(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_tenant_id ON {table}(tenant_id)"
        )
    )


def _ensure_pos_settings_id_sequence_postgres(connection) -> None:
    connection.execute(text("CREATE SEQUENCE IF NOT EXISTS pos_settings_id_seq"))
    connection.execute(
        text(
            """
            ALTER TABLE pos_settings
            ALTER COLUMN id SET DEFAULT nextval('pos_settings_id_seq')
            """
        )
    )
    connection.execute(
        text(
            """
            ALTER SEQUENCE pos_settings_id_seq
            OWNED BY pos_settings.id
            """
        )
    )
    connection.execute(
        text(
            """
            SELECT setval(
                'pos_settings_id_seq',
                COALESCE((SELECT MAX(id) FROM pos_settings), 0) + 1,
                false
            )
            """
        )
    )


def _seed_default_payment_methods(connection) -> None:
    defaults = [
        ("Efectivo", "cash", True, 10),
        ("Tarjeta", "card", False, 20),
        ("QR", "qr", False, 30),
        ("Nequi", "nequi", False, 40),
        ("Daviplata", "daviplata", False, 50),
        ("Crédito", "credit", False, 60),
        ("Separado", "separado", False, 70),
    ]

    for name, slug, allow_change, order_index in defaults:
        exists = connection.execute(
            text(
                "SELECT 1 FROM payment_methods WHERE slug = :slug"
            ),
            {"slug": slug},
        ).first()
        if exists:
            continue
        connection.execute(
            text(
                """
                INSERT INTO payment_methods (
                    name, slug, description, is_active,
                    allow_change, order_index, color, icon, deleted_at
                ) VALUES (:name, :slug, '', 1, :allow_change, :order_index, NULL, NULL, NULL)
                """
            ),
            {
                "name": name,
                "slug": slug,
                "allow_change": 1 if allow_change else 0,
                "order_index": order_index,
            },
        )
