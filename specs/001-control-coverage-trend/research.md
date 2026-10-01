# Phase 0 Research: Control Coverage Trend

All spec inputs were resolved during `/speckit-specify`; no `[NEEDS CLARIFICATION]`
markers remain. This document records the design decisions that shape Phase 1.

## Decision 1: How to obtain the prior period's coverage

- **Decision**: Load the compare period's raw extracts with the existing `load_sources()`
  and run the existing `build_report()` for that period, consuming only its
  `control_rows` and `controls_evidenced_pct`.
- **Rationale**: Reuses the exact coverage logic used for the current period, so the two
  figures are computed identically and the current-period number continues to match the
  existing hero card (spec Assumption 3, Constitution II/III). Fully self-contained and
  reproducible from raw inputs (spec FR-002, Option A).
- **Alternatives considered**:
  - *Coverage-only recompute for the compare period* — rejected: duplicates evidence-status
    logic, risking drift from the canonical computation.
  - *Read a previously saved `control_coverage.csv`* (Option B) — rejected by stakeholder:
    depends on a prior run's artifact; less auditable.

## Decision 2: Specifying the prior period

- **Decision**: New optional CLI argument `--compare-period <YYYY-MM>`.
- **Rationale**: The comparable prior period (2026-05) is not the immediately preceding
  calendar month, so it cannot be auto-derived (spec FR-008). Explicit input keeps the run
  reproducible and self-documenting via the manifest parameters.
- **Alternatives considered**: auto-detect previous month — rejected (wrong period, and
  intervening months lack required extracts).

## Decision 3: Per-control change classification

- **Decision**: Classify each checklist control by comparing its evidenced/not-evidenced
  state in the two periods:
  - `NEWLY_EVIDENCED` — not evidenced prior, evidenced current
  - `REGRESSED` — evidenced prior, not evidenced current
  - `STILL_EVIDENCED` — evidenced in both
  - `STILL_UNEVIDENCED` — evidenced in neither
- **Rationale**: Directly answers spec User Story 2; four mutually exclusive, testable
  classes. "Evidenced" reuses the existing `EV_EVIDENCED` status (spec FR-003).
- **Edge handling**: A control present in only one period (checklist changed) is classed
  `NEW_CONTROL` (only current) or `RETIRED_CONTROL` (only prior); these are reported, not
  dropped (spec Edge Cases).

## Decision 4: Missing / unusable compare period (FR-009)

- **Decision**: If `--compare-period` is omitted, or any required extract for it is not
  `LOADED`, the tool skips trend computation, emits an empty `control_coverage_trend.csv`
  (header only), and the HTML card shows a clear notice explaining why no trend is shown.
  The run does not abort.
- **Rationale**: Keeps the pack usable and deterministic while never presenting misleading
  figures (spec FR-009, Constitution III).

## Decision 5: Determinism of the new rows

- **Decision**: Trend rows are emitted sorted by `control_id`; the headline delta and new
  summary metrics are pure functions of the two coverage snapshots; no timestamps enter
  the trend outputs.
- **Rationale**: Preserves byte-for-byte reproducibility and the determinism digest
  (spec FR-007, SC-003, Constitution III).

## Decision 6: Read-only integrity for the second period

- **Decision**: The compare period's input files flow through the same digest-capture and
  post-run re-verification path as current-period inputs, appearing in `manifest.inputs`
  and `read_only_verification`.
- **Rationale**: Constitution I applies equally to every extract the run reads.
