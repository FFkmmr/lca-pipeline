"""Test suite for the LCA pipeline.

Run with:  python -m pytest tests -q      (or)  python -m unittest discover tests
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import DEFAULT_CSV_NAME, PROJECT_ROOT, REQUIRED_STEPS  # noqa: E402
from src.database import DatabaseManager  # noqa: E402
from src.main import LCAPipeline  # noqa: E402
from src.transformer import LCADataTransformer  # noqa: E402
from src.validator import DataValidator, ValidationError  # noqa: E402

REAL_CSV = PROJECT_ROOT / "data" / DEFAULT_CSV_NAME


def make_csv(path: Path, rows: list[dict]) -> Path:
    """Write a minimal CSV containing only the columns the pipeline requires."""
    columns = [
        "Product id",
        "Product Ref",
        "Color Code",
        "Product Supplier",
        "Component category",
        "Process Step",
        "Climate change - kg CO2 eq",
        "Water use - m3 eq",
        "source",
    ]
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False)
    return path


def lifecycle_rows(ref: str, climate: float = 1.0, water: float = 2.0) -> list[dict]:
    steps = ["END_OF_LIFE", "USE_PHASE", "DISTRIBUTION", "WAREHOUSE",
             "PRODUCT_TRANSPORT", "MANUFACTURING"]
    return [
        {
            "Product id": f"id-{ref}",
            "Product Ref": ref,
            "Color Code": "DEFAULT",
            "Product Supplier": None,
            "Component category": None,
            "Process Step": step,
            "Climate change - kg CO2 eq": climate,
            "Water use - m3 eq": water,
            "source": "PRODUCT_LIFECYCLE_STEP",
        }
        for step in steps
    ]


def component_row(ref: str, category: str, climate: float, water: float) -> dict:
    return {
        "Product id": f"id-{ref}",
        "Product Ref": ref,
        "Color Code": "DEFAULT",
        "Product Supplier": None,
        "Component category": category,
        "Process Step": "COMPONENT IMPACT",
        "Climate change - kg CO2 eq": climate,
        "Water use - m3 eq": water,
        "source": "COMPONENT_IMPACT",
    }


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# Schema and persistence
# ---------------------------------------------------------------------------


class TestDatabaseManager(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db = DatabaseManager(self.tmp / "test.db")
        self.db.create_schema()

    def _tables(self) -> set[str]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchall()
        return {row[0] for row in rows}

    def test_schema_objects_created(self):
        tables = self._tables()
        self.assertIn("products", tables)
        self.assertIn("product_impacts", tables)
        self.assertIn("v_product_impacts_wide", tables)

    def test_insert_and_count(self):
        self.db.insert_products([("REF1", "id1", "DEFAULT", "SupplierA")])
        self.db.insert_impacts(
            [("REF1", "MANUFACTURING", "LIFECYCLE", 1.5, 2.5, 1)]
        )
        self.assertEqual(self.db.count("products"), 1)
        self.assertEqual(self.db.count("product_impacts"), 1)

    def test_reinsert_is_idempotent(self):
        """Re-running must update in place, not duplicate rows."""
        for _ in range(2):
            self.db.insert_products([("REF1", "id1", "DEFAULT", "SupplierA")])
            self.db.insert_impacts([("REF1", "USE_PHASE", "LIFECYCLE", 1.0, 2.0, 1)])
        self.assertEqual(self.db.count("products"), 1)
        self.assertEqual(self.db.count("product_impacts"), 1)

    def test_upsert_overwrites_values(self):
        self.db.insert_products([("REF1", "id1", "DEFAULT", None)])
        self.db.insert_impacts([("REF1", "USE_PHASE", "LIFECYCLE", 1.0, 2.0, 1)])
        self.db.insert_impacts([("REF1", "USE_PHASE", "LIFECYCLE", 9.0, 8.0, 1)])
        summary = self.db.product_summary("REF1")
        self.assertEqual(summary["impacts"]["USE_PHASE"]["climate_change_kg_co2_eq"], 9.0)

    def test_null_impact_is_stored_as_null(self):
        """A missing measurement must be NULL, never coerced to 0.0."""
        self.db.insert_products([("REF1", "id1", "DEFAULT", None)])
        self.db.insert_impacts([("REF1", "LININGS", "COMPONENT", None, None, 0)])
        summary = self.db.product_summary("REF1")
        row = summary["impacts"]["LININGS"]
        self.assertIsNone(row["climate_change_kg_co2_eq"])
        self.assertIsNone(row["water_use_m3_eq"])
        self.assertEqual(row["is_present"], 0)

    def test_foreign_key_is_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.insert_impacts([("GHOST", "USE_PHASE", "LIFECYCLE", 1.0, 1.0, 1)])

    def test_reset_clears_previous_rows(self):
        self.db.insert_products([("REF1", "id1", "DEFAULT", None)])
        self.db.create_schema(reset=True)
        self.assertEqual(self.db.count("products"), 0)

    def test_incomplete_products_detected(self):
        self.db.insert_products([("REF1", "id1", "DEFAULT", None)])
        self.db.insert_impacts([("REF1", "USE_PHASE", "LIFECYCLE", 1.0, 1.0, 1)])
        self.assertEqual(self.db.products_with_incomplete_steps(), ["REF1"])


# ---------------------------------------------------------------------------
# Transformation
# ---------------------------------------------------------------------------


class TestTransformer(TempDirTestCase):
    def test_component_category_mapping(self):
        m = LCADataTransformer.map_component_category
        self.assertEqual(m("MAIN FABRIC (WOVEN)"), "MAIN_FABRIC")
        self.assertEqual(m("main fabric (knit)"), "MAIN_FABRIC")
        self.assertEqual(m("LININGS"), "LININGS")
        self.assertEqual(m("Lining"), "LININGS")
        self.assertIsNone(m("TRIMS"))
        self.assertIsNone(m(None))
        self.assertIsNone(m(float("nan")))

    def test_grid_has_eight_rows_per_product(self):
        csv = make_csv(
            self.tmp / "a.csv",
            lifecycle_rows("R1") + [component_row("R1", "MAIN FABRIC (WOVEN)", 5.0, 6.0)],
        )
        t = LCADataTransformer(csv)
        t.load()
        impacts = t.impacts()
        self.assertEqual(len(impacts), len(REQUIRED_STEPS))
        self.assertEqual(set(impacts["process_step"]), set(REQUIRED_STEPS))

    def test_absent_lining_becomes_null_row(self):
        """The core missing-data case: a garment with no lining."""
        csv = make_csv(
            self.tmp / "b.csv",
            lifecycle_rows("R1") + [component_row("R1", "MAIN FABRIC (WOVEN)", 5.0, 6.0)],
        )
        t = LCADataTransformer(csv)
        t.load()
        impacts = t.impacts().set_index("process_step")
        lining = impacts.loc["LININGS"]
        self.assertTrue(pd.isna(lining["Climate change - kg CO2 eq"]))
        self.assertEqual(lining["is_present"], 0)
        fabric = impacts.loc["MAIN_FABRIC"]
        self.assertEqual(fabric["is_present"], 1)
        self.assertEqual(fabric["Climate change - kg CO2 eq"], 5.0)

    def test_zero_impact_is_present_not_missing(self):
        """0.0 is a real measurement and must not be confused with absent data."""
        csv = make_csv(self.tmp / "c.csv", lifecycle_rows("R1", climate=0.0, water=0.0))
        t = LCADataTransformer(csv)
        t.load()
        impacts = t.impacts().set_index("process_step")
        self.assertEqual(impacts.loc["WAREHOUSE", "is_present"], 1)
        self.assertEqual(impacts.loc["WAREHOUSE", "Climate change - kg CO2 eq"], 0.0)

    def test_out_of_scope_sources_ignored(self):
        rows = lifecycle_rows("R1")
        rows.append(
            {
                "Product id": "id-R1",
                "Product Ref": "R1",
                "Color Code": "DEFAULT",
                "Product Supplier": None,
                "Component category": "MAIN FABRIC (WOVEN)",
                "Process Step": "DYEING",
                "Climate change - kg CO2 eq": 999.0,
                "Water use - m3 eq": 999.0,
                "source": "COMPONENT_LIFECYCLE_STEP",
            }
        )
        csv = make_csv(self.tmp / "d.csv", rows)
        t = LCADataTransformer(csv)
        t.load()
        impacts = t.impacts()
        self.assertNotIn(999.0, impacts["Climate change - kg CO2 eq"].dropna().tolist())

    def test_supplier_taken_from_populated_row(self):
        rows = lifecycle_rows("R1")
        rows[0]["Product Supplier"] = None
        rows.append(
            {
                "Product id": "id-R1",
                "Product Ref": "R1",
                "Color Code": "DEFAULT",
                "Product Supplier": "CHOICE_PLOT_1",
                "Component category": None,
                "Process Step": "PRODUCT IMPACT",
                "Climate change - kg CO2 eq": 1.0,
                "Water use - m3 eq": 1.0,
                "source": "PRODUCT_IMPACT",
            }
        )
        csv = make_csv(self.tmp / "e.csv", rows)
        t = LCADataTransformer(csv)
        t.load()
        self.assertEqual(t.product_rows()[0][3], "CHOICE_PLOT_1")

    def test_product_ref_keeps_leading_zeros(self):
        rows = lifecycle_rows("0012345")
        csv = make_csv(self.tmp / "f.csv", rows)
        t = LCADataTransformer(csv)
        t.load()
        self.assertEqual(t.product_rows()[0][0], "0012345")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidator(TempDirTestCase):
    def test_missing_columns_raise(self):
        df = pd.DataFrame({"nonsense": [1, 2]})
        with self.assertRaises(ValidationError):
            DataValidator().validate(df)

    def test_no_in_scope_rows_raises(self):
        csv = make_csv(
            self.tmp / "g.csv",
            [
                {
                    "Product id": "id",
                    "Product Ref": "R1",
                    "Color Code": "DEFAULT",
                    "Product Supplier": None,
                    "Component category": None,
                    "Process Step": "DYEING",
                    "Climate change - kg CO2 eq": 1.0,
                    "Water use - m3 eq": 1.0,
                    "source": "MATERIAL_LIFECYCLE_STEP",
                }
            ],
        )
        df = pd.read_csv(csv)
        with self.assertRaises(ValidationError):
            DataValidator().validate(df)

    def test_valid_input_reports_in_scope_rows(self):
        csv = make_csv(self.tmp / "h.csv", lifecycle_rows("R1"))
        report = DataValidator().validate(pd.read_csv(csv))
        self.assertEqual(report.in_scope_rows, 6)
        self.assertEqual(report.missing_columns, [])

    def test_negative_values_warn_but_do_not_fail(self):
        csv = make_csv(self.tmp / "i.csv", lifecycle_rows("R1", climate=-1.0))
        report = DataValidator().validate(pd.read_csv(csv))
        self.assertTrue(any("negative" in w for w in report.warnings))


# ---------------------------------------------------------------------------
# End-to-end against the supplied dataset
# ---------------------------------------------------------------------------


@unittest.skipUnless(REAL_CSV.is_file(), "supplied CSV not present")
class TestEndToEndRealData(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db_path = self.tmp / "e2e.db"
        self.summary = LCAPipeline(REAL_CSV, self.db_path).run()

    def test_all_products_loaded(self):
        self.assertEqual(self.summary["products"], 28)

    def test_every_product_has_eight_steps(self):
        db = DatabaseManager(self.db_path)
        self.assertEqual(db.products_with_incomplete_steps(), [])
        self.assertEqual(db.count("product_impacts"), 28 * 8)

    def test_missing_linings_recorded_as_null(self):
        db = DatabaseManager(self.db_path)
        with db.connect() as conn:
            populated = conn.execute(
                "SELECT COUNT(*) FROM product_impacts "
                "WHERE process_step='LININGS' AND is_present=1"
            ).fetchone()[0]
            empty = conn.execute(
                "SELECT COUNT(*) FROM product_impacts "
                "WHERE process_step='LININGS' AND is_present=0 "
                "AND climate_change_kg_co2_eq IS NULL"
            ).fetchone()[0]
        self.assertEqual(populated, 10)
        self.assertEqual(empty, 18)

    def test_known_product_values_match_source(self):
        """Spot-check one product against the raw CSV."""
        db = DatabaseManager(self.db_path)
        summary = db.product_summary("2616093001")
        self.assertAlmostEqual(
            summary["impacts"]["MANUFACTURING"]["climate_change_kg_co2_eq"],
            0.551370298,
            places=9,
        )
        self.assertAlmostEqual(
            summary["impacts"]["MAIN_FABRIC"]["water_use_m3_eq"],
            14.115834143,
            places=9,
        )

    def test_wide_view_returns_one_row_per_product(self):
        db = DatabaseManager(self.db_path)
        with db.connect() as conn:
            rows = conn.execute("SELECT * FROM v_product_impacts_wide").fetchall()
        self.assertEqual(len(rows), 28)

    def test_rerun_is_idempotent(self):
        LCAPipeline(REAL_CSV, self.db_path).run()
        db = DatabaseManager(self.db_path)
        self.assertEqual(db.count("products"), 28)
        self.assertEqual(db.count("product_impacts"), 28 * 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
