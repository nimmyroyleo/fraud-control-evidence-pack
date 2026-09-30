# Feature Specification: Control Coverage Trend

**Feature Branch**: `spec-kit-evaluation`
**Created**: 2026-09-30
**Status**: Draft
**Input**: Compare audit-checklist control coverage between a prior and current
reporting period and surface the change in the evidence pack.

## User Scenarios & Testing

### User Story 1 - See whether control coverage improved or slipped (Priority: P1)
A compliance reviewer opens the evidence pack for the current period and immediately
sees how the headline "percentage of audit-checklist controls evidenced" compares to a
prior reporting period — e.g. 78% -> 84% (+6 points) — without doing any external
calculation.

**Why this priority**: The single most valuable signal of the feature; turns a static
snapshot into a trend an examiner can act on.

**Independent Test**: Run the pack for the current period against a specified prior
period; confirm the header/card shows both percentages and the signed delta.

**Acceptance Scenarios**:
1. **Given** two fully-usable periods, **When** the pack is generated, **Then** a
   "Control coverage trend" card shows prior %, current %, and the signed delta in
   percentage points.
2. **Given** coverage fell versus the prior period, **When** the pack renders, **Then**
   the delta is shown as negative (a decline), not hidden.

---

### User Story 2 - Identify which controls changed (Priority: P2)
The reviewer sees, per checklist control, whether it **newly gained** evidence,
**lost** evidence (regressed), stayed evidenced, or stayed unevidenced — so remediation
can be targeted at the controls that slipped.

**Independent Test**: Compare the two periods' control-coverage data by hand; confirm
each control's trend classification in the pack matches.

**Acceptance Scenarios**:
1. **Given** a control evidenced this period but not the prior one, **When** the pack
   renders, **Then** that control is classified as newly-evidenced.
2. **Given** a control evidenced in the prior period but not this one, **When** the pack
   renders, **Then** it is classified as regressed.

---

### User Story 3 - Re-derivable trend artifact (Priority: P3)
An examiner receives a new `control_coverage_trend.csv` and can trace every trend figure
back to its source, consistent with the rest of the pack.

**Independent Test**: Confirm the new CSV exists, each column appears in the lineage
output, and figures match the per-control classifications.

**Acceptance Scenarios**:
1. **Given** the pack is generated, **When** the outputs are inspected, **Then**
   `control_coverage_trend.csv` is present with one row per checklist control.

---

### Edge Cases
- A checklist control exists in one period but not the other (checklist changed between
  periods).
- The prior period's required extracts are missing or incomplete.
- Coverage is identical between periods (delta = 0).
- A control is unevidenced in both periods.

## Requirements

### Functional Requirements
- **FR-001**: The pack MUST report the current period's checklist-control coverage
  alongside a specified prior period's coverage, and the signed change between them.
- **FR-002**: Both periods' coverage MUST be computed from their raw source extracts
  within a single run; the feature MUST NOT depend on a previously saved output artifact.
- **FR-003**: Each checklist control MUST be classified as newly-evidenced, regressed,
  still-evidenced, or still-unevidenced based on the two periods.
- **FR-004**: The headline delta MUST be expressed in percentage points with a clear sign
  (improvement vs. decline).
- **FR-005**: A "Control coverage trend" section MUST appear in the HTML pack immediately
  after the existing headline coverage card and before the charts.
- **FR-006**: The feature MUST emit a new `control_coverage_trend.csv` (one row per
  checklist control) and MUST declare its columns in the pack's lineage output.
- **FR-007**: Output MUST remain deterministic and byte-for-byte reproducible across
  reruns; no wall-clock or non-deterministic value may enter the hashed payload.
- **FR-008**: The prior comparison period MUST be identified via an explicit run input —
  a new `--compare-period <YYYY-MM>` command-line argument — not auto-detected, because
  the comparable prior period is not necessarily the immediately preceding calendar month.
- **FR-009**: If the prior period cannot be computed (missing/incomplete extracts, or no
  `--compare-period` supplied), the pack MUST render with a clear, non-failing notice in
  place of the trend rather than aborting the run or emitting misleading figures.

### Key Entities
- **Checklist control**: an audit checklist control and whether it is evidenced in a
  given period.
- **Coverage snapshot**: the set of evidenced/unevidenced controls for one period.
- **Control trend row**: one control's prior status, current status, and change class.

## Success Criteria

### Measurable Outcomes
- **SC-001**: A reviewer can determine, from the pack alone, whether coverage improved or
  declined versus the prior period, with no external calculation.
- **SC-002**: Every per-control trend classification matches an independent manual
  comparison of the two periods' control-coverage data.
- **SC-003**: Regenerating the pack for the same inputs produces byte-identical outputs.
- **SC-004**: Every trend figure is traceable to its source extracts via the pack's
  lineage output.

## Assumptions
- Exactly two fully-usable periods are in scope for this feature: prior = 2026-05,
  current = 2026-08 (intervening months lack the required alert/rule-fire/review extracts).
- The prior comparison period is supplied via a new `--compare-period <YYYY-MM>` argument.
- The existing control-coverage computation is reused unchanged to score both periods,
  so the current-period figure continues to match the existing coverage card.
