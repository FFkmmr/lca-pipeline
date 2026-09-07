# Assumptions and technical decisions

## Reading the source file

**`source` is the column that determines row granularity.** The export
interleaves six kinds of row in one flat file. `Process Step` alone is not a safe
selector: the value `COMPONENT IMPACT` appears as a process step on component
rows, and step names like `SPINNING` recur at both the component and material
level. Selecting on `source` first, then on `Process Step` or
`Component category`, is unambiguous. Rows with any other `source` value are
ignored, as the task asks.

**`Product Ref` is the primary key, not `Product id`.** The task names Product
Ref as the main identifier. In the supplied file the two are 1:1 (28 each, no
Product Ref maps to more than one Product id or vice versa), and `Color Code` is
`DEFAULT` throughout, so no colourway disambiguation is needed. If a future
export carried multiple colourways per reference, the key would have to widen to
`(Product Ref, Color Code)`.

**Identifier columns are read as strings.** Product references such as
`2616093001` are numeric-looking but are codes. Letting pandas infer them as
integers would risk losing leading zeros on a future file. Covered by a test.

**Component categories are matched on substrings.** The source has
`MAIN FABRIC (WOVEN)`; a knit style would presumably read `MAIN FABRIC (KNIT)`.
Matching on `MAIN FABRIC` and `LINING` rather than exact strings keeps those
variants working. Anything unmatched is logged as a warning and skipped, so a new
component category shows up in the log rather than disappearing silently.

**Supplier is taken from the first populated row.** `Product Supplier` is only
filled on the `PRODUCT_IMPACT` row and is blank on every other row for the same
product, so a naive "first row wins" aggregation would drop it.

## Modelling

**All eight steps are modelled in one table.** The task calls all eight
"logical process steps", including the two component impacts, so they are stored
uniformly as steps. `step_kind` retains whether a row came from a lifecycle or a
component source, so nothing is lost. The alternative — separate
`process_impacts` and `component_impacts` tables — needs a `UNION` for the most
natural query ("show me the eight numbers for this product") and makes the
completeness check awkward.

**Every product gets all eight rows, absent ones as NULL.** This is the main
missing-data decision. 18 of the 28 products have no lining. The grid is built as
a cross join of products against the eight steps, left-joined onto what the CSV
actually contains, so the table is always 8 x *n* rows.

`NULL` rather than `0.0`, because the data contains real zeros — `WAREHOUSE` is
0.0 for every product in this file, and several `Water use` values are legitimately
0.0. Collapsing "absent" into 0.0 would corrupt any downstream average or sum.
The `is_present` flag makes the distinction explicit without forcing every
consumer to reason about NULL semantics, and both cases are covered by tests.

**A wide view is provided alongside the long table.** The long form is the right
storage shape; the wide form matches how the task describes the output and is
easier to eyeball. The view costs nothing and neither shape is authoritative over
the other.

## Behaviour

**Re-running rebuilds by default.** `create_schema(reset=True)` drops and
recreates the tables, so a second run against a corrected CSV cannot leave stale
rows behind. `--append` keeps existing rows and relies on the upserts instead.
Both paths are idempotent; there is a test that runs the pipeline twice and
checks the row count is unchanged.

**Validation fails loudly on structure, warns on content.** A missing required
column or zero in-scope rows raises `ValidationError` and the process exits 1 —
there is no useful output to produce. Recoverable problems (unparseable numbers,
negative values, duplicate keys, unknown component categories) are logged as
warnings and collected in the report, so an imperfect file still yields a usable
database. The supplied file produces no warnings.

**Quality checks look only at in-scope rows.** Computing outlier or negative-value
statistics over the whole file would mix in material- and component-level rows
that are never loaded, producing warnings about data the pipeline does not touch.

**Post-load verification.** After loading, the pipeline queries the database to
confirm every product has all eight step rows and logs an error if not. The load
is checked against the requirement rather than assumed correct.

**Foreign keys are enforced.** `PRAGMA foreign_keys = ON` is set per connection,
since SQLite defaults it off. There is a test that an impact row for an unknown
product is rejected.

## Tooling

**pandas**, as the task prefers. The join-based construction of the 8-step grid
(cross join, then left join) is the natural expression of "every product must
have all eight steps" and would be considerably more code with the `csv` module.
The file is small enough that everything fits in memory comfortably; for an
export orders of magnitude larger the extraction would move to chunked reads,
which the current structure allows without changing the schema.

**Standard library `unittest`** rather than pytest, to keep the dependency list
to one line. The tests run under pytest as well if preferred.

## Known limitations

- The whole CSV is loaded into memory. Fine at this size, would need chunking at
  a few million rows.
- `--append` upserts by `(product_ref, process_step)`; it will not detect that a
  product has disappeared from a newer export. A full rebuild handles that.
- Only the two impact columns named in the task are extracted. Adding others
  means widening the table or moving to a long `(step, indicator, value)` form —
  the latter would be the better shape if the set of indicators is expected to
  grow.
- `Color Code` is stored but not part of any key, on the evidence of this file.

## AI assistance

This implementation was written with **Claude Opus 5** (Anthropic), through the
Claude web interface, in a single interactive session: the task description and
CSV were supplied, the assistant inspected the data, wrote the code, tests and
documentation, ran them, and revised after a review pass.

The review pass mattered. The first version had four real defects that only
surfaced on re-checking: the test suite had been broken by a later change to the
import style and was no longer running at all despite being reported as passing;
a dead import left an `ERROR` line in the log that was submitted as a deliverable;
products without linings were simply absent from the database rather than
recorded as missing, which sidestepped the part of the task about optional data;
and the documentation contained performance and coverage figures that had never
been measured. The figures quoted in this repository are now measured — the
coverage number from `coverage report`, the timing from the pipeline's own clock,
the row counts from the database.

Session transcript available on request.
