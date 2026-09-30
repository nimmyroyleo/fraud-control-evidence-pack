# Implementation Plan: Control Coverage Trend

**Branch**: `spec-kit-evaluation` | **Date**: 2026-09-30 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/001-control-coverage-trend/spec.md`

## Summary

Add a cross-period "Control Coverage Trend" capability to the evidence pack: given the
current reporting period and a new `--compare-period <YYYY-MM>` argument, the tool loads
both periods' raw extracts in one run, computes each period's audit-checklist control
coverage using the existing coverage logic, and surfaces the change as (a) a headline
delta in percentage points, (b) a per-control trend classification, and (c) a new
`control_coverage_trend.csv` output with full lineage. A new HTML card is placed between
the existing hero coverage card and the charts. When no comparable prior period is
available, the pack renders a non-failing notice in place of the trend.

## Technical Context

**Language/Version**: Python 3.12 (standard library only — no third-party runtime imports)

**Primary Dependencies**: None at runtime (stdlib: `csv`, `hashlib`, `argparse`,
`dataclasses`, `datetime`, etc.). `pytest` for tests only.

**Storage**: Filesystem — CSV extracts under `data/`, outputs under `out/<period>/`.

**Testing**: `pytest` (`test_fraud_control_view.py`)

**Target Platform**: Any OS with Python 3.12; examiner-reproducible in a locked-down
environment.

**Project Type**: Single-file CLI tool (`fraud_control_view.py`) plus generator and tests.

**Performance Goals**: N/A (batch run over ~30k loans/period × a few cohorts); running the
coverage computation for a second period is acceptable overhead.

**Constraints**: Read-only inputs; deterministic byte-for-byte output; full lineage; no
wall-clock in hashed payload; no new runtime dependency.

**Scale/Scope**: Two periods per run (current + one comparison). One production module
changed; one test module extended.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Impact | Compliance approach |
|---|---|---|
| I. Read-Only Evidence Integrity | Feature reads a second period's extracts | Compare-period files opened read-only; their digests captured on load and included in the manifest `inputs` + read-only re-verification, identical to current-period files. |
| II. Full Lineage & Traceability | New CSV + new summary metrics | Every new output column (trend CSV + any new summary rows) gets an explicit `build_lineage_rows()` entry; no undeclared output. |
| III. Deterministic & Idempotent Output | New rows enter the hashed payload | Trend derived only from the two periods' deterministic coverage; no wall-clock or ordering nondeterminism; controls emitted in a stable sorted order. |
| IV. No Credit Decisioning; Maturity Labels | Trend is control-coverage, not fraud loss | No borrower-level decision introduced; coverage figures are factual counts; provisional/loss labeling untouched. |
| V. Stdlib-Only Runtime, Test-Enforced | Implementation + tests | No new imports; new behavior covered by new pytest cases that assert FR/SC. |

**Result**: PASS — no violations; Complexity Tracking not required.

## Project Structure

### Documentation (this feature)

```text
specs/001-control-coverage-trend/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output (CLI + output-file contracts)
└── tasks.md             # Phase 2 output (/speckit-tasks — not created here)
```

### Source Code (repository root)

```text
fraud_control_view.py        # single production module — all changes land here
├── build_arg_parser()       # +--compare-period argument
├── load_sources()           # invoked for the compare period as well
├── build_report()           # reused unchanged to score the compare period
├── (new) build_control_trend_rows()   # compares two periods' control_rows
├── (new) ControlTrendRow dataclass    # one row per checklist control
├── build_summary_rows()     # +prior-period pct and delta metrics
├── build_lineage_rows()     # +lineage for trend CSV and new summary metrics
├── CSV_OUTPUTS              # +("control_coverage_trend.csv", "trend_rows")
├── render_html()            # +control-coverage-trend card after the hero card
└── Report dataclass         # +trend_rows field, +compare metadata

test_fraud_control_view.py   # extended with trend correctness, edge, determinism, lineage, regression tests

data/                        # existing extracts for 2026-05 (prior) and 2026-08 (current)
```

**Structure Decision**: Keep the single-file CLI architecture. No new modules or packages
are introduced — this preserves the stdlib-only, examiner-reproducible posture and keeps
the change surface auditable. All production logic lands in `fraud_control_view.py`; all
verification lands in `test_fraud_control_view.py`.

## Complexity Tracking

> No Constitution Check violations — section intentionally empty.
