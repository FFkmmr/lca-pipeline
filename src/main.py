"""Pipeline orchestration and command-line entry point."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .config import (
    DATA_DIR,
    DB_PATH,
    DEFAULT_CSV_NAME,
    REQUIRED_STEPS,
    get_logger,
    setup_logging,
)
from .database import DatabaseManager
from .transformer import LCADataTransformer
from .validator import CompletenessReporter, DataValidator, ValidationError


class LCAPipeline:
    """Runs validate -> transform -> load against one CSV file."""

    def __init__(self, csv_path: Path, db_path: Path = DB_PATH, reset: bool = True) -> None:
        self.csv_path = Path(csv_path)
        self.db_path = Path(db_path)
        self.reset = reset
        self.db = DatabaseManager(self.db_path)
        self.transformer = LCADataTransformer(self.csv_path)
        self.validator = DataValidator()
        self.logger = get_logger()

    def run(self) -> dict:
        log = self.logger
        started = time.perf_counter()
        log.info("=" * 70)
        log.info("LCA pipeline starting")
        log.info("  input : %s", self.csv_path)
        log.info("  output: %s", self.db_path)
        log.info("=" * 70)

        df = self.transformer.load()
        log.info("Row breakdown by source: %s", self.transformer.source_breakdown())

        log.info("-- validation --")
        report = self.validator.validate(df)

        log.info("-- transformation --")
        product_rows = self.transformer.product_rows()
        impacts_frame = self.transformer.impacts()
        coverage = CompletenessReporter(impacts_frame).summarise()
        impact_rows = self.transformer.impact_rows()

        log.info("-- load --")
        self.db.create_schema(reset=self.reset)
        self.db.insert_products(product_rows)
        self.db.insert_impacts(impact_rows)

        incomplete = self.db.products_with_incomplete_steps()
        if incomplete:
            log.error(
                "%d products do not have all %d step rows: %s",
                len(incomplete),
                len(REQUIRED_STEPS),
                incomplete[:10],
            )
        else:
            log.info(
                "Post-load check: every product has all %d step rows", len(REQUIRED_STEPS)
            )

        elapsed = time.perf_counter() - started
        summary = {
            "products": self.db.count("products"),
            "impact_rows": self.db.count("product_impacts"),
            "empty_slots": self.db.count_missing(),
            "coverage": coverage,
            "validation": report.as_dict(),
            "elapsed_seconds": round(elapsed, 3),
        }
        self._log_summary(summary)
        return summary

    def _log_summary(self, summary: dict) -> None:
        log = self.logger
        log.info("=" * 70)
        log.info("SUMMARY")
        log.info("  products                : %d", summary["products"])
        log.info(
            "  impact rows             : %d (expected %d = %d x %d)",
            summary["impact_rows"],
            summary["products"] * len(REQUIRED_STEPS),
            summary["products"],
            len(REQUIRED_STEPS),
        )
        log.info(
            "  populated / empty slots : %d / %d",
            summary["coverage"]["populated_slots"],
            summary["empty_slots"],
        )
        log.info("  validation warnings     : %d", len(summary["validation"]["warnings"]))
        log.info("  elapsed                 : %.3f s", summary["elapsed_seconds"])
        log.info("=" * 70)
        log.info("Pipeline finished successfully")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lca-pipeline",
        description="Load LCA environmental impact data from CSV into SQLite.",
    )
    parser.add_argument(
        "csv",
        nargs="?",
        default=None,
        help=f"path to the LCA CSV (default: data/{DEFAULT_CSV_NAME})",
    )
    parser.add_argument(
        "--db", default=str(DB_PATH), help="output SQLite path (default: output/lca_data.db)"
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="keep existing rows instead of rebuilding the tables",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging on stderr")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logger = setup_logging(verbose=args.verbose)

    csv_path = Path(args.csv) if args.csv else DATA_DIR / DEFAULT_CSV_NAME
    if not csv_path.is_file():
        logger.error("CSV not found: %s", csv_path)
        return 2

    try:
        LCAPipeline(csv_path, Path(args.db), reset=not args.append).run()
    except ValidationError as exc:
        logger.error("Input rejected: %s", exc)
        return 1
    except Exception:
        logger.exception("Pipeline failed with an unexpected error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
