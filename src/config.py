"""
Configuration for the LCA pipeline.

Importing this module has no side effects: logging is configured explicitly by
the entry point via `setup_logging()`, so importing `config` from tests or from
a notebook does not create log files.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"
LOG_DIR = OUTPUT_DIR / "logs"
DB_PATH = OUTPUT_DIR / "lca_data.db"

DEFAULT_CSV_NAME = "fairlymade-nc-lca-report_2026_06_02_AW26_TRANS_choice.csv"

LOGGER_NAME = "lca_pipeline"

# --------------------------------------------------------------------------
# Domain model
# --------------------------------------------------------------------------
#
# The task specification lists eight "logical process steps" per Product Ref.
# Six originate from product lifecycle rows, two from component impact rows.
# They are modelled uniformly as steps, because that is how the specification
# frames them, but `step_kind` preserves where each value came from.

STEP_KIND_LIFECYCLE = "LIFECYCLE"
STEP_KIND_COMPONENT = "COMPONENT"

LIFECYCLE_STEPS: tuple[str, ...] = (
    "END_OF_LIFE",
    "USE_PHASE",
    "DISTRIBUTION",
    "WAREHOUSE",
    "PRODUCT_TRANSPORT",
    "MANUFACTURING",
)

COMPONENT_STEPS: tuple[str, ...] = (
    "MAIN_FABRIC",
    "LININGS",
)

# The canonical eight steps, in specification order.
REQUIRED_STEPS: tuple[str, ...] = LIFECYCLE_STEPS + COMPONENT_STEPS

STEP_KINDS: dict[str, str] = {
    **{step: STEP_KIND_LIFECYCLE for step in LIFECYCLE_STEPS},
    **{step: STEP_KIND_COMPONENT for step in COMPONENT_STEPS},
}

# --------------------------------------------------------------------------
# CSV contract
# --------------------------------------------------------------------------

COL_PRODUCT_ID = "Product id"
COL_PRODUCT_REF = "Product Ref"
COL_COLOR_CODE = "Color Code"
COL_SUPPLIER = "Product Supplier"
COL_COMPONENT_CATEGORY = "Component category"
COL_PROCESS_STEP = "Process Step"
COL_SOURCE = "source"

COL_CLIMATE = "Climate change - kg CO2 eq"
COL_WATER = "Water use - m3 eq"

REQUIRED_COLUMNS: tuple[str, ...] = (
    COL_PRODUCT_ID,
    COL_PRODUCT_REF,
    COL_COLOR_CODE,
    COL_SUPPLIER,
    COL_COMPONENT_CATEGORY,
    COL_PROCESS_STEP,
    COL_SOURCE,
    COL_CLIMATE,
    COL_WATER,
)

# `source` discriminates row granularity. Only these two are in scope; the
# remaining values (COMPONENT_LIFECYCLE_STEP, MATERIAL_IMPACT,
# MATERIAL_LIFECYCLE_STEP, PRODUCT_IMPACT) describe finer or aggregate levels
# that the specification asks us to ignore.
SOURCE_PRODUCT_LIFECYCLE = "PRODUCT_LIFECYCLE_STEP"
SOURCE_COMPONENT_IMPACT = "COMPONENT_IMPACT"

# Component categories in the CSV carry construction detail, e.g.
# "MAIN FABRIC (WOVEN)". Matching is done on whole words so that variants such as
# "MAIN FABRIC (KNIT)" map to the same logical step, while a different category
# that merely contains the word ("INTERLINING") does not.
COMPONENT_CATEGORY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bMAIN FABRIC\b", "MAIN_FABRIC"),
    (r"\bLININGS?\b", "LININGS"),
)


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def setup_logging(log_file_name: str | None = None, verbose: bool = False) -> logging.Logger:
    """Configure and return the application logger.

    Writes DEBUG and above to a timestamped file, INFO and above to stderr.
    Safe to call more than once; handlers are reset each time.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if log_file_name is None:
        log_file_name = f"lca_pipeline_{datetime.now():%Y%m%d_%H%M%S}.log"
    log_path = LOG_DIR / log_file_name

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s.%(funcName)s:%(lineno)d | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.info("Logging initialised, writing to %s", log_path)
    return logger


def get_logger() -> logging.Logger:
    """Return the application logger without reconfiguring it."""
    return logging.getLogger(LOGGER_NAME)
