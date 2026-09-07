# LCA Product Impact Pipeline

Reads the supplied LCA export (CSV), extracts **Climate change (kg CO2 eq)** and
**Water use (m3 eq)** for the eight logical process steps named in the task, and
writes them to a local SQLite database.

## Requirements

- Python 3.10 or newer (the code uses `X | None` type syntax)
- pandas 1.5+

```bash
pip install -r requirements.txt
```

## Running

```bash
python run.py                      # uses data/<supplied csv>, writes output/lca_data.db
python run.py path/to/other.csv    # explicit input
python run.py --db /tmp/other.db   # explicit output
python run.py -v                   # debug logging on the console
python run.py --append             # keep existing rows instead of rebuilding
```

By default each run rebuilds the tables, so re-running is safe and idempotent.

## Tests

```bash
python -m unittest discover -s tests -v
```

25 tests. The end-to-end tests run the whole pipeline against the supplied CSV
and check the loaded values against the source file; they skip automatically if
the CSV is absent.

Measured statement coverage (`python -m coverage run --source=src -m unittest
discover -s tests && python -m coverage report`): **86%**. The uncovered lines
are the logging setup and the CLI argument handling.

## Output

| Path | Contents |
|---|---|
| `output/lca_data.db` | SQLite database |
| `output/logs/lca_pipeline_<timestamp>.log` | Full DEBUG-level log for the run |

## Data model

Two tables and one view.

**`products`** — one row per Product Ref.

| Column | Notes |
|---|---|
| `product_ref` | **primary key**; the task names this the main identifier |
| `product_id` | source UUID |
| `color_code` | |
| `supplier` | only populated on `PRODUCT_IMPACT` rows, carried across |
| `loaded_at` | |

**`product_impacts`** — one row per (product, logical step). Always eight rows
per product.

| Column | Notes |
|---|---|
| `product_ref` | FK to `products`, `ON DELETE CASCADE` |
| `process_step` | one of the eight steps |
| `step_kind` | `LIFECYCLE` (six) or `COMPONENT` (two) |
| `climate_change_kg_co2_eq` | `REAL`, nullable |
| `water_use_m3_eq` | `REAL`, nullable |
| `is_present` | `1` if the source had this step, `0` if it was absent |
| `loaded_at` | |

Primary key `(product_ref, process_step)`.

**`v_product_impacts_wide`** — the same data pivoted to one row per product with
sixteen measurement columns, which is the shape the task description implies when
it lists the eight steps side by side. Useful for a quick look:

```sql
SELECT product_ref, main_fabric_co2, linings_co2 FROM v_product_impacts_wide;
```

### The eight steps

Six come from rows where `source = 'PRODUCT_LIFECYCLE_STEP'`, matched on
`Process Step`:

`END_OF_LIFE`, `USE_PHASE`, `DISTRIBUTION`, `WAREHOUSE`, `PRODUCT_TRANSPORT`,
`MANUFACTURING`

Two come from rows where `source = 'COMPONENT_IMPACT'`, matched on
`Component category`:

`MAIN_FABRIC`, `LININGS`

All other `source` values (`COMPONENT_LIFECYCLE_STEP`, `MATERIAL_IMPACT`,
`MATERIAL_LIFECYCLE_STEP`, `PRODUCT_IMPACT`) describe finer or aggregate levels
and are ignored, as the task asks.

## Results for the supplied CSV

```
538 rows read, 206 in scope (168 lifecycle + 38 component)
28 products
224 impact rows (28 x 8)
206 slots populated, 18 empty
0 validation warnings
```

The 18 empty slots are all `LININGS`: only 10 of the 28 products have a lining
component in the source. See the note on missing data below.

Every value loaded was checked against the CSV; the maximum discrepancy was 0.

## Example queries

```sql
-- one product, all eight steps
SELECT process_step, step_kind, climate_change_kg_co2_eq, water_use_m3_eq, is_present
FROM product_impacts
WHERE product_ref = '2616093001'
ORDER BY step_kind DESC, process_step;

-- products with no lining
SELECT product_ref FROM product_impacts
WHERE process_step = 'LININGS' AND is_present = 0;

-- total CO2 across the eight steps, per product
SELECT product_ref, ROUND(SUM(climate_change_kg_co2_eq), 4) AS total_co2
FROM product_impacts GROUP BY product_ref ORDER BY total_co2 DESC;
```

```python
import sqlite3, pandas as pd
conn = sqlite3.connect("output/lca_data.db")
df = pd.read_sql("SELECT * FROM v_product_impacts_wide", conn)
```

## Missing data

18 of the 28 products have no `LININGS` component. This is a property of the
garments, not a defect in the export.

The pipeline writes all eight rows for every product regardless, with
`climate_change_kg_co2_eq` and `water_use_m3_eq` as `NULL` and `is_present = 0`
where the source had nothing. Writing `0.0` instead would have made "this
garment has no lining" indistinguishable from "this lining has zero measured
impact" — and the data contains genuine zeros, for instance `WAREHOUSE` is 0.0
for every product. Both cases are covered by tests.

## Project layout

```
run.py                      entry point
requirements.txt
src/
  config.py                 paths, the eight steps, column names, logging setup
  database.py               schema, upserts, read helpers
  transformer.py            CSV parsing, step extraction, the 8-step grid
  validator.py              structural validation, coverage reporting
  main.py                   orchestration and CLI
tests/test_pipeline.py      25 tests
data/                       input CSV
output/                     database and logs
DECISIONS.md                assumptions and technical decisions
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| `TypeError: unsupported operand type(s) for \|` | Python older than 3.10 |
| `CSV not found` (exit code 2) | wrong path; pass it explicitly |
| `Input rejected: CSV is missing required columns` (exit 1) | the export changed shape |
| Tests report `skipped` | the CSV is not in `data/` |
