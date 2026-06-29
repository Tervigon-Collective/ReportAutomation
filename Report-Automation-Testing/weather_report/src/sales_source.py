"""Phase 2 data source: raw Shopify city-level order aggregates.

Reads ``public.shopify_orders`` (Postgres) and returns one row per
``(sale_date, raw_city, raw_state)`` with order, revenue, unit, customer,
cancelled and returned counts. Revenue is converted to net-of-GST to match
the rest of the reporting stack.

Connection settings mirror ``database_manager.py``: ``DATABASE_URL`` if set,
otherwise ``DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD``. The module's parent
``.env`` (the main project's) is loaded automatically.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

logger = logging.getLogger(__name__)

MODULE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = MODULE_ROOT.parent  # Report-Automation-Testing (holds .env)

# Default GST rate (India) -> net revenue divisor, overridable via env.
_DEFAULT_GST_RATE = float(os.getenv("GST_RATE", "0.18"))

RAW_COLUMNS = [
    "sale_date",
    "raw_city",
    "raw_state",
    "orders",
    "revenue",
    "units",
    "customers",
    "cancelled_orders",
    "returned_orders",
]

# Aggregation at the raw-city grain. processed_at_ist is stored as text; the
# Postgres ``date(expr)`` cast handles the 'YYYY-MM-DD ...' format (same as the
# existing api_data_fetcher queries).
_SQL = """
WITH base AS (
    SELECT
        o.order_id,
        DATE(o.processed_at_ist) AS sale_date,
        COALESCE(NULLIF(TRIM(o.ship_city), ''), 'Unknown') AS raw_city,
        COALESCE(NULLIF(TRIM(o.ship_province), ''), 'Unknown') AS raw_state,
        o.total_price_amount,
        (o.cancelled_at_ist IS NOT NULL) AS is_cancelled,
        o.display_fulfillment_status
    FROM public.shopify_orders o
    WHERE DATE(o.processed_at_ist) BETWEEN :start_date AND :end_date
      AND (
            o.ship_country IS NULL
         OR o.ship_country = ''
         OR o.ship_country ILIKE 'in'
         OR o.ship_country ILIKE 'ind%'
         OR o.ship_country ILIKE 'bharat'
      )
),
units AS (
    SELECT order_id, SUM(quantity) AS units
    FROM public.shopify_order_line_items
    GROUP BY order_id
),
cust AS (
    SELECT order_id, MAX(email) AS email
    FROM public.customer_details_th
    GROUP BY order_id
)
SELECT
    b.sale_date,
    b.raw_city,
    b.raw_state,
    COUNT(*) FILTER (WHERE NOT b.is_cancelled) AS orders,
    COALESCE(SUM(b.total_price_amount) FILTER (WHERE NOT b.is_cancelled), 0) AS revenue_gross,
    COALESCE(SUM(u.units)             FILTER (WHERE NOT b.is_cancelled), 0) AS units,
    COUNT(DISTINCT c.email)           FILTER (WHERE NOT b.is_cancelled) AS customers,
    COUNT(*) FILTER (WHERE b.is_cancelled) AS cancelled_orders,
    COUNT(*) FILTER (
        WHERE NOT b.is_cancelled
          AND b.display_fulfillment_status ILIKE '%return%'
    ) AS returned_orders
FROM base b
LEFT JOIN units u ON u.order_id = b.order_id
LEFT JOIN cust  c ON c.order_id = b.order_id
GROUP BY b.sale_date, b.raw_city, b.raw_state
ORDER BY b.sale_date, b.raw_city
"""


def _gst_divisor() -> float:
    rate = float(os.getenv("GST_RATE", str(_DEFAULT_GST_RATE)))
    return 1.0 + rate if rate > 0 else 1.0


def get_engine():
    """Create a SQLAlchemy engine, mirroring database_manager.py settings."""
    from dotenv import load_dotenv
    from sqlalchemy import create_engine

    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv()

    db_url = os.getenv("DATABASE_URL")
    if db_url:
        p = urlparse(db_url)
        cfg = {
            "host": p.hostname or "selericdb.postgres.database.azure.com",
            "port": p.port or 5432,
            "database": (p.path or "/postgres").lstrip("/") or "postgres",
            "user": p.username or "admin_seleric",
            "password": p.password or "",
        }
    else:
        cfg = {
            "host": os.environ.get("DB_HOST", "selericdb.postgres.database.azure.com"),
            "port": int(os.environ.get("DB_PORT", 5432)),
            "database": os.environ.get("DB_NAME", "postgres"),
            "user": os.environ.get("DB_USER", "admin_seleric"),
            "password": os.environ.get("DB_PASSWORD", ""),
        }

    conn_str = (
        f"postgresql://{cfg['user']}:{cfg['password']}@"
        f"{cfg['host']}:{cfg['port']}/{cfg['database']}?sslmode=require"
    )
    logger.info("Connecting to host=%s db=%s user=%s",
                cfg["host"], cfg["database"], cfg["user"])
    return create_engine(conn_str, isolation_level="AUTOCOMMIT")


def fetch_raw_city_orders(start_date: str, end_date: str) -> pd.DataFrame:
    """Return raw per-(date, city, state) aggregates for the inclusive window.

    Revenue is net-of-GST. On any failure an empty, correctly-typed frame is
    returned so callers can degrade gracefully.
    """
    from sqlalchemy import text

    try:
        engine = get_engine()
        with engine.connect() as conn:
            df = pd.read_sql(
                text(_SQL), conn,
                params={"start_date": start_date, "end_date": end_date},
            )
    except Exception as exc:  # pragma: no cover - network/credential dependent
        logger.warning("fetch_raw_city_orders failed: %s", exc)
        return pd.DataFrame(columns=RAW_COLUMNS)

    if df.empty:
        return pd.DataFrame(columns=RAW_COLUMNS)

    df["revenue"] = pd.to_numeric(df.pop("revenue_gross"), errors="coerce").fillna(0) / _gst_divisor()
    df["sale_date"] = pd.to_datetime(df["sale_date"]).dt.strftime("%Y-%m-%d")
    for col in ("orders", "units", "customers", "cancelled_orders", "returned_orders"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    df["revenue"] = df["revenue"].round(2)
    return df[RAW_COLUMNS]
