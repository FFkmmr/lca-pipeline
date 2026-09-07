"""SQLite persistence for the LCA pipeline."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .config import REQUIRED_STEPS, get_logger

logger = get_logger()

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS products (
        product_ref   TEXT PRIMARY KEY,
        product_id    TEXT,
        color_code    TEXT,
        supplier      TEXT,
        loaded_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS product_impacts (
        product_ref              TEXT NOT NULL,
        process_step             TEXT NOT NULL,
        step_kind                TEXT NOT NULL,
        climate_change_kg_co2_eq REAL,
        water_use_m3_eq          REAL,
        is_present               INTEGER NOT NULL,
        loaded_at                TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (product_ref, process_step),
        FOREIGN KEY (product_ref) REFERENCES products(product_ref) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_impacts_step ON product_impacts(process_step)",
)

# Wide, one-row-per-product view. Convenient for eyeballing the deliverable
# against the specification, which lists the eight steps side by side.
VIEW_STATEMENT = """
CREATE VIEW IF NOT EXISTS v_product_impacts_wide AS
SELECT
    p.product_ref,
    p.supplier,
    MAX(CASE WHEN i.process_step = 'END_OF_LIFE'       THEN i.climate_change_kg_co2_eq END) AS end_of_life_co2,
    MAX(CASE WHEN i.process_step = 'END_OF_LIFE'       THEN i.water_use_m3_eq          END) AS end_of_life_water,
    MAX(CASE WHEN i.process_step = 'USE_PHASE'         THEN i.climate_change_kg_co2_eq END) AS use_phase_co2,
    MAX(CASE WHEN i.process_step = 'USE_PHASE'         THEN i.water_use_m3_eq          END) AS use_phase_water,
    MAX(CASE WHEN i.process_step = 'DISTRIBUTION'      THEN i.climate_change_kg_co2_eq END) AS distribution_co2,
    MAX(CASE WHEN i.process_step = 'DISTRIBUTION'      THEN i.water_use_m3_eq          END) AS distribution_water,
    MAX(CASE WHEN i.process_step = 'WAREHOUSE'         THEN i.climate_change_kg_co2_eq END) AS warehouse_co2,
    MAX(CASE WHEN i.process_step = 'WAREHOUSE'         THEN i.water_use_m3_eq          END) AS warehouse_water,
    MAX(CASE WHEN i.process_step = 'PRODUCT_TRANSPORT' THEN i.climate_change_kg_co2_eq END) AS product_transport_co2,
    MAX(CASE WHEN i.process_step = 'PRODUCT_TRANSPORT' THEN i.water_use_m3_eq          END) AS product_transport_water,
    MAX(CASE WHEN i.process_step = 'MANUFACTURING'     THEN i.climate_change_kg_co2_eq END) AS manufacturing_co2,
    MAX(CASE WHEN i.process_step = 'MANUFACTURING'     THEN i.water_use_m3_eq          END) AS manufacturing_water,
    MAX(CASE WHEN i.process_step = 'MAIN_FABRIC'       THEN i.climate_change_kg_co2_eq END) AS main_fabric_co2,
    MAX(CASE WHEN i.process_step = 'MAIN_FABRIC'       THEN i.water_use_m3_eq          END) AS main_fabric_water,
    MAX(CASE WHEN i.process_step = 'LININGS'           THEN i.climate_change_kg_co2_eq END) AS linings_co2,
    MAX(CASE WHEN i.process_step = 'LININGS'           THEN i.water_use_m3_eq          END) AS linings_water
FROM products p
LEFT JOIN product_impacts i ON i.product_ref = p.product_ref
GROUP BY p.product_ref, p.supplier
"""


class DatabaseManager:
    """Owns the SQLite connection lifecycle and all write paths."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        logger.debug("DatabaseManager bound to %s", self.db_path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection, committing on success and rolling back on error."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("Transaction rolled back")
            raise
        finally:
            conn.close()

    def create_schema(self, reset: bool = False) -> None:
        """Create tables, indexes and the reporting view.

        With `reset=True` existing rows are cleared first, so that a re-run
        against a corrected CSV cannot leave stale records behind.
        """
        with self.connect() as conn:
            if reset:
                conn.execute("DROP VIEW IF EXISTS v_product_impacts_wide")
                conn.execute("DROP TABLE IF EXISTS product_impacts")
                conn.execute("DROP TABLE IF EXISTS products")
                logger.info("Existing schema dropped (reset requested)")
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            conn.execute(VIEW_STATEMENT)
        logger.info("Schema ready: products, product_impacts, v_product_impacts_wide")

    def insert_products(self, rows: Sequence[tuple]) -> int:
        """Insert product master rows. Returns the number of rows written."""
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO products (product_ref, product_id, color_code, supplier)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(product_ref) DO UPDATE SET
                    product_id = excluded.product_id,
                    color_code = excluded.color_code,
                    supplier   = excluded.supplier
                """,
                rows,
            )
        logger.info("Wrote %d product rows", len(rows))
        return len(rows)

    def insert_impacts(self, rows: Sequence[tuple]) -> int:
        """Insert impact rows. Returns the number of rows written."""
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO product_impacts (
                    product_ref, process_step, step_kind,
                    climate_change_kg_co2_eq, water_use_m3_eq, is_present
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_ref, process_step) DO UPDATE SET
                    step_kind                = excluded.step_kind,
                    climate_change_kg_co2_eq = excluded.climate_change_kg_co2_eq,
                    water_use_m3_eq          = excluded.water_use_m3_eq,
                    is_present               = excluded.is_present
                """,
                rows,
            )
        logger.info("Wrote %d impact rows", len(rows))
        return len(rows)

    # -- read helpers -------------------------------------------------------

    def count(self, table: str) -> int:
        if table not in {"products", "product_impacts"}:
            raise ValueError(f"Unknown table: {table}")
        with self.connect() as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def count_missing(self) -> int:
        """Number of impact rows that are structurally absent in the source."""
        with self.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM product_impacts WHERE is_present = 0"
            ).fetchone()[0]

    def products_with_incomplete_steps(self) -> list[str]:
        """Product refs that do not have all eight step rows materialised."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT p.product_ref, COUNT(i.process_step) AS n
                FROM products p
                LEFT JOIN product_impacts i ON i.product_ref = p.product_ref
                GROUP BY p.product_ref
                HAVING n <> ?
                """,
                (len(REQUIRED_STEPS),),
            ).fetchall()
        return [row["product_ref"] for row in rows]

    def product_summary(self, product_ref: str) -> dict:
        with self.connect() as conn:
            product = conn.execute(
                "SELECT * FROM products WHERE product_ref = ?", (product_ref,)
            ).fetchone()
            impacts = conn.execute(
                """
                SELECT process_step, step_kind, climate_change_kg_co2_eq,
                       water_use_m3_eq, is_present
                FROM product_impacts WHERE product_ref = ?
                """,
                (product_ref,),
            ).fetchall()
        return {
            "product": dict(product) if product else None,
            "impacts": {row["process_step"]: dict(row) for row in impacts},
        }
