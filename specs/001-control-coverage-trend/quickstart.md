# Quickstart & Validation: Control Coverage Trend

Validates the feature end-to-end. See [contracts/cli-and-outputs.md](contracts/cli-and-outputs.md)
and [data-model.md](data-model.md) for details.

## Prerequisites

- Python 3.12, repository checked out, synthetic data present under `data/` for periods
  `2026-05` and `2026-08` (already generated; otherwise run
  `python generate_sample_data.py --out-dir data`).

## Run with a comparison period

```bash
python fraud_control_view.py --period 2026-08 --as-of 2026-09-15 \
    --input-dir data --output-dir out --audit-date 2026-10-28 \
    --compare-period 2026-05
```

Expected:
- `out/2026-08/control_coverage_trend.csv` exists with one row per checklist control and
  the columns listed in the output contract.
- `out/2026-08/evidence_pack.html` shows a "Control coverage trend" card between the
  headline coverage card and the "Alert volume by fraud rule" chart, displaying
  prior %, current %, and the signed delta.
- `summary.csv` contains `controls_evidenced_pct_prior` and `controls_evidenced_pct_delta`.
- `metric_lineage.csv` contains a row for every new column/metric.
- `manifest.json` lists `control_coverage_trend.csv` under `outputs` and `row_counts`, and
  lists the `2026-05` extracts under `inputs`.

## Run without a comparison period (graceful degradation)

```bash
python fraud_control_view.py --period 2026-08 --as-of 2026-09-15 \
    --input-dir data --output-dir out --audit-date 2026-10-28
```

Expected:
- Run succeeds.
- `control_coverage_trend.csv` contains only the header row.
- The HTML card shows a notice that no comparison period was specified.

## Determinism check

```bash
python fraud_control_view.py --period 2026-08 --as-of 2026-09-15 --input-dir data \
    --output-dir out_a --compare-period 2026-05
python fraud_control_view.py --period 2026-08 --as-of 2026-09-15 --input-dir data \
    --output-dir out_b --compare-period 2026-05
```

Expected: `control_coverage_trend.csv` (and all non-manifest outputs) are byte-identical
between `out_a/2026-08` and `out_b/2026-08`.

## Automated tests

```bash
python -m pytest
```

Expected: the full suite passes, including new cases for trend correctness (per-control
classification), the graceful no-compare-period path, determinism, lineage completeness,
and regression (existing coverage card/table unchanged).
