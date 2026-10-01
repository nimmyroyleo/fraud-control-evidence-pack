# lineage-check Extension

**Version**: 1.0.0  
**Category**: Validation  
**Effect**: read-only

## Purpose

Validates that every output column referenced in a feature specification has a corresponding lineage declaration in `metric_lineage.csv`.

This read-only extension ensures traceability compliance before implementation begins.

## Usage

```bash
# Validate a spec file
python validate_spec_lineage.py specs/001-control-coverage-trend/spec.md

# Via Spec-Kit (when installed)
/lineage-check specs/001-control-coverage-trend/spec.md
```

## What It Checks

- Scans spec.md for output column references (e.g., `control_id`, `control_coverage_pct`)
- Verifies each column has a row in `metric_lineage.csv`
- Reports missing lineage declarations
- Fails if any column lacks lineage documentation

## Example Output

```
--- Lineage Check Report ---
Spec: specs/001-control-coverage-trend/spec.md
Columns referenced in spec: {'control_id', 'coverage_pct', 'trend_status'}
Columns in metric_lineage.csv: {'control_id', 'coverage_pct', 'trend_status'}

✅ All referenced columns have lineage declarations
```

## When to Use

- **Before** `/speckit-implement` to catch spec-to-code mismatches
- **During** `/speckit-analyze` as a consistency check
- **During** code review to verify output contracts

## Fraud-Control-Evidence-Pack Context

This extension enforces **Principle II (Traceable)** from the project constitution:
> Every figure resolves to a pinned file. Three layers: output row carries source_files; metric_lineage.csv states transform; manifest.json resolves logical names to paths.

By requiring lineage declarations at spec time, the extension catches broken traceability chains before implementation.
