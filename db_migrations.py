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


def run_schema_upgrades(engine: Engine) -> None:
    """Adds missing columns if they don't exist yet."""

    backend = engine.url.get_backend_name()
    if backend not in {"sqlite", "postgresql"}:
        return

    with engine.connect() as connection:
        with connection.begin():
            if backend == "postgresql":
                _ensure_table_sale_changes_postgres(connection)
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
                return
            if backend == "sqlite":
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
                    "tile_color",
                    "VARCHAR(7)",
                )
                _ensure_table_password_resets(connection)
                _ensure_table_payment_methods(connection)
                _seed_default_payment_methods(connection)
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
                _ensure_table_sale_changes(connection)
                _ensure_column(connection, "pos_users", "phone", "TEXT")
                _ensure_column(connection, "pos_users", "position", "TEXT")
                _ensure_column(connection, "pos_users", "notes", "TEXT")
                _ensure_column(connection, "pos_users", "invited_at", "DATETIME")
                _ensure_column(connection, "pos_users", "accepted_at", "DATETIME")
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
                            consecutive TEXT UNIQUE,
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
                            FOREIGN KEY(closed_by_user_id) REFERENCES pos_users(id)
                        )
                        """
                    )
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
                        INSERT INTO pos_users (name, email, role, status, is_active, password_hash, created_at)
                        SELECT :name, :email, 'Administrador', 'Activo', 1, :hash, CURRENT_TIMESTAMP
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


def _ensure_table_payment_methods(connection) -> None:
    if not _table_exists(connection, "payment_methods"):
        connection.execute(
            text(
                """
                CREATE TABLE payment_methods (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    slug TEXT NOT NULL UNIQUE,
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


def _ensure_table_pos_stations(connection) -> None:
    if not _table_exists(connection, "pos_stations"):
        connection.execute(
            text(
                """
                CREATE TABLE pos_stations (
                    id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    pos_user_id INTEGER NOT NULL,
                    pin_hash TEXT NOT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    last_login_at DATETIME,
                    last_failed_at DATETIME,
                    printer_mode TEXT,
                    printer_name TEXT,
                    printer_width TEXT,
                    printer_auto_open_drawer BOOLEAN,
                    printer_show_drawer_button BOOLEAN,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(pos_user_id) REFERENCES pos_users(id)
                )
                """
            )
        )
    else:
        _ensure_column(connection, "pos_stations", "label", "TEXT")
        _ensure_column(connection, "pos_stations", "pos_user_id", "INTEGER")
        _ensure_column(connection, "pos_stations", "pin_hash", "TEXT")
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


def _ensure_table_sale_changes(connection) -> None:
    if not _table_exists(connection, "sale_changes"):
        connection.execute(
            text(
                """
                CREATE TABLE sale_changes (
                    id INTEGER PRIMARY KEY,
                    sale_id INTEGER NOT NULL,
                    closure_id INTEGER,
                    document_number TEXT UNIQUE,
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


def _ensure_table_sale_changes_postgres(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS sale_changes (
                id SERIAL PRIMARY KEY,
                sale_id INTEGER NOT NULL REFERENCES sales(id),
                closure_id INTEGER REFERENCES pos_closures(id),
                document_number TEXT UNIQUE,
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
