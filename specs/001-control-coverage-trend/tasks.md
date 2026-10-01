# Tasks: Control Coverage Trend

**Input**: Design documents from `specs/001-control-coverage-trend/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/cli-and-outputs.md](contracts/cli-and-outputs.md)

**Tests**: INCLUDED — the project constitution (Principle V) requires tests as the enforcement mechanism, so every user story has test tasks written before its implementation.

**Organization**: Tasks are grouped by user story (US1–US3) so each can be implemented and verified independently.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files / independent, no ordering dependency)
- **[Story]**: US1 / US2 / US3, or FND (foundational) / SETUP / POLISH
- All production changes land in `fraud_control_view.py`; all tests in `test_fraud_control_view.py`.

## Path Conventions

Single-file CLI tool (per plan.md Structure Decision): production code in
`fraud_control_view.py`, tests in `test_fraud_control_view.py`, data fixtures under `data/`.

---

## Phase 1: Setup

**Purpose**: Establish a known-good baseline before changing anything.

- [ ] T001 [SETUP] Run `python -m pytest` and confirm the existing suite is green; record the baseline test count as the regression reference.
- [ ] T002 [SETUP] Confirm `data/` contains full extracts for both `2026-08` (current) and `2026-05` (compare) — `alerts_`, `rule_fires_`, `review_outcomes_`, `loans_` for each.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Shared plumbing every user story depends on. No user story can begin until this is done.

**⚠️ CRITICAL**: Complete this phase before Phase 3+.

- [ ] T003 [FND] Add a `--compare-period <YYYY-MM>` optional argument in `build_arg_parser()` (~L2703) and thread it into `RunParams` (~L2738); record it in the manifest run parameters. (FR-008)
- [ ] T004 [FND] Add a `ControlTrendRow` dataclass (fields per [data-model.md](data-model.md): `control_id`, `control_title`, `framework`, `prior_period`, `current_period`, `prior_status`, `current_status`, `change_class`).
- [ ] T005 [FND] Add `trend_rows: list[ControlTrendRow]` (default empty) plus compare-period metadata (prior period, prior evidenced pct, usable flag) to the `Report` dataclass (~L710).
- [ ] T006 [FND] Add a helper that, given a period, loads its sources (`load_sources`, ~L395) and builds its report (`build_report`, ~L779), returning the compare-period `Report`; detect "unusable compare period" (any required extract not `LOADED`) and surface it via the usable flag. (FR-002, FR-009, Constitution I: compare inputs read-only + digest-verified)

**Checkpoint**: `--compare-period` parsed, trend data structures exist, compare-period report obtainable.

---

## Phase 3: User Story 1 — Headline coverage delta (Priority: P1) 🎯 MVP

**Goal**: Show prior %, current %, and the signed percentage-point delta in the pack.

**Independent Test**: Run with `--compare-period 2026-05 --period 2026-08`; the pack shows both percentages and the signed delta; omit the arg and a non-failing notice appears.

### Tests for User Story 1

- [ ] T007 [P] [US1] Test: with a compare period, `summary.csv` contains `controls_evidenced_pct_prior` and `controls_evidenced_pct_delta`, and delta = current − prior. (SC-001)
- [ ] T008 [P] [US1] Test: the HTML pack renders a "Control coverage trend" card between the hero coverage card and the "Alert volume by fraud rule" heading. (FR-005)
- [ ] T009 [P] [US1] Test: with no `--compare-period`, the run succeeds and the card shows the graceful notice; no delta metrics are emitted as misleading values. (FR-009)

### Implementation for User Story 1

- [ ] T010 [US1] In `build_summary_rows()` (~L1344), add `controls_evidenced_pct_prior` and `controls_evidenced_pct_delta` metrics from the compare-period report (omit/`n/a` when not usable). (FR-001, FR-004)
- [ ] T011 [US1] In `render_html()` (~L2065, after the hero card, before the charts at ~L2068), add a "Control coverage trend" card showing prior %, current %, signed delta, or the FR-009 notice. (FR-005)
- [ ] T012 [US1] Ensure no wall-clock/non-deterministic value enters the new card or metrics (Constitution III / FR-007).

**Checkpoint**: Headline trend visible and correct; graceful path works. MVP complete.

---

## Phase 4: User Story 2 — Per-control change classification (Priority: P2)

**Goal**: Classify each checklist control as newly-evidenced / regressed / still-evidenced / still-unevidenced (plus new/retired edges).

**Independent Test**: Construct two coverage states; confirm each control's `change_class` matches a manual comparison.

### Tests for User Story 2

- [ ] T013 [P] [US2] Test: a control evidenced now but not prior → `NEWLY_EVIDENCED`; evidenced prior but not now → `REGRESSED`; both → `STILL_EVIDENCED`; neither → `STILL_UNEVIDENCED`. (FR-003, SC-002)
- [ ] T014 [P] [US2] Test: a control present in only one period → `NEW_CONTROL` / `RETIRED_CONTROL` (edge cases), and every control appears exactly once. (data-model validation)
- [ ] T015 [P] [US2] Test: count of `current_status == EVIDENCED` reconciles with existing `controls_evidenced`. (data-model validation / regression)

### Implementation for User Story 2

- [ ] T016 [US2] Add `build_control_trend_rows(current_report, compare_report)` producing `ControlTrendRow`s keyed by `control_id`, sorted by `control_id`; map `EV_EVIDENCED`→`EVIDENCED`, all else→`NOT_EVIDENCED`; derive `change_class`. (FR-003)
- [ ] T017 [US2] Populate `Report.trend_rows` in the main pipeline when a usable compare period exists (empty otherwise).
- [ ] T018 [US2] Extend the trend card (T011) with a per-control breakdown (counts per change class, and/or a table).

**Checkpoint**: Per-control classifications correct and reconciled.

---

## Phase 5: User Story 3 — Re-derivable trend artifact (Priority: P3)

**Goal**: Emit `control_coverage_trend.csv` with full lineage and manifest inclusion.

**Independent Test**: Confirm the CSV exists with the contracted columns, every column is in `metric_lineage.csv`, and it appears in the manifest.

### Tests for User Story 3

- [ ] T019 [P] [US3] Test: `control_coverage_trend.csv` is written with the exact header from [contracts/cli-and-outputs.md](contracts/cli-and-outputs.md), one row per control, sorted by `control_id`. (FR-006)
- [ ] T020 [P] [US3] Test: every `control_coverage_trend.csv` column and both new summary metrics have a `metric_lineage.csv` row. (Constitution II / FR-006)
- [ ] T021 [P] [US3] Test: no-compare-period run writes the CSV with header only. (FR-009)

### Implementation for User Story 3

- [ ] T022 [US3] Add `("control_coverage_trend.csv", "trend_rows")` to `CSV_OUTPUTS` (~L2519) so `write_csv_outputs()` emits it. (FR-006)
- [ ] T023 [US3] In `build_lineage_rows()` (~L1680), add `add(...)` entries for each trend CSV column and for `controls_evidenced_pct_prior` / `controls_evidenced_pct_delta`. (Constitution II)
- [ ] T024 [US3] Confirm the new CSV flows into `build_manifest()` `outputs`/`row_counts` and the determinism payload automatically via `CSV_OUTPUTS`. (FR-007)

**Checkpoint**: Trend CSV emitted, fully lineage-declared, manifest-tracked.

---

## Phase 6: Polish & Cross-Cutting

**Purpose**: Guarantees that span all stories.

- [ ] T025 [P] [POLISH] Determinism test: render the pack twice for the same inputs+compare period and assert non-manifest outputs (incl. `control_coverage_trend.csv`) are byte-identical. (SC-003 / FR-007)
- [ ] T026 [P] [POLISH] Regression test: existing coverage hero card, `control_coverage.csv`, and current-period `controls_evidenced_pct` are unchanged by the feature. (spec Assumption 3)
- [ ] T027 [POLISH] Read-only test: the `2026-05` compare inputs appear in `manifest.inputs` + `read_only_verification` and are unmodified after the run. (Constitution I)
- [ ] T028 [POLISH] Run `python -m pytest` — full suite green, count = baseline (T001) + new tests.
- [ ] T029 [POLISH] Execute [quickstart.md](quickstart.md) steps end-to-end (with/without compare period, determinism) and confirm expected outputs.

---

## Dependencies & Execution Order

- **Phase 1 (Setup)** → no dependencies.
- **Phase 2 (Foundational)** → after Setup; **blocks** all user stories.
- **US1 (P1)** → after Phase 2. MVP.
- **US2 (P2)** → after Phase 2; builds on US1's card (T018 extends T011).
- **US3 (P3)** → after US2 (needs `trend_rows` from T016/T017).
- **Phase 6 (Polish)** → after the user stories being delivered.

### Within each story
- Tests written first and expected to FAIL before implementation.
- Data structures → computation → rendering/output.

### Parallel opportunities
- T007–T009 (US1 tests) parallel; T013–T015 (US2 tests) parallel; T019–T021 (US3 tests) parallel.
- T025–T027 (polish tests) parallel.

## Implementation Strategy

Deliver **US1 as the MVP** (headline delta + graceful notice), then layer US2 (per-control
detail) and US3 (CSV + lineage). Each story is independently testable and leaves the suite
green.

## Task Summary

- **Total tasks**: 29
- **US1 (P1/MVP)**: 6 (3 tests, 3 impl) · **US2 (P2)**: 6 (3 tests, 3 impl) · **US3 (P3)**: 6 (3 tests, 3 impl)
- **Foundational**: 4 · **Setup**: 2 · **Polish**: 5
- **Parallel groups**: US1 tests, US2 tests, US3 tests, polish tests
