"""Validation and data-quality reporting."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .config import (
    COL_CLIMATE,
    COL_PRODUCT_REF,
    COL_PROCESS_STEP,
    COL_SOURCE,
    COL_WATER,
    REQUIRED_COLUMNS,
    REQUIRED_STEPS,
    SOURCE_COMPONENT_IMPACT,
    SOURCE_PRODUCT_LIFECYCLE,
    get_logger,
)

logger = get_logger()


class ValidationError(Exception):
    """Raised when the input cannot be processed at all."""


@dataclass
class ValidationReport:
    total_rows: int = 0
    in_scope_rows: int = 0
    missing_columns: list[str] = field(default_factory=list)
    dropped_rows: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "total_rows": self.total_rows,
            "in_scope_rows": self.in_scope_rows,
            "missing_columns": self.missing_columns,
            "dropped_rows": self.dropped_rows,
            "warnings": self.warnings,
        }


class DataValidator:
    """Structural validation of the input frame.

    Hard failures (missing columns, no usable rows) raise; anything recoverable
    is recorded as a warning so that a partially imperfect file still produces
    a usable database.
    """

    def __init__(self) -> None:
        self.report = ValidationReport()

    def validate(self, df: pd.DataFrame) -> ValidationReport:
        self.report.total_rows = len(df)

        missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
        if missing:
            self.report.missing_columns = missing
            raise ValidationError(f"CSV is missing required columns: {missing}")
        logger.info("All %d required columns present", len(REQUIRED_COLUMNS))

        in_scope = df[df[COL_SOURCE].isin([SOURCE_PRODUCT_LIFECYCLE, SOURCE_COMPONENT_IMPACT])]
        self.report.in_scope_rows = len(in_scope)
        if in_scope.empty:
            raise ValidationError(
                "No rows with source PRODUCT_LIFECYCLE_STEP or COMPONENT_IMPACT"
            )
        logger.info(
            "%d of %d rows are in scope for extraction",
            len(in_scope),
            len(df),
        )

        no_ref = int(in_scope[COL_PRODUCT_REF].isna().sum())
        if no_ref:
            self.report.dropped_rows["missing_product_ref"] = no_ref
            self._warn("%d in-scope rows have no Product Ref and will be skipped", no_ref)

        no_step = int(in_scope[COL_PROCESS_STEP].isna().sum())
        if no_step:
            self.report.dropped_rows["missing_process_step"] = no_step
            self._warn("%d in-scope rows have no Process Step", no_step)

        self._check_numeric(in_scope)
        self._check_duplicates(in_scope)
        return self.report

    def _check_numeric(self, df: pd.DataFrame) -> None:
        for col in (COL_CLIMATE, COL_WATER):
            numeric = pd.to_numeric(df[col], errors="coerce")
            # Non-null in the source but non-numeric after coercion => bad value.
            unparsed = int((numeric.isna() & df[col].notna()).sum())
            if unparsed:
                self._warn("%d values in '%s' could not be parsed as numbers", unparsed, col)
            negatives = int((numeric < 0).sum())
            if negatives:
                self._warn(
                    "%d negative values in '%s' (may be legitimate credits/offsets)",
                    negatives,
                    col,
                )

    def _check_duplicates(self, df: pd.DataFrame) -> None:
        lifecycle = df[df[COL_SOURCE] == SOURCE_PRODUCT_LIFECYCLE]
        dupes = lifecycle.duplicated(subset=[COL_PRODUCT_REF, COL_PROCESS_STEP]).sum()
        if dupes:
            self._warn(
                "%d duplicate (Product Ref, Process Step) lifecycle rows; last value wins",
                int(dupes),
            )

    def _warn(self, message: str, *args) -> None:
        rendered = message % args
        self.report.warnings.append(rendered)
        logger.warning(rendered)


class CompletenessReporter:
    """Describes how much of the required 8-step grid the source actually fills."""

    def __init__(self, impacts: pd.DataFrame) -> None:
        self.impacts = impacts

    def summarise(self) -> dict:
        total = len(self.impacts)
        present = int(self.impacts["is_present"].sum())
        by_step = (
            self.impacts.groupby("process_step")["is_present"].agg(["sum", "count"]).to_dict("index")
        )
        summary = {
            "expected_slots": total,
            "populated_slots": present,
            "empty_slots": total - present,
            "coverage_pct": round(100.0 * present / total, 1) if total else 0.0,
            "by_step": {
                step: {"populated": int(v["sum"]), "expected": int(v["count"])}
                for step, v in by_step.items()
            },
        }
        logger.info(
            "Step coverage: %d/%d slots populated (%.1f%%)",
            present,
            total,
            summary["coverage_pct"],
        )
        for step in REQUIRED_STEPS:
            stats = summary["by_step"].get(step)
            if stats and stats["populated"] < stats["expected"]:
                logger.info(
                    "  %-18s %d/%d products have data",
                    step,
                    stats["populated"],
                    stats["expected"],
                )
        return summary
