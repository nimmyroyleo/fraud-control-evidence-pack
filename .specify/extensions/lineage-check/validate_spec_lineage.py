#!/usr/bin/env python3
"""
Lineage Validator: Checks that spec output columns are declared in metric_lineage.csv

This read-only validation gate ensures every output column referenced in a spec
has a corresponding entry in metric_lineage.csv, enforcing traceability.

Usage:
  python validate_spec_lineage.py specs/001-control-coverage-trend/spec.md
"""

import re
import sys
from pathlib import Path


def extract_output_columns_from_spec(spec_file):
    """Extract output column names from spec.md file."""
    try:
        spec_content = Path(spec_file).read_text()
    except FileNotFoundError:
        print(f"ERROR: Spec file not found: {spec_file}")
        return set()

    # Look for patterns like:
    # - `control_view.csv` (file)
    # - `control_id` (column reference)
    # - "control_coverage_trend.csv" (in output descriptions)

    # Find all backtick-quoted identifiers that look like columns or files
    pattern = r"`([a-z_]+(?:\.csv)?)`"
    matches = re.findall(pattern, spec_content)

    # Filter to likely columns (underscore-separated, lowercase)
    columns = set()
    for match in matches:
        if "_" in match and not match.endswith(".csv"):
            columns.add(match)

    return columns


def load_lineage_declarations(repo_root="."):
    """Load existing lineage declarations from metric_lineage.csv if it exists."""
    lineage_file = Path(repo_root) / "metric_lineage.csv"

    if not lineage_file.exists():
        print(f"NOTE: No metric_lineage.csv found in {repo_root}")
        return set()

    try:
        lineage_content = lineage_file.read_text()
        # First line is header; remaining lines have format: output_column,source_file,...
        lines = lineage_content.strip().split("\n")
        if len(lines) < 1:
            return set()

        # Extract output column names (first column)
        lineage_columns = set()
        for line in lines[1:]:  # Skip header
            parts = line.split(",")
            if parts:
                col = parts[0].strip()
                if col:
                    lineage_columns.add(col)

        return lineage_columns
    except Exception as e:
        print(f"WARNING: Could not parse metric_lineage.csv: {e}")
        return set()


def validate(spec_file, repo_root="."):
    """Validate that spec outputs are declared in lineage."""
    spec_columns = extract_output_columns_from_spec(spec_file)
    lineage_columns = load_lineage_declarations(repo_root)

    # For now, just report what was found
    print(f"\n--- Lineage Check Report ---")
    print(f"Spec: {spec_file}")
    print(f"Columns referenced in spec: {spec_columns if spec_columns else '(none detected)'}")
    print(f"Columns in metric_lineage.csv: {lineage_columns if lineage_columns else '(not present)'}")

    # Check for missing lineage
    missing = spec_columns - lineage_columns
    if missing:
        print(f"\n[MISSING] Lineage for: {missing}")
        print("ACTION: Add these columns to metric_lineage.csv")
        return False
    elif spec_columns:
        print(f"\n[OK] All referenced columns have lineage declarations")
        return True
    else:
        print(f"\n[NOTE] No output columns detected in spec (may be false negative)")
        return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        spec_file = "specs/001-control-coverage-trend/spec.md"
        print(f"No spec file provided; using default: {spec_file}")
    else:
        spec_file = sys.argv[1]

    success = validate(spec_file)
    sys.exit(0 if success else 1)
