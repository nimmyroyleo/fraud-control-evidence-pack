# CLI & Output Contracts: Control Coverage Trend

This is a CLI tool; the "contracts" are the command interface and the output-file schema.

## Command-line contract

New optional argument on `fraud_control_view.py`:

| Argument | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `--compare-period` | `YYYY-MM` | No | none | Prior reporting period to compare control coverage against. |

Example:

```bash
python fraud_control_view.py --period 2026-08 --as-of 2026-09-15 \
    --input-dir data --output-dir out --audit-date 2026-10-28 \
    --compare-period 2026-05
```

Behavior:
- **Provided and usable** → trend computed; `control_coverage_trend.csv` populated; HTML
  card shows prior %, current %, signed delta, and per-control changes.
- **Omitted** → no trend; `control_coverage_trend.csv` contains header only; HTML card
  shows a "no comparison period specified" notice. Run succeeds.
- **Provided but extracts missing/incomplete** → same non-failing notice, explaining the
  compare period could not be computed (FR-009). Run succeeds.

The argument value is recorded in `manifest.run.parameters` for reproducibility.

## Output-file contract: `control_coverage_trend.csv`

One row per checklist control (see [data-model.md](../data-model.md) for field semantics).

Header (stable column order):

```csv
control_id,control_title,framework,prior_period,current_period,prior_status,current_status,change_class
```

- Encoding/format identical to existing CSV outputs (BOM-aware, `\n` line terminator for
  idempotency).
- Rows sorted by `control_id`.
- Every column declared in `metric_lineage.csv`.

## Lineage contract

`metric_lineage.csv` MUST gain one row per `control_coverage_trend.csv` column and one row
per new `summary.csv` metric (`controls_evidenced_pct_prior`, `controls_evidenced_pct_delta`),
each naming its source files/columns and transform.

## Manifest contract

- `control_coverage_trend.csv` appears in `manifest.outputs` and `manifest.row_counts`.
- Its SHA-256 participates in `deterministic_payload_sha256` exactly like other CSVs.
- The compare period's input files appear in `manifest.inputs` and
  `read_only_verification` when a compare period is supplied.
