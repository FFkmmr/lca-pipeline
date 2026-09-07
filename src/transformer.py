"""CSV parsing and transformation into the pipeline's target shape."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .config import (
    COL_CLIMATE,
    COL_COLOR_CODE,
    COL_COMPONENT_CATEGORY,
    COL_PRODUCT_ID,
    COL_PRODUCT_REF,
    COL_PROCESS_STEP,
    COL_SOURCE,
    COL_SUPPLIER,
    COL_WATER,
    COMPONENT_CATEGORY_PATTERNS,
    REQUIRED_STEPS,
    SOURCE_COMPONENT_IMPACT,
    SOURCE_PRODUCT_LIFECYCLE,
    STEP_KINDS,
    LIFECYCLE_STEPS,
    get_logger,
)

logger = get_logger()


class LCADataTransformer:
    """Turns the wide LCA export into one row per (product, logical step).

    The specification asks for eight logical steps per Product Ref. Products
    legitimately lack some of them — a garment without a lining has no LININGS
    component row — so every product gets all eight rows and absent ones are
    written with NULL measurements and `is_present = 0`. That keeps "no lining"
    distinguishable from "lining with zero impact", which a plain 0.0 would not.
    """

    def __init__(self, csv_path: Path) -> None:
        self.csv_path = Path(csv_path)
        self.df: pd.DataFrame | None = None
        self._products: pd.DataFrame | None = None
        self._impacts: pd.DataFrame | None = None

    # -- loading ------------------------------------------------------------

    def load(self) -> pd.DataFrame:
        """Read the CSV. Identifier columns stay strings to protect leading zeros."""
        logger.info("Reading CSV: %s", self.csv_path)
        self.df = pd.read_csv(
            self.csv_path,
            dtype={
                COL_PRODUCT_ID: "string",
                COL_PRODUCT_REF: "string",
                COL_COLOR_CODE: "string",
                COL_SUPPLIER: "string",
                COL_COMPONENT_CATEGORY: "string",
                COL_PROCESS_STEP: "string",
                COL_SOURCE: "string",
            },
        )
        logger.info(
            "Read %d rows x %d columns", len(self.df), len(self.df.columns)
        )
        self._products = None
        self._impacts = None
        return self.df

    @property
    def frame(self) -> pd.DataFrame:
        if self.df is None:
            raise RuntimeError("CSV not loaded; call load() first")
        return self.df

    # -- extraction ---------------------------------------------------------

    def products(self) -> pd.DataFrame:
        """One row per Product Ref with its master data (computed once)."""
        if self._products is not None:
            return self._products
        df = self.frame
        products = (
            df[[COL_PRODUCT_REF, COL_PRODUCT_ID, COL_COLOR_CODE, COL_SUPPLIER]]
            .dropna(subset=[COL_PRODUCT_REF])
            .copy()
        )
        # Supplier is only populated on the PRODUCT_IMPACT row, so take the first
        # non-null value per product rather than whatever row happens to sort first.
        products = (
            products.groupby(COL_PRODUCT_REF, as_index=False)
            .agg(
                {
                    COL_PRODUCT_ID: "first",
                    COL_COLOR_CODE: "first",
                    COL_SUPPLIER: lambda s: s.dropna().iloc[0] if s.notna().any() else None,
                }
            )
        )
        logger.info("Found %d unique Product Refs", len(products))
        self._products = products
        return products

    def _lifecycle_impacts(self) -> pd.DataFrame:
        df = self.frame
        mask = (df[COL_SOURCE] == SOURCE_PRODUCT_LIFECYCLE) & df[
            COL_PROCESS_STEP
        ].isin(LIFECYCLE_STEPS)
        out = df.loc[mask, [COL_PRODUCT_REF, COL_PROCESS_STEP, COL_CLIMATE, COL_WATER]].copy()
        out = out.rename(columns={COL_PROCESS_STEP: "process_step"})
        logger.info("Matched %d lifecycle impact rows", len(out))
        return out

    @staticmethod
    def map_component_category(category: object) -> str | None:
        """Map a CSV component category onto a logical step name."""
        if category is None or pd.isna(category):
            return None
        text = str(category).upper()
        for pattern, step in COMPONENT_CATEGORY_PATTERNS:
            if pattern in text:
                return step
        return None

    def _component_impacts(self) -> pd.DataFrame:
        df = self.frame
        subset = df.loc[df[COL_SOURCE] == SOURCE_COMPONENT_IMPACT].copy()
        subset["process_step"] = subset[COL_COMPONENT_CATEGORY].map(
            self.map_component_category
        )

        unmapped = subset.loc[subset["process_step"].isna(), COL_COMPONENT_CATEGORY]
        if not unmapped.empty:
            logger.warning(
                "Ignoring %d component rows with out-of-scope categories: %s",
                len(unmapped),
                sorted(unmapped.dropna().unique().tolist()),
            )

        out = subset.loc[
            subset["process_step"].notna(),
            [COL_PRODUCT_REF, "process_step", COL_CLIMATE, COL_WATER],
        ]

        # A product should not carry two rows for the same logical component.
        duplicates = out.duplicated(subset=[COL_PRODUCT_REF, "process_step"], keep=False)
        if duplicates.any():
            logger.warning(
                "Found %d duplicate (product, component) rows; keeping the first of each",
                int(duplicates.sum()),
            )
            out = out.drop_duplicates(subset=[COL_PRODUCT_REF, "process_step"], keep="first")

        logger.info("Matched %d component impact rows", len(out))
        return out

    def impacts(self) -> pd.DataFrame:
        """The full 8-steps-per-product grid, with absent steps as NULL (computed once)."""
        if self._impacts is not None:
            return self._impacts
        products = self.products()[[COL_PRODUCT_REF]]

        observed = pd.concat(
            [self._lifecycle_impacts(), self._component_impacts()], ignore_index=True
        )
        observed[COL_CLIMATE] = pd.to_numeric(observed[COL_CLIMATE], errors="coerce")
        observed[COL_WATER] = pd.to_numeric(observed[COL_WATER], errors="coerce")

        # Cartesian product of products x the eight required steps.
        grid = products.merge(
            pd.DataFrame({"process_step": list(REQUIRED_STEPS)}), how="cross"
        )
        merged = grid.merge(
            observed, on=[COL_PRODUCT_REF, "process_step"], how="left"
        )

        merged["step_kind"] = merged["process_step"].map(STEP_KINDS)
        merged["is_present"] = (
            merged[COL_CLIMATE].notna() | merged[COL_WATER].notna()
        ).astype(int)

        missing = int((merged["is_present"] == 0).sum())
        if missing:
            by_step = (
                merged.loc[merged["is_present"] == 0, "process_step"]
                .value_counts()
                .to_dict()
            )
            logger.info(
                "%d of %d step slots have no source data; recorded as NULL (%s)",
                missing,
                len(merged),
                by_step,
            )

        logger.info(
            "Built impact grid: %d products x %d steps = %d rows",
            len(products),
            len(REQUIRED_STEPS),
            len(merged),
        )
        self._impacts = merged
        return merged

    # -- database-ready tuples ----------------------------------------------

    def product_rows(self) -> list[tuple]:
        df = self.products()
        return [
            (
                row[COL_PRODUCT_REF],
                _none_if_na(row[COL_PRODUCT_ID]),
                _none_if_na(row[COL_COLOR_CODE]),
                _none_if_na(row[COL_SUPPLIER]),
            )
            for _, row in df.iterrows()
        ]

    def impact_rows(self) -> list[tuple]:
        df = self.impacts()
        return [
            (
                row[COL_PRODUCT_REF],
                row["process_step"],
                row["step_kind"],
                _none_if_na(row[COL_CLIMATE]),
                _none_if_na(row[COL_WATER]),
                int(row["is_present"]),
            )
            for _, row in df.iterrows()
        ]

    # -- reporting ----------------------------------------------------------

    def source_breakdown(self) -> dict[str, int]:
        return self.frame[COL_SOURCE].value_counts(dropna=False).to_dict()


def _none_if_na(value):
    """Convert pandas NA/NaN sentinels to None so SQLite stores a real NULL."""
    if value is None or pd.isna(value):
        return None
    if isinstance(value, float):
        return float(value)
    return str(value)
