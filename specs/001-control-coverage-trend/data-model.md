# Phase 1 Data Model: Control Coverage Trend

## New entity: Control Trend Row

One row per checklist control considered across the two periods. Emitted to
`control_coverage_trend.csv` and rendered in the new HTML card.

| Field | Type | Description |
|---|---|---|
| `control_id` | str | Audit checklist control identifier (join key across periods). |
| `control_title` | str | Human-readable control text (carried from the control row). |
| `framework` | str | Regulatory framework the control belongs to (carried from the control row). |
| `prior_period` | str | The compare period (`YYYY-MM`), e.g. `2026-05`. |
| `current_period` | str | The reporting period (`YYYY-MM`), e.g. `2026-08`. |
| `prior_status` | str | `EVIDENCED` or `NOT_EVIDENCED` (derived from prior `evidence_status`). |
| `current_status` | str | `EVIDENCED` or `NOT_EVIDENCED` (derived from current `evidence_status`). |
| `change_class` | str | One of `NEWLY_EVIDENCED`, `REGRESSED`, `STILL_EVIDENCED`, `STILL_UNEVIDENCED`, `NEW_CONTROL`, `RETIRED_CONTROL`. |

**Derivation**: `EVIDENCED` iff the period's `ControlCoverageRow.evidence_status ==
EV_EVIDENCED`; every other status maps to `NOT_EVIDENCED`. `change_class` is a pure
function of (`prior_status`, `current_status`) plus presence in each period.

**Ordering**: rows sorted by `control_id` for deterministic output.

## New summary metrics (added to `summary.csv`)

| Metric key | Description | Computation |
|---|---|---|
| `controls_evidenced_pct_prior` | Prior period's evidenced coverage % | From the compare period's `build_report()` result. |
| `controls_evidenced_pct_delta` | Signed change in percentage points | `controls_evidenced_pct - controls_evidenced_pct_prior`. |

Both are omitted (or rendered as `n/a`) when no usable compare period is present.

## Changes to existing entities

- **`Report`**: add `trend_rows: list[ControlTrendRow]` (empty when no compare period) and
  optional compare-period metadata (prior period string, prior evidenced pct, usable flag).
- **`ControlCoverageRow`**: unchanged — consumed as input to the trend computation.

## Relationships

```text
CompareReport.control_rows ─┐
                            ├─> build_control_trend_rows() ─> [ControlTrendRow] ─> control_coverage_trend.csv
CurrentReport.control_rows ─┘                                                   └─> HTML "Control coverage trend" card
```

## Validation rules

- `change_class` MUST be exactly one of the enumerated values.
- Every `control_id` present in either period MUST appear exactly once in the trend rows.
- `current_status` distribution MUST reconcile with the existing control-coverage table
  (count of `EVIDENCED` equals `controls_evidenced`).
