"""Audit-Readiness Fraud Control view - read-only monthly evidence builder.

PURPOSE
-------
For one calendar month, join the fraud rules engine decision log, the fraud alert
queue, manual review dispositions, the loan disbursal ledger, the confirmed-fraud
label file and the regulatory audit checklist into a single evidence pack that an
internal compliance team can hand to an examiner.

For every fraud rule in scope the pack reports: fire count, alert volume, the manual
review outcome breakdown, and the audit checklist control the rule evidences. It then
rolls that up into the headline "percentage of checklist controls evidenced" and an
observed fraud-loss baseline.

DESIGN CONSTRAINTS (non-negotiable - they are what makes the output auditable)
------------------------------------------------------------------------------
1. READ-ONLY. Source extracts are opened in text-read mode only. Input digests are
   captured on load and re-verified at the end of the run; a mismatch aborts with a
   non-zero exit. Writing into the input directory is refused.
2. NO CREDIT DECISIONS. This module contains no scoring, no thresholding of
   applicants, no approve/decline/refer logic. It describes decisions the rules
   engine already made. It must never be placed in a decisioning path.
3. TRACEABLE. Every emitted column carries the logical source files it derives from,
   `metric_lineage.csv` states the transform for each column, and `manifest.json`
   pins each logical source to a path, SHA-256 digest, row count and column list.
4. MONTHLY IDEMPOTENT. Output is a pure function of (reporting period, as-of date,
   input bytes). No wall-clock value enters any CSV or the HTML pack; re-running
   produces byte-identical files. The run timestamp lives only in the manifest,
   outside the hashed payload.
5. PROVISIONAL LABELLING. Confirmed fraud is labelled 60-90 days after disbursal, so
   any cohort younger than `--label-maturity-days` is reported as PROVISIONAL and the
   defensible loss baseline is taken from the most recent fully matured cohort.
6. UNMAPPED RULES ARE FLAGGED, NOT DROPPED. Active rules with no checklist control,
   controls with no active rule, and non-active or unregistered rules found in the
   decision log are all reported as explicit gaps.

USAGE
-----
    python fraud_control_view.py --period 2026-08 --as-of 2026-09-15 \
        --input-dir data --output-dir out

Outputs land in ``<output-dir>/<period>/``:

    control_view.csv          one row per fraud rule in scope
    control_coverage.csv      one row per audit checklist control
    unmapped_rules.csv        rule <-> control mapping gaps
    integrity_exceptions.csv  data integrity / change control exceptions
    fraud_loss_baseline.csv   per-cohort observed fraud loss
    summary.csv               headline metrics, each with its own lineage
    metric_lineage.csv        output column -> source file/column -> transform
    evidence_pack.html        single-file human-readable pack
    manifest.json             digests, row counts, parameters, determinism hash

Standard library only - no third-party runtime dependency, so an examiner can run
this in a locked-down environment and reproduce the numbers.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

TOOL_NAME = "audit-readiness-fraud-control-view"
TOOL_VERSION = "0.4.0"

# --------------------------------------------------------------------------------------
# Domain vocabulary
# --------------------------------------------------------------------------------------

OUTCOME_CONFIRMED = "CONFIRMED_FRAUD"
OUTCOME_CLEARED = "CLEARED_FALSE_POSITIVE"
OUTCOME_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
DECIDED_OUTCOMES = (OUTCOME_CONFIRMED, OUTCOME_CLEARED, OUTCOME_INSUFFICIENT)

STATUS_ACTIVE = "ACTIVE"

LABEL_FINAL = "FINAL"
LABEL_PROVISIONAL = "PROVISIONAL"
LABEL_NOT_APPLICABLE = "NOT_APPLICABLE"

# Rule -> control mapping states
MAP_MAPPED = "MAPPED"
MAP_UNMAPPED_ACTIVE = "UNMAPPED_ACTIVE_RULE"
MAP_UNMAPPED_NON_ACTIVE = "UNMAPPED_NON_ACTIVE_RULE"
MAP_UNREGISTERED = "UNREGISTERED_RULE"

# Control evidence states
EV_EVIDENCED = "EVIDENCED"
EV_NO_ACTIVITY = "NOT_EVIDENCED_NO_ACTIVITY_IN_PERIOD"
EV_NO_ACTIVE_RULE = "NOT_EVIDENCED_NO_ACTIVE_RULE_MAPPED"
EV_UNMAPPED = "NOT_EVIDENCED_NO_RULE_MAPPED"

SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}

#: Sample ids quoted inside an aggregated exception description.
EXCEPTION_SAMPLE_SIZE = 5


class SourceDataError(RuntimeError):
    """Raised when a required source extract is missing or structurally unusable."""


class ReadOnlyViolationError(RuntimeError):
    """Raised when an input file changed during the run, or output would overwrite input."""


# --------------------------------------------------------------------------------------
# Run parameters
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunParams:
    """Everything that determines the output, besides the input bytes themselves."""

    period: str                       # reporting month, "YYYY-MM"
    as_of: date                       # snapshot date; nothing after this is observed
    input_dir: Path
    output_dir: Path
    label_maturity_days: int = 90     # confirmed fraud lands 60-90 days post disbursal
    baseline_lookback_months: int = 3  # prior cohorts pulled in for the loss baseline
    expected_reviewer_count: int = 6  # manual review team size per the control narrative
    min_fires_for_evidence: int = 1   # a control is evidenced by >= this many fires
    stale_review_days: int = 365      # annual rule review expectation (SR 11-7)
    alert_ageing_sla_days: int = 30   # pending alert older than this is an exception
    audit_date: date | None = None    # optional: drives "days to audit" in the summary
    compare_period: str | None = None  # optional prior period for the coverage trend

    def period_bounds(self) -> tuple[date, date]:
        return period_bounds(self.period)

    def run_dir(self) -> Path:
        return self.output_dir / self.period

    def as_dict(self) -> dict[str, Any]:
        out = {
            "period": self.period,
            "as_of": self.as_of.isoformat(),
            "input_dir": str(self.input_dir),
            "output_dir": str(self.output_dir),
            "label_maturity_days": self.label_maturity_days,
            "baseline_lookback_months": self.baseline_lookback_months,
            "expected_reviewer_count": self.expected_reviewer_count,
            "min_fires_for_evidence": self.min_fires_for_evidence,
            "stale_review_days": self.stale_review_days,
            "alert_ageing_sla_days": self.alert_ageing_sla_days,
            "audit_date": self.audit_date.isoformat() if self.audit_date else None,
            "compare_period": self.compare_period,
        }
        return out


def period_bounds(period: str) -> tuple[date, date]:
    """Return (first_day, last_day) for a ``YYYY-MM`` period, validating the format."""
    parts = period.split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        raise ValueError(f"period must be formatted YYYY-MM, got {period!r}")
    year, month = int(parts[0]), int(parts[1])
    if not 1 <= month <= 12:
        raise ValueError(f"period month out of range: {period!r}")
    first = date(year, month, 1)
    last = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return first, last


def shift_period(period: str, months: int) -> str:
    """Shift a ``YYYY-MM`` period by a (possibly negative) number of months."""
    year, month = (int(part) for part in period.split("-"))
    index = year * 12 + (month - 1) + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


# --------------------------------------------------------------------------------------
# Source loading (read-only)
# --------------------------------------------------------------------------------------


@dataclass
class SourceFile:
    """A loaded source extract plus the metadata that makes its figures traceable."""

    logical_name: str
    path: Path
    status: str                # LOADED | MISSING
    sha256: str = ""
    row_count: int = 0
    columns: list[str] = field(default_factory=list)
    required: bool = True

    def manifest_entry(self) -> dict[str, Any]:
        return {
            "logical_name": self.logical_name,
            "path": self.path.as_posix(),
            "status": self.status,
            "sha256": self.sha256,
            "row_count": self.row_count,
            "columns": self.columns,
            "required": self.required,
        }


def sha256_file(path: Path) -> str:
    """Stream a SHA-256 digest of ``path`` without loading it into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_table(
    logical_name: str,
    path: Path,
    required_columns: Sequence[str],
    required: bool = True,
) -> tuple[list[dict[str, str]], SourceFile]:
    """Read a CSV extract read-only, returning its rows and provenance metadata.

    Missing optional files return an empty row list with status ``MISSING`` so the
    manifest still records that the tool looked for them.
    """
    if not path.is_file():
        if required:
            raise SourceDataError(f"required source extract not found: {path}")
        return [], SourceFile(logical_name, path, "MISSING", required=False)

    digest = sha256_file(path)
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = list(reader.fieldnames or [])
        missing = [column for column in required_columns if column not in columns]
        if missing:
            raise SourceDataError(f"{path.name} is missing required column(s): {missing}")
        rows = [{key: (value or "").strip() for key, value in row.items() if key is not None}
                for row in reader]

    return rows, SourceFile(
        logical_name=logical_name,
        path=path,
        status="LOADED",
        sha256=digest,
        row_count=len(rows),
        columns=columns,
        required=required,
    )


@dataclass
class SourceSet:
    """All source extracts for one run, keyed by logical name."""

    rule_registry: list[dict[str, str]]
    audit_checklist: list[dict[str, str]]
    rule_control_map: list[dict[str, str]]
    loans: list[dict[str, str]]
    rule_fires: list[dict[str, str]]
    alerts: list[dict[str, str]]
    review_outcomes: list[dict[str, str]]
    fraud_labels: list[dict[str, str]]
    baseline_loans: dict[str, list[dict[str, str]]]
    files: dict[str, SourceFile]

    def digests(self) -> dict[str, str]:
        return {name: source.sha256 for name, source in self.files.items()
                if source.status == "LOADED"}


def load_sources(params: RunParams) -> SourceSet:
    """Load every extract the report needs. Opens files for reading only."""
    in_dir = params.input_dir
    period = params.period
    files: dict[str, SourceFile] = {}

    def load(name: str, filename: str, required_columns: Sequence[str], required: bool = True):
        rows, source = read_table(name, in_dir / filename, required_columns, required)
        files[name] = source
        return rows

    registry = load("rule_registry", "rule_registry.csv",
                    ["rule_id", "rule_name", "status", "risk_typology", "owner",
                     "last_reviewed_date"])
    checklist = load("audit_checklist", "audit_checklist.csv",
                     ["control_id", "control_title", "control_family",
                      "regulatory_reference", "evidence_requirement"])
    mapping = load("rule_control_map", "rule_control_map.csv", ["rule_id", "control_id"])
    loans = load("loans", f"loans_{period}.csv",
                 ["loan_id", "application_id", "disbursal_date", "principal_amount"])
    fires = load("rule_fires", f"rule_fires_{period}.csv",
                 ["fire_id", "application_id", "rule_id", "fired_at"])
    alerts = load("alerts", f"alerts_{period}.csv",
                  ["alert_id", "application_id", "rule_id", "created_at",
                   "assigned_reviewer_id"])
    reviews = load("review_outcomes", f"review_outcomes_{period}.csv",
                   ["review_id", "alert_id", "reviewer_id", "decided_at", "outcome"])
    labels = load("fraud_labels", "fraud_labels.csv",
                  ["loan_id", "label_date", "net_loss_amount"])

    # Prior cohorts, used only for the matured fraud-loss baseline. Optional: a missing
    # cohort narrows the baseline but must not break the control view.
    baseline_loans: dict[str, list[dict[str, str]]] = {}
    for offset in range(1, params.baseline_lookback_months + 1):
        cohort = shift_period(period, -offset)
        rows = load(
            f"loans_baseline_{cohort}", f"loans_{cohort}.csv",
            ["loan_id", "disbursal_date", "principal_amount"], required=False,
        )
        if rows:
            baseline_loans[cohort] = rows

    return SourceSet(
        rule_registry=registry,
        audit_checklist=checklist,
        rule_control_map=mapping,
        loans=loans,
        rule_fires=fires,
        alerts=alerts,
        review_outcomes=reviews,
        fraud_labels=labels,
        baseline_loans=baseline_loans,
        files=files,
    )


# --------------------------------------------------------------------------------------
# Output row schemas. Field order IS the CSV column order.
# --------------------------------------------------------------------------------------


@dataclass
class RuleControlRow:
    """One fraud rule in scope for the reporting period."""

    period: str
    rule_id: str
    rule_name: str
    rule_status: str
    risk_typology: str
    rule_owner: str
    last_reviewed_date: str
    days_since_rule_review: str
    control_ids: str
    control_titles: str
    control_families: str
    regulatory_references: str
    mapping_status: str
    fire_count: int
    distinct_applications_fired: int
    share_of_period_fires_pct: str
    alert_count: int
    alert_rate_pct: str
    reviews_decided: int
    outcome_confirmed_fraud: int
    outcome_cleared_false_positive: int
    outcome_insufficient_evidence: int
    outcome_pending: int
    review_coverage_pct: str
    alert_precision_pct: str
    distinct_reviewers: int
    linked_disbursed_loans: int
    linked_confirmed_fraud_loans: int
    linked_confirmed_fraud_net_loss_usd: str
    fraud_label_status: str
    rule_evidence_status: str
    source_files: str


@dataclass
class ControlCoverageRow:
    """One audit checklist control and the rule evidence standing behind it."""

    period: str
    control_id: str
    control_title: str
    control_family: str
    regulatory_reference: str
    evidence_requirement: str
    mapped_rules_total: int
    mapped_rules_active: int
    mapped_rules_non_active: int
    active_rules_with_fires: int
    fire_count: int
    alert_count: int
    reviews_decided: int
    confirmed_fraud_reviews: int
    manual_review_evidence: str
    evidence_status: str
    remediation_action: str
    source_files: str


TREND_NEWLY_EVIDENCED = "NEWLY_EVIDENCED"
TREND_REGRESSED = "REGRESSED"
TREND_STILL_EVIDENCED = "STILL_EVIDENCED"
TREND_STILL_UNEVIDENCED = "STILL_UNEVIDENCED"
TREND_NEW_CONTROL = "NEW_CONTROL"      # control seen only in the current period
TREND_RETIRED_CONTROL = "RETIRED_CONTROL"  # control seen only in the prior period


@dataclass
class ControlTrendRow:
    """One checklist control's coverage change between two reporting periods."""

    current_period: str
    prior_period: str
    control_id: str
    control_title: str
    control_family: str
    prior_status: str
    current_status: str
    change_class: str
    source_files: str


@dataclass
class UnmappedRuleRow:
    """A rule/control mapping gap that an examiner would otherwise find first."""

    period: str
    rule_id: str
    rule_name: str
    rule_status: str
    risk_typology: str
    rule_owner: str
    fire_count: int
    alert_count: int
    gap_type: str
    audit_risk: str
    recommended_action: str
    source_files: str


@dataclass
class ExceptionRow:
    """A data integrity, change control or governance exception."""

    period: str
    exception_id: str
    exception_code: str
    severity: str
    entity_type: str
    entity_id: str
    description: str
    detected_by_check: str
    source_files: str


@dataclass
class FraudLossRow:
    """Observed fraud loss for one disbursal cohort, with its label maturity."""

    cohort_period: str
    is_reporting_period: str
    loans_disbursed: int
    principal_disbursed_usd: str
    label_window_days_elapsed: int
    matured_loans: int
    cohort_maturity_pct: str
    confirmed_fraud_loans: int
    fraud_incidence_pct: str
    gross_exposure_usd: str
    recoveries_usd: str
    net_loss_usd: str
    net_loss_rate_bps: str
    label_status: str
    source_files: str


@dataclass
class SummaryRow:
    """A headline metric plus the lineage needed to defend it in an audit meeting."""

    metric_id: str
    metric_label: str
    value: str
    unit: str
    label_status: str
    source_files: str
    computation: str


@dataclass
class LineageRow:
    """Output column -> source file(s) -> source column(s) -> transform."""

    output_file: str
    output_column: str
    source_files: str
    source_columns: str
    transform: str


# --------------------------------------------------------------------------------------
# Small numeric helpers (kept explicit so every figure is reproducible by hand)
# --------------------------------------------------------------------------------------


def pct(numerator: float, denominator: float, digits: int = 2) -> str:
    """Percentage as a string; empty string when undefined (denominator 0).

    An undefined ratio is reported as blank rather than 0.0 so a reader never mistakes
    "no denominator" for "zero performance".
    """
    if not denominator:
        return ""
    return f"{round(numerator / denominator * 100, digits):.{digits}f}"


def bps(numerator: float, denominator: float, digits: int = 1) -> str:
    """Basis points of ``numerator`` over ``denominator``; blank when undefined."""
    if not denominator:
        return ""
    return f"{round(numerator / denominator * 10_000, digits):.{digits}f}"


def money(amount: float) -> str:
    return f"{amount:.2f}"


def to_float(raw: str, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def parse_date(raw: str) -> date | None:
    """Parse an ISO date or the date part of an ISO timestamp; None if unparseable."""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def in_period(raw_timestamp: str, period: str) -> bool:
    """True when an ISO date/timestamp falls inside the ``YYYY-MM`` period."""
    return raw_timestamp[:7] == period


def join_unique(values: Iterable[str]) -> str:
    """Pipe-join unique, sorted, non-empty values - deterministic by construction."""
    return " | ".join(sorted({value for value in values if value}))


# --------------------------------------------------------------------------------------
# Report assembly
# --------------------------------------------------------------------------------------


@dataclass
class Report:
    """Everything the writers need. Built once, written many ways."""

    params: RunParams
    sources: SourceSet
    rule_rows: list[RuleControlRow]
    control_rows: list[ControlCoverageRow]
    unmapped_rows: list[UnmappedRuleRow]
    exception_rows: list[ExceptionRow]
    loss_rows: list[FraudLossRow]
    summary_rows: list[SummaryRow]
    lineage_rows: list[LineageRow]
    trend_rows: list[ControlTrendRow] = field(default_factory=list)
    compare_period: str | None = None
    compare_evidenced_pct: str | None = None
    compare_usable: bool = False
    compare_note: str = ""

    def summary(self, metric_id: str) -> SummaryRow | None:
        for row in self.summary_rows:
            if row.metric_id == metric_id:
                return row
        return None

    def summary_value(self, metric_id: str, default: str = "") -> str:
        row = self.summary(metric_id)
        return row.value if row else default


def build_report(params: RunParams, sources: SourceSet) -> Report:
    """Aggregate the source extracts into the full audit-readiness report."""
    period = params.period
    exceptions: list[tuple[str, str, str, str, str, str, str]] = []

    def add_exception(code: str, severity: str, entity_type: str, entity_id: str,
                      description: str, check: str, source_files: str) -> None:
        exceptions.append((code, severity, entity_type, entity_id, description, check,
                           source_files))

    def add_aggregate_exception(code: str, severity: str, entity_type: str,
                                ids: Sequence[str], description_template: str,
                                check: str, source_files: str) -> None:
        """Emit one bounded exception row for a record-level defect class."""
        if not ids:
            return
        sample = ", ".join(sorted(ids)[:EXCEPTION_SAMPLE_SIZE])
        add_exception(
            code, severity, entity_type, f"n={len(ids)}",
            description_template.format(count=len(ids), sample=sample), check, source_files,
        )

    # ---------------------------------------------------------------- rule registry
    registry: dict[str, dict[str, str]] = {}
    duplicate_rule_ids: list[str] = []
    for row in sources.rule_registry:
        rule_id = row["rule_id"]
        if rule_id in registry:
            duplicate_rule_ids.append(rule_id)
        registry[rule_id] = row
    add_aggregate_exception(
        "E-DUPLICATE_RULE_ID", "HIGH", "rule", duplicate_rule_ids,
        "{count} duplicate rule_id value(s) in the rule registry (e.g. {sample}); "
        "registry is not uniquely keyed.",
        "registry primary-key uniqueness", "rule_registry",
    )
    active_rule_ids = {rid for rid, row in registry.items() if row["status"] == STATUS_ACTIVE}

    # ---------------------------------------------------------------- checklist & mapping
    checklist: dict[str, dict[str, str]] = {row["control_id"]: row
                                            for row in sources.audit_checklist}
    controls_by_rule: dict[str, set[str]] = defaultdict(set)
    rules_by_control: dict[str, set[str]] = defaultdict(set)
    unknown_control_refs: list[str] = []
    for row in sources.rule_control_map:
        rule_id, control_id = row["rule_id"], row["control_id"]
        if control_id not in checklist:
            unknown_control_refs.append(f"{rule_id}->{control_id}")
            continue
        controls_by_rule[rule_id].add(control_id)
        rules_by_control[control_id].add(rule_id)
    add_aggregate_exception(
        "E-MAPPING_TO_UNKNOWN_CONTROL", "HIGH", "mapping", unknown_control_refs,
        "{count} mapping row(s) reference a control_id absent from the audit checklist "
        "(e.g. {sample}); the mapping workbook and the checklist have diverged.",
        "mapping referential integrity", "rule_control_map|audit_checklist",
    )

    # ---------------------------------------------------------------- rule fires
    fires_by_rule: Counter[str] = Counter()
    fire_apps_by_rule: dict[str, set[str]] = defaultdict(set)
    fire_pairs: set[tuple[str, str]] = set()   # (application_id, rule_id) seen in the log
    fire_ids: set[str] = set()
    duplicate_fire_ids: list[str] = []
    out_of_period_fires: list[str] = []
    for row in sources.rule_fires:
        if not in_period(row["fired_at"], period):
            out_of_period_fires.append(row["fire_id"])
            continue
        if row["fire_id"] in fire_ids:
            duplicate_fire_ids.append(row["fire_id"])
            continue
        fire_ids.add(row["fire_id"])
        rule_id = row["rule_id"]
        fires_by_rule[rule_id] += 1
        fire_apps_by_rule[rule_id].add(row["application_id"])
        fire_pairs.add((row["application_id"], rule_id))
    add_aggregate_exception(
        "E-FIRE_OUTSIDE_REPORTING_PERIOD", "MEDIUM", "rule_fire", out_of_period_fires,
        "{count} fire row(s) in the period extract carry a fired_at outside {period} "
        "(e.g. {sample}); excluded from all metrics to keep the month idempotent."
        .replace("{period}", period),
        "period boundary check on fired_at", "rule_fires",
    )
    add_aggregate_exception(
        "E-DUPLICATE_FIRE_ID", "HIGH", "rule_fire", duplicate_fire_ids,
        "{count} duplicate fire_id value(s) (e.g. {sample}); duplicates dropped, "
        "fire counts would otherwise be overstated.",
        "fire log primary-key uniqueness", "rule_fires",
    )

    # ---------------------------------------------------------------- alerts
    alerts_by_rule: Counter[str] = Counter()
    alert_rule: dict[str, str] = {}
    alert_created: dict[str, str] = {}
    alert_reviewer: dict[str, str] = {}
    duplicate_alert_ids: list[str] = []
    out_of_period_alerts: list[str] = []
    alerts_without_fire: list[str] = []
    alerts_unknown_rule: list[str] = []
    for row in sources.alerts:
        alert_id = row["alert_id"]
        if not in_period(row["created_at"], period):
            out_of_period_alerts.append(alert_id)
            continue
        if alert_id in alert_rule:
            duplicate_alert_ids.append(alert_id)
            continue
        rule_id = row["rule_id"]
        alert_rule[alert_id] = rule_id
        alert_created[alert_id] = row["created_at"]
        alert_reviewer[alert_id] = row["assigned_reviewer_id"]
        alerts_by_rule[rule_id] += 1
        if rule_id not in registry:
            alerts_unknown_rule.append(alert_id)
        if (row["application_id"], rule_id) not in fire_pairs:
            alerts_without_fire.append(alert_id)
    add_aggregate_exception(
        "E-ALERT_OUTSIDE_REPORTING_PERIOD", "MEDIUM", "alert", out_of_period_alerts,
        "{count} alert row(s) carry a created_at outside the reporting period "
        "(e.g. {sample}); excluded from all metrics.",
        "period boundary check on created_at", "alerts",
    )
    add_aggregate_exception(
        "E-DUPLICATE_ALERT_ID", "HIGH", "alert", duplicate_alert_ids,
        "{count} duplicate alert_id value(s) (e.g. {sample}); duplicates dropped.",
        "alert queue primary-key uniqueness", "alerts",
    )
    add_aggregate_exception(
        "E-ALERT_RULE_NOT_IN_REGISTRY", "HIGH", "alert", alerts_unknown_rule,
        "{count} alert(s) were raised by a rule_id that does not exist in the rule "
        "registry (e.g. {sample}); the alert cannot be tied to an owned control.",
        "alert -> registry referential integrity", "alerts|rule_registry",
    )
    add_aggregate_exception(
        "E-ALERT_WITHOUT_MATCHING_FIRE", "MEDIUM", "alert", alerts_without_fire,
        "{count} alert(s) have no matching (application_id, rule_id) row in the fire log "
        "(e.g. {sample}); alert volume cannot be reconciled to engine output for these.",
        "alert -> fire log reconciliation", "alerts|rule_fires",
    )

    # ---------------------------------------------------------------- review outcomes
    as_of_iso = params.as_of.isoformat()
    outcome_by_alert: dict[str, str] = {}
    reviewers_by_alert: dict[str, str] = {}
    duplicate_review_ids: list[str] = []
    review_ids: set[str] = set()
    orphan_reviews: list[str] = []
    future_reviews: list[str] = []
    unknown_outcomes: list[str] = []
    self_assigned_conflicts: list[str] = []
    for row in sources.review_outcomes:
        review_id = row["review_id"]
        if review_id in review_ids:
            duplicate_review_ids.append(review_id)
            continue
        review_ids.add(review_id)
        alert_id = row["alert_id"]
        if alert_id not in alert_rule:
            orphan_reviews.append(review_id)
            continue
        if row["decided_at"][:10] > as_of_iso:
            # Decided after the snapshot: excluded so the month stays idempotent.
            future_reviews.append(review_id)
            continue
        outcome = row["outcome"]
        if outcome not in DECIDED_OUTCOMES:
            unknown_outcomes.append(review_id)
            continue
        if row["decided_at"][:10] < alert_created[alert_id][:10]:
            self_assigned_conflicts.append(review_id)
        outcome_by_alert[alert_id] = outcome
        reviewers_by_alert[alert_id] = row["reviewer_id"]
    add_aggregate_exception(
        "E-DUPLICATE_REVIEW_ID", "HIGH", "review", duplicate_review_ids,
        "{count} duplicate review_id value(s) (e.g. {sample}); duplicates dropped.",
        "review file primary-key uniqueness", "review_outcomes",
    )
    add_aggregate_exception(
        "E-REVIEW_WITHOUT_ALERT", "HIGH", "review", orphan_reviews,
        "{count} review disposition(s) reference an alert_id not present in the period "
        "alert queue (e.g. {sample}); the disposition has no auditable alert.",
        "review -> alert referential integrity", "review_outcomes|alerts",
    )
    add_aggregate_exception(
        "E-REVIEW_AFTER_SNAPSHOT", "INFO", "review", future_reviews,
        "{count} disposition(s) were decided after the as-of date (e.g. {sample}); "
        "excluded so a re-run of this period reproduces identical figures.",
        "as-of cutoff on decided_at", "review_outcomes",
    )
    add_aggregate_exception(
        "E-UNKNOWN_REVIEW_OUTCOME", "MEDIUM", "review", unknown_outcomes,
        "{count} disposition(s) carry an outcome outside the approved code set "
        "(e.g. {sample}); excluded from the outcome breakdown.",
        "outcome code-set validation", "review_outcomes",
    )
    add_aggregate_exception(
        "E-REVIEW_BEFORE_ALERT", "MEDIUM", "review", self_assigned_conflicts,
        "{count} disposition(s) are dated before their alert was created "
        "(e.g. {sample}); timestamp integrity issue in case management.",
        "decided_at >= created_at sequencing", "review_outcomes|alerts",
    )

    # Per-rule outcome tallies, derived from the alert -> outcome join.
    outcomes_by_rule: dict[str, Counter[str]] = defaultdict(Counter)
    reviewers_by_rule: dict[str, set[str]] = defaultdict(set)
    pending_by_rule: Counter[str] = Counter()
    pending_over_sla: list[str] = []
    for alert_id, rule_id in alert_rule.items():
        outcome = outcome_by_alert.get(alert_id)
        if outcome is None:
            pending_by_rule[rule_id] += 1
            created = parse_date(alert_created[alert_id])
            if created and (params.as_of - created).days > params.alert_ageing_sla_days:
                pending_over_sla.append(alert_id)
            continue
        outcomes_by_rule[rule_id][outcome] += 1
        reviewers_by_rule[rule_id].add(reviewers_by_alert[alert_id])
    add_aggregate_exception(
        "E-ALERT_PENDING_BEYOND_SLA", "MEDIUM", "alert", pending_over_sla,
        f"{{count}} alert(s) were still undispositioned more than "
        f"{params.alert_ageing_sla_days} days after creation as at the as-of date "
        "(e.g. {sample}); review queue ageing control is not operating.",
        "pending alert ageing vs SLA", "alerts|review_outcomes",
    )

    # Reviewer roster drift vs the documented six-person team.
    active_reviewers = {reviewer for reviewer in reviewers_by_alert.values() if reviewer}
    if active_reviewers and len(active_reviewers) != params.expected_reviewer_count:
        add_exception(
            "E-REVIEWER_ROSTER_DRIFT", "MEDIUM", "review_team",
            f"n={len(active_reviewers)}",
            f"{len(active_reviewers)} distinct reviewer(s) dispositioned alerts in "
            f"{period}, but the control narrative documents "
            f"{params.expected_reviewer_count}; roster evidence needs refreshing.",
            "distinct reviewer count vs documented team size", "review_outcomes",
        )

    # ---------------------------------------------------------------- loans and labels
    loans_by_id: dict[str, dict[str, str]] = {}
    loan_by_application: dict[str, str] = {}
    duplicate_loan_ids: list[str] = []
    out_of_period_loans: list[str] = []
    period_principal = 0.0
    matured_loans = 0
    for row in sources.loans:
        loan_id = row["loan_id"]
        if not in_period(row["disbursal_date"], period):
            out_of_period_loans.append(loan_id)
            continue
        if loan_id in loans_by_id:
            duplicate_loan_ids.append(loan_id)
            continue
        loans_by_id[loan_id] = row
        loan_by_application[row["application_id"]] = loan_id
        period_principal += to_float(row["principal_amount"])
        disbursal = parse_date(row["disbursal_date"])
        if disbursal and (params.as_of - disbursal).days >= params.label_maturity_days:
            matured_loans += 1
    add_aggregate_exception(
        "E-LOAN_OUTSIDE_REPORTING_PERIOD", "MEDIUM", "loan", out_of_period_loans,
        "{count} loan(s) in the period extract were disbursed outside the reporting "
        "period (e.g. {sample}); excluded from period metrics.",
        "period boundary check on disbursal_date", "loans",
    )
    add_aggregate_exception(
        "E-DUPLICATE_LOAN_ID", "HIGH", "loan", duplicate_loan_ids,
        "{count} duplicate loan_id value(s) (e.g. {sample}); duplicates dropped.",
        "loan ledger primary-key uniqueness", "loans",
    )

    # Labels observable at the as-of date, indexed by loan.
    labels_by_loan: dict[str, dict[str, str]] = {}
    labels_unknown_loan: list[str] = []
    all_period_loan_ids = set(loans_by_id)
    baseline_loan_ids = {loan_id
                         for rows in sources.baseline_loans.values()
                         for loan_id in (row["loan_id"] for row in rows)}
    for row in sources.fraud_labels:
        if row["label_date"][:10] > as_of_iso:
            continue  # not yet observable; excluded for idempotency
        loan_id = row["loan_id"]
        labels_by_loan[loan_id] = row
        if loan_id not in all_period_loan_ids and loan_id not in baseline_loan_ids:
            labels_unknown_loan.append(loan_id)
    add_aggregate_exception(
        "E-LABEL_LOAN_NOT_IN_LEDGER", "HIGH", "fraud_label", labels_unknown_loan,
        "{count} confirmed-fraud label(s) reference a loan_id absent from every loaded "
        "loan ledger (e.g. {sample}); loss figures cannot be tied to a disbursal.",
        "label -> loan ledger referential integrity", "fraud_labels|loans",
    )

    period_fraud_loans = [loan_id for loan_id in loans_by_id if loan_id in labels_by_loan]
    period_net_loss = sum(to_float(labels_by_loan[loan_id]["net_loss_amount"])
                          for loan_id in period_fraud_loans)

    cohort_maturity = pct(matured_loans, len(loans_by_id))
    period_label_status = (
        LABEL_FINAL if loans_by_id and matured_loans == len(loans_by_id) else LABEL_PROVISIONAL
    )
    if period_label_status == LABEL_PROVISIONAL:
        add_exception(
            "E-LABEL_COHORT_IMMATURE", "INFO", "cohort", period,
            f"Only {cohort_maturity or '0.00'}% of {period} disbursals have reached the "
            f"{params.label_maturity_days}-day confirmed-fraud labelling window as at "
            f"{as_of_iso}. All fraud-outcome and loss figures for this period are "
            f"PROVISIONAL and will move; the FINAL baseline is taken from the most "
            f"recent fully matured cohort.",
            "cohort label maturity vs label_maturity_days", "loans|fraud_labels",
        )

    # ---------------------------------------------------------------- per-rule rows
    rules_in_scope = sorted(active_rule_ids | set(fires_by_rule) | set(alerts_by_rule))
    total_fires = sum(fires_by_rule.values())

    rule_rows: list[RuleControlRow] = []
    for rule_id in rules_in_scope:
        registry_row = registry.get(rule_id)
        status = registry_row["status"] if registry_row else "NOT_IN_REGISTRY"
        control_ids = sorted(controls_by_rule.get(rule_id, set()))

        if registry_row is None:
            mapping_status = MAP_UNREGISTERED
        elif control_ids:
            mapping_status = MAP_MAPPED
        elif status == STATUS_ACTIVE:
            mapping_status = MAP_UNMAPPED_ACTIVE
        else:
            mapping_status = MAP_UNMAPPED_NON_ACTIVE

        fire_count = fires_by_rule.get(rule_id, 0)
        alert_count = alerts_by_rule.get(rule_id, 0)
        tally = outcomes_by_rule.get(rule_id, Counter())
        confirmed = tally[OUTCOME_CONFIRMED]
        cleared = tally[OUTCOME_CLEARED]
        insufficient = tally[OUTCOME_INSUFFICIENT]
        decided = confirmed + cleared + insufficient

        # Attribute disbursed loans and confirmed fraud to the rule that fired on the
        # application. Attribution is NON-EXCLUSIVE: several rules fire on one
        # application, so these columns must never be summed across rules.
        fired_apps = fire_apps_by_rule.get(rule_id, set())
        linked_loans = [loan_by_application[app] for app in fired_apps
                        if app in loan_by_application]
        linked_fraud = [loan_id for loan_id in linked_loans if loan_id in labels_by_loan]
        linked_loss = sum(to_float(labels_by_loan[loan_id]["net_loss_amount"])
                          for loan_id in linked_fraud)

        if fire_count >= params.min_fires_for_evidence and status == STATUS_ACTIVE:
            rule_evidence = EV_EVIDENCED
        elif status == STATUS_ACTIVE:
            rule_evidence = EV_NO_ACTIVITY
        else:
            rule_evidence = LABEL_NOT_APPLICABLE

        last_reviewed = registry_row["last_reviewed_date"] if registry_row else ""
        reviewed_on = parse_date(last_reviewed)
        days_since_review = str((params.as_of - reviewed_on).days) if reviewed_on else ""

        rule_rows.append(RuleControlRow(
            period=period,
            rule_id=rule_id,
            rule_name=registry_row["rule_name"] if registry_row else "",
            rule_status=status,
            risk_typology=registry_row["risk_typology"] if registry_row else "",
            rule_owner=registry_row["owner"] if registry_row else "",
            last_reviewed_date=last_reviewed,
            days_since_rule_review=days_since_review,
            control_ids=" | ".join(control_ids),
            control_titles=" | ".join(checklist[cid]["control_title"] for cid in control_ids),
            control_families=join_unique(checklist[cid]["control_family"] for cid in control_ids),
            regulatory_references=join_unique(
                checklist[cid]["regulatory_reference"] for cid in control_ids),
            mapping_status=mapping_status,
            fire_count=fire_count,
            distinct_applications_fired=len(fired_apps),
            share_of_period_fires_pct=pct(fire_count, total_fires),
            alert_count=alert_count,
            alert_rate_pct=pct(alert_count, fire_count),
            reviews_decided=decided,
            outcome_confirmed_fraud=confirmed,
            outcome_cleared_false_positive=cleared,
            outcome_insufficient_evidence=insufficient,
            outcome_pending=pending_by_rule.get(rule_id, 0),
            review_coverage_pct=pct(decided, alert_count),
            alert_precision_pct=pct(confirmed, confirmed + cleared),
            distinct_reviewers=len(reviewers_by_rule.get(rule_id, set())),
            linked_disbursed_loans=len(linked_loans),
            linked_confirmed_fraud_loans=len(linked_fraud),
            linked_confirmed_fraud_net_loss_usd=money(linked_loss),
            fraud_label_status=period_label_status,
            rule_evidence_status=rule_evidence,
            source_files="rule_registry|rule_control_map|audit_checklist|rule_fires|alerts|"
                         "review_outcomes|loans|fraud_labels",
        ))

    # Deterministic ordering: busiest rules first, rule_id breaks ties.
    rule_rows.sort(key=lambda row: (-row.fire_count, row.rule_id))
    rule_row_by_id = {row.rule_id: row for row in rule_rows}

    # ---------------------------------------------------------------- governance checks
    for rule_id in sorted(active_rule_ids):
        registry_row = registry[rule_id]
        reviewed_on = parse_date(registry_row["last_reviewed_date"])
        if reviewed_on and (params.as_of - reviewed_on).days > params.stale_review_days:
            add_exception(
                "E-STALE_RULE_GOVERNANCE_REVIEW", "MEDIUM", "rule", rule_id,
                f"Active rule {rule_id} was last reviewed on "
                f"{registry_row['last_reviewed_date']} "
                f"({(params.as_of - reviewed_on).days} days before the as-of date), "
                f"exceeding the {params.stale_review_days}-day annual review expectation.",
                "active rule last_reviewed_date vs stale_review_days", "rule_registry",
            )
        elif reviewed_on is None:
            add_exception(
                "E-MISSING_RULE_REVIEW_DATE", "MEDIUM", "rule", rule_id,
                f"Active rule {rule_id} has no parseable last_reviewed_date; annual "
                f"review evidence is unavailable.",
                "active rule last_reviewed_date parse", "rule_registry",
            )

    dormant_active = [row.rule_id for row in rule_rows
                      if row.rule_status == STATUS_ACTIVE and row.fire_count == 0]
    add_aggregate_exception(
        "E-ACTIVE_RULE_NO_FIRES", "LOW", "rule", dormant_active,
        "{count} rule(s) are flagged ACTIVE in the registry but produced no fires in "
        "the reporting period (e.g. {sample}); either dormant by design or decommission "
        "candidates. They cannot evidence a control this month.",
        "active rule fire count == 0", "rule_registry|rule_fires",
    )

    for row in rule_rows:
        if row.mapping_status == MAP_UNREGISTERED:
            add_exception(
                "E-UNREGISTERED_RULE_FIRING", "CRITICAL", "rule", row.rule_id,
                f"Rule {row.rule_id} produced {row.fire_count} fire(s) and "
                f"{row.alert_count} alert(s) in {period} but does not exist in the rule "
                f"registry; production logic is operating outside the documented "
                f"inventory.",
                "fire log rule_id not in registry", "rule_fires|rule_registry",
            )
        elif row.rule_status != STATUS_ACTIVE and row.fire_count > 0:
            severity = "HIGH" if row.rule_status == "RETIRED" else "MEDIUM"
            add_exception(
                "E-NON_ACTIVE_RULE_FIRING", severity, "rule", row.rule_id,
                f"Rule {row.rule_id} has registry status {row.rule_status} yet produced "
                f"{row.fire_count} fire(s) and {row.alert_count} alert(s) in {period}; "
                f"change control between the registry and the engine is not effective.",
                "non-active registry status with fires > 0", "rule_registry|rule_fires",
            )

    # ---------------------------------------------------------------- unmapped rules
    unmapped_rows: list[UnmappedRuleRow] = []
    for row in rule_rows:
        if row.mapping_status == MAP_MAPPED:
            continue
        if row.mapping_status == MAP_UNMAPPED_NON_ACTIVE and row.fire_count == 0:
            continue  # non-active, silent, unmapped: not an audit finding
        if row.mapping_status == MAP_UNMAPPED_ACTIVE:
            gap_type, risk = MAP_UNMAPPED_ACTIVE, "HIGH"
            action = ("Map the rule to a checklist control, or document it as "
                      "non-control monitoring logic, before the audit.")
        elif row.mapping_status == MAP_UNREGISTERED:
            gap_type, risk = "UNREGISTERED_RULE_FIRING", "CRITICAL"
            action = ("Identify the owner, add the rule to the registry (or disable it) "
                      "and map it to a control; unregistered production logic is a "
                      "change-control finding.")
        else:
            gap_type, risk = "NON_ACTIVE_RULE_FIRING", "HIGH"
            action = ("Remove the rule from the production decision path or restore it "
                      "to ACTIVE with a mapped control.")
        unmapped_rows.append(UnmappedRuleRow(
            period=period,
            rule_id=row.rule_id,
            rule_name=row.rule_name,
            rule_status=row.rule_status,
            risk_typology=row.risk_typology,
            rule_owner=row.rule_owner,
            fire_count=row.fire_count,
            alert_count=row.alert_count,
            gap_type=gap_type,
            audit_risk=risk,
            recommended_action=action,
            source_files="rule_registry|rule_control_map|rule_fires|alerts",
        ))
        if row.mapping_status == MAP_UNMAPPED_ACTIVE:
            add_exception(
                "E-UNMAPPED_ACTIVE_RULE", "HIGH", "rule", row.rule_id,
                f"Active rule {row.rule_id} ({row.risk_typology or 'typology unknown'}) "
                f"fired {row.fire_count} time(s) in {period} but is not mapped to any "
                f"audit checklist control; its output is unevidenced.",
                "active rule absent from rule_control_map", "rule_registry|rule_control_map",
            )
    unmapped_rows.sort(key=lambda row: (SEVERITY_ORDER[row.audit_risk], -row.fire_count,
                                        row.rule_id))

    # ---------------------------------------------------------------- control coverage
    control_rows: list[ControlCoverageRow] = []
    for control_id in sorted(checklist):
        control = checklist[control_id]
        mapped = sorted(rules_by_control.get(control_id, set()))
        mapped_active = [rid for rid in mapped if rid in active_rule_ids]
        mapped_non_active = [rid for rid in mapped if rid not in active_rule_ids]
        contributing = [rule_row_by_id[rid] for rid in mapped_active if rid in rule_row_by_id]
        with_fires = [row for row in contributing
                      if row.fire_count >= params.min_fires_for_evidence]

        fire_count = sum(row.fire_count for row in contributing)
        alert_count = sum(row.alert_count for row in contributing)
        decided = sum(row.reviews_decided for row in contributing)
        confirmed = sum(row.outcome_confirmed_fraud for row in contributing)

        if with_fires:
            status = EV_EVIDENCED
            action = ""
        elif mapped_active:
            status = EV_NO_ACTIVITY
            action = ("Mapped active rule(s) produced no fires this month. Confirm the "
                      "rule is deployed and expected to fire, or evidence the control "
                      "another way.")
        elif mapped:
            status = EV_NO_ACTIVE_RULE
            action = (f"Control is mapped only to non-active rule(s) "
                      f"({', '.join(mapped_non_active)}). Refresh the mapping workbook "
                      f"to a live rule.")
        else:
            status = EV_UNMAPPED
            action = ("No rule is mapped to this control. Identify the rule(s) that "
                      "operate it, or evidence it outside the rules engine.")

        for rule_id in mapped_non_active:
            add_exception(
                "E-MAPPING_TO_NON_ACTIVE_RULE", "MEDIUM", "mapping",
                f"{rule_id}->{control_id}",
                f"Control {control_id} is mapped to rule {rule_id}, whose registry status "
                f"is {registry.get(rule_id, {}).get('status', 'NOT_IN_REGISTRY')}; the "
                f"mapping workbook is stale.",
                "mapped rule registry status != ACTIVE",
                "rule_control_map|rule_registry",
            )
        if status != EV_EVIDENCED:
            add_exception(
                "E-CONTROL_NOT_EVIDENCED",
                "HIGH" if status in (EV_UNMAPPED, EV_NO_ACTIVE_RULE) else "MEDIUM",
                "control", control_id,
                f"Control {control_id} ({control['control_title']}) is {status} for "
                f"{period}. Required evidence: {control['evidence_requirement']}.",
                "control evidence status != EVIDENCED",
                "audit_checklist|rule_control_map|rule_registry|rule_fires",
            )

        control_rows.append(ControlCoverageRow(
            period=period,
            control_id=control_id,
            control_title=control["control_title"],
            control_family=control["control_family"],
            regulatory_reference=control["regulatory_reference"],
            evidence_requirement=control["evidence_requirement"],
            mapped_rules_total=len(mapped),
            mapped_rules_active=len(mapped_active),
            mapped_rules_non_active=len(mapped_non_active),
            active_rules_with_fires=len(with_fires),
            fire_count=fire_count,
            alert_count=alert_count,
            reviews_decided=decided,
            confirmed_fraud_reviews=confirmed,
            manual_review_evidence="Y" if decided > 0 else "N",
            evidence_status=status,
            remediation_action=action,
            source_files="audit_checklist|rule_control_map|rule_registry|rule_fires|alerts|"
                         "review_outcomes",
        ))

    # ---------------------------------------------------------------- fraud loss baseline
    loss_rows = build_loss_rows(params, sources, labels_by_loan, loans_by_id)

    # ---------------------------------------------------------------- exceptions
    exception_rows: list[ExceptionRow] = []
    for index, (code, severity, entity_type, entity_id, description, check, source_files) in \
            enumerate(sorted(exceptions,
                             key=lambda item: (SEVERITY_ORDER[item[1]], item[0], item[3])), 1):
        exception_rows.append(ExceptionRow(
            period=period,
            exception_id=f"EX-{index:04d}",
            exception_code=code,
            severity=severity,
            entity_type=entity_type,
            entity_id=entity_id,
            description=description,
            detected_by_check=check,
            source_files=source_files,
        ))

    # ---------------------------------------------------------------- summary
    summary_rows = build_summary_rows(
        params=params,
        sources=sources,
        rule_rows=rule_rows,
        control_rows=control_rows,
        unmapped_rows=unmapped_rows,
        exception_rows=exception_rows,
        loss_rows=loss_rows,
        registry=registry,
        active_rule_ids=active_rule_ids,
        controls_by_rule=controls_by_rule,
        period_loans=len(loans_by_id),
        period_principal=period_principal,
        matured_loans=matured_loans,
        period_fraud_loans=len(period_fraud_loans),
        period_net_loss=period_net_loss,
        period_label_status=period_label_status,
        active_reviewers=len(active_reviewers),
    )

    return Report(
        params=params,
        sources=sources,
        rule_rows=rule_rows,
        control_rows=control_rows,
        unmapped_rows=unmapped_rows,
        exception_rows=exception_rows,
        loss_rows=loss_rows,
        summary_rows=summary_rows,
        lineage_rows=build_lineage_rows(),
    )


def build_loss_rows(
    params: RunParams,
    sources: SourceSet,
    labels_by_loan: dict[str, dict[str, str]],
    period_loans: dict[str, dict[str, str]],
) -> list[FraudLossRow]:
    """Per-cohort observed fraud loss, oldest cohort first.

    Loss is *observed to date*, never projected: a cohort's figures are FINAL only once
    every loan in it has passed the labelling window.
    """
    cohorts: list[tuple[str, list[dict[str, str]]]] = sorted(
        list(sources.baseline_loans.items()) + [(params.period, list(period_loans.values()))]
    )

    rows: list[FraudLossRow] = []
    for cohort_period, loans in cohorts:
        _, cohort_end = period_bounds(cohort_period)
        matured = 0
        principal = 0.0
        fraud_loans = 0
        gross = recoveries = net = 0.0
        for loan in loans:
            principal += to_float(loan["principal_amount"])
            disbursal = parse_date(loan["disbursal_date"])
            if disbursal and (params.as_of - disbursal).days >= params.label_maturity_days:
                matured += 1
            label = labels_by_loan.get(loan["loan_id"])
            if label is None:
                continue
            fraud_loans += 1
            gross += to_float(label.get("gross_exposure_amount", ""))
            recoveries += to_float(label.get("recovery_amount", ""))
            net += to_float(label["net_loss_amount"])

        rows.append(FraudLossRow(
            cohort_period=cohort_period,
            is_reporting_period="Y" if cohort_period == params.period else "N",
            loans_disbursed=len(loans),
            principal_disbursed_usd=money(principal),
            label_window_days_elapsed=(params.as_of - cohort_end).days,
            matured_loans=matured,
            cohort_maturity_pct=pct(matured, len(loans)),
            confirmed_fraud_loans=fraud_loans,
            fraud_incidence_pct=pct(fraud_loans, len(loans), 3),
            gross_exposure_usd=money(gross),
            recoveries_usd=money(recoveries),
            net_loss_usd=money(net),
            net_loss_rate_bps=bps(net, principal),
            label_status=LABEL_FINAL if loans and matured == len(loans) else LABEL_PROVISIONAL,
            source_files="loans|fraud_labels",
        ))
    return rows


def build_summary_rows(
    *,
    params: RunParams,
    sources: SourceSet,
    rule_rows: list[RuleControlRow],
    control_rows: list[ControlCoverageRow],
    unmapped_rows: list[UnmappedRuleRow],
    exception_rows: list[ExceptionRow],
    loss_rows: list[FraudLossRow],
    registry: dict[str, dict[str, str]],
    active_rule_ids: set[str],
    controls_by_rule: dict[str, set[str]],
    period_loans: int,
    period_principal: float,
    matured_loans: int,
    period_fraud_loans: int,
    period_net_loss: float,
    period_label_status: str,
    active_reviewers: int,
) -> list[SummaryRow]:
    """Headline metrics. Each row states its own sources and computation."""
    rows: list[SummaryRow] = []

    def add(metric_id: str, label: str, value: Any, unit: str, label_status: str,
            source_files: str, computation: str) -> None:
        rows.append(SummaryRow(metric_id, label, str(value), unit, label_status,
                               source_files, computation))

    controls_total = len(control_rows)
    controls_evidenced = sum(1 for row in control_rows
                             if row.evidence_status == EV_EVIDENCED)
    active_rows = [row for row in rule_rows if row.rule_status == STATUS_ACTIVE]
    active_with_activity = [row for row in active_rows if row.fire_count > 0]
    active_mapped = [row for row in active_rows if row.mapping_status == MAP_MAPPED]
    total_fires = sum(row.fire_count for row in rule_rows)
    total_alerts = sum(row.alert_count for row in rule_rows)
    total_decided = sum(row.reviews_decided for row in rule_rows)
    total_pending = sum(row.outcome_pending for row in rule_rows)
    total_confirmed = sum(row.outcome_confirmed_fraud for row in rule_rows)
    total_cleared = sum(row.outcome_cleared_false_positive for row in rule_rows)
    total_insufficient = sum(row.outcome_insufficient_evidence for row in rule_rows)

    add("reporting_period", "Reporting period", params.period, "YYYY-MM",
        LABEL_NOT_APPLICABLE, "run_parameter", "--period argument")
    add("as_of_date", "Snapshot as-of date", params.as_of.isoformat(), "date",
        LABEL_NOT_APPLICABLE, "run_parameter",
        "--as-of argument; nothing dated after this is included")
    if params.audit_date:
        add("days_to_audit", "Days from as-of date to audit",
            (params.audit_date - params.as_of).days, "days", LABEL_NOT_APPLICABLE,
            "run_parameter", "audit_date - as_of")

    # ---- control coverage (the headline)
    add("controls_total", "Audit checklist controls in scope", controls_total, "count",
        LABEL_NOT_APPLICABLE, "audit_checklist", "count of distinct control_id")
    add("controls_evidenced", "Controls evidenced by rule activity", controls_evidenced,
        "count", LABEL_NOT_APPLICABLE,
        "audit_checklist|rule_control_map|rule_registry|rule_fires",
        f"controls with >= 1 mapped ACTIVE rule with fire_count >= "
        f"{params.min_fires_for_evidence} in the period")
    add("controls_evidenced_pct", "Controls evidenced", pct(controls_evidenced, controls_total),
        "percent", LABEL_NOT_APPLICABLE,
        "audit_checklist|rule_control_map|rule_registry|rule_fires",
        "controls_evidenced / controls_total * 100")
    for status, metric in (
        (EV_NO_ACTIVITY, "controls_not_evidenced_no_activity"),
        (EV_NO_ACTIVE_RULE, "controls_not_evidenced_no_active_rule"),
        (EV_UNMAPPED, "controls_not_evidenced_unmapped"),
    ):
        add(metric, f"Controls: {status}",
            sum(1 for row in control_rows if row.evidence_status == status), "count",
            LABEL_NOT_APPLICABLE, "control_coverage.csv",
            f"count of control_coverage rows with evidence_status = {status}")

    # ---- rule inventory
    add("rules_in_registry_total", "Rules in legacy registry", len(registry), "count",
        LABEL_NOT_APPLICABLE, "rule_registry", "count of registry rows")
    add("active_rules_registered", "Rules flagged ACTIVE in registry", len(active_rule_ids),
        "count", LABEL_NOT_APPLICABLE, "rule_registry", "count where status = ACTIVE")
    add("active_rules_with_activity", "Active rules that fired in period",
        len(active_with_activity), "count", LABEL_NOT_APPLICABLE,
        "rule_registry|rule_fires", "active rules with fire_count > 0")
    add("active_rules_dormant", "Active rules with no fires in period",
        len(active_rows) - len(active_with_activity), "count", LABEL_NOT_APPLICABLE,
        "rule_registry|rule_fires", "active rules with fire_count = 0")
    add("active_rules_mapped", "Active rules mapped to a control", len(active_mapped),
        "count", LABEL_NOT_APPLICABLE, "rule_registry|rule_control_map",
        "active rules present in rule_control_map with a known control_id")
    add("active_rules_unmapped", "Active rules NOT mapped to a control",
        len(active_rows) - len(active_mapped), "count", LABEL_NOT_APPLICABLE,
        "rule_registry|rule_control_map", "active rules absent from rule_control_map")
    add("active_rules_mapped_pct", "Active rules mapped",
        pct(len(active_mapped), len(active_rows)), "percent", LABEL_NOT_APPLICABLE,
        "rule_registry|rule_control_map", "active_rules_mapped / active_rules_registered * 100")
    add("non_active_rules_firing", "Non-active registry rules found firing",
        sum(1 for row in rule_rows
            if row.rule_status not in (STATUS_ACTIVE, "NOT_IN_REGISTRY") and row.fire_count > 0),
        "count", LABEL_NOT_APPLICABLE, "rule_registry|rule_fires",
        "rules with status in (RETIRED, SHADOW, ...) and fire_count > 0")
    add("unregistered_rules_firing", "Unregistered rules found firing",
        sum(1 for row in rule_rows if row.rule_status == "NOT_IN_REGISTRY"), "count",
        LABEL_NOT_APPLICABLE, "rule_fires|rule_registry",
        "distinct fire-log rule_id values absent from the registry")

    # ---- volumes
    add("loans_disbursed", "Loans disbursed in period", period_loans, "count",
        LABEL_NOT_APPLICABLE, "loans", "count of loans with disbursal_date in period")
    add("principal_disbursed_usd", "Principal disbursed", money(period_principal), "USD",
        LABEL_NOT_APPLICABLE, "loans", "sum of principal_amount for period loans")
    add("rule_fires_total", "Rule fires in period", total_fires, "count",
        LABEL_NOT_APPLICABLE, "rule_fires",
        "count of de-duplicated fire rows with fired_at in period")
    add("alerts_total", "Alerts raised in period", total_alerts, "count",
        LABEL_NOT_APPLICABLE, "alerts",
        "count of de-duplicated alert rows with created_at in period")
    add("alerts_per_1000_loans", "Alerts per 1,000 disbursed loans",
        f"{round(total_alerts / period_loans * 1000, 2):.2f}" if period_loans else "",
        "ratio", LABEL_NOT_APPLICABLE, "alerts|loans",
        "alerts_total / loans_disbursed * 1000")
    add("alert_rate_of_fires_pct", "Fires that raised an alert",
        pct(total_alerts, total_fires), "percent", LABEL_NOT_APPLICABLE,
        "alerts|rule_fires", "alerts_total / rule_fires_total * 100")

    # ---- manual review
    add("alerts_dispositioned", "Alerts dispositioned by as-of date", total_decided, "count",
        LABEL_NOT_APPLICABLE, "review_outcomes|alerts",
        "alerts with an approved outcome code and decided_at <= as_of")
    add("alerts_pending", "Alerts still pending at as-of date", total_pending, "count",
        LABEL_NOT_APPLICABLE, "alerts|review_outcomes",
        "period alerts with no approved disposition on or before as_of")
    add("review_coverage_pct", "Alert review coverage", pct(total_decided, total_alerts),
        "percent", LABEL_NOT_APPLICABLE, "review_outcomes|alerts",
        "alerts_dispositioned / alerts_total * 100")
    add("reviews_confirmed_fraud", "Dispositions: confirmed fraud", total_confirmed, "count",
        LABEL_PROVISIONAL, "review_outcomes", f"count of outcome = {OUTCOME_CONFIRMED}")
    add("reviews_cleared_false_positive", "Dispositions: cleared / false positive",
        total_cleared, "count", LABEL_PROVISIONAL, "review_outcomes",
        f"count of outcome = {OUTCOME_CLEARED}")
    add("reviews_insufficient_evidence", "Dispositions: insufficient evidence",
        total_insufficient, "count", LABEL_PROVISIONAL, "review_outcomes",
        f"count of outcome = {OUTCOME_INSUFFICIENT}")
    add("portfolio_alert_precision_pct", "Alert precision (confirmed / decided ex-inconclusive)",
        pct(total_confirmed, total_confirmed + total_cleared), "percent", LABEL_PROVISIONAL,
        "review_outcomes",
        f"{OUTCOME_CONFIRMED} / ({OUTCOME_CONFIRMED} + {OUTCOME_CLEARED}) * 100; "
        f"{OUTCOME_INSUFFICIENT} and pending excluded from the denominator")
    add("distinct_reviewers", "Distinct reviewers dispositioning alerts", active_reviewers,
        "count", LABEL_NOT_APPLICABLE, "review_outcomes",
        "count of distinct reviewer_id on included dispositions")
    add("expected_reviewers", "Documented review team size", params.expected_reviewer_count,
        "count", LABEL_NOT_APPLICABLE, "run_parameter", "--expected-reviewer-count argument")

    # ---- fraud loss baseline
    add("label_maturity_days", "Confirmed-fraud labelling window",
        params.label_maturity_days, "days", LABEL_NOT_APPLICABLE, "run_parameter",
        "--label-maturity-days argument (confirmed fraud lands 60-90 days post disbursal)")
    add("reporting_period_label_maturity_pct", "Reporting period cohort label maturity",
        pct(matured_loans, period_loans), "percent", LABEL_NOT_APPLICABLE, "loans",
        "period loans with (as_of - disbursal_date) >= label_maturity_days / loans_disbursed"
        " * 100")
    add("reporting_period_confirmed_fraud_loans",
        "Reporting period confirmed-fraud loans (observed to date)", period_fraud_loans,
        "count", period_label_status, "loans|fraud_labels",
        "period loans with a fraud label whose label_date <= as_of")
    add("reporting_period_net_loss_usd", "Reporting period net fraud loss (observed to date)",
        money(period_net_loss), "USD", period_label_status, "loans|fraud_labels",
        "sum of net_loss_amount for labelled period loans")
    add("reporting_period_net_loss_rate_bps",
        "Reporting period net fraud loss rate (observed to date)",
        bps(period_net_loss, period_principal), "bps", period_label_status,
        "loans|fraud_labels", "net_loss_usd / principal_disbursed_usd * 10000")

    matured_cohorts = [row for row in loss_rows if row.label_status == LABEL_FINAL]
    baseline = matured_cohorts[-1] if matured_cohorts else None
    add("fraud_loss_baseline_period", "Fraud loss baseline cohort",
        baseline.cohort_period if baseline else "", "YYYY-MM",
        LABEL_FINAL if baseline else LABEL_NOT_APPLICABLE, "loans|fraud_labels",
        "most recent loaded cohort where every loan has passed the labelling window")
    add("fraud_loss_baseline_rate_bps", "Fraud loss baseline (net loss rate)",
        baseline.net_loss_rate_bps if baseline else "", "bps",
        LABEL_FINAL if baseline else LABEL_NOT_APPLICABLE, "loans|fraud_labels",
        "net_loss_usd / principal_disbursed_usd * 10000 for the baseline cohort")
    add("fraud_loss_baseline_net_loss_usd", "Fraud loss baseline (net loss)",
        baseline.net_loss_usd if baseline else "", "USD",
        LABEL_FINAL if baseline else LABEL_NOT_APPLICABLE, "loans|fraud_labels",
        "sum of net_loss_amount for the baseline cohort")
    add("fraud_loss_baseline_incidence_pct", "Fraud loss baseline (incidence)",
        baseline.fraud_incidence_pct if baseline else "", "percent",
        LABEL_FINAL if baseline else LABEL_NOT_APPLICABLE, "loans|fraud_labels",
        "confirmed_fraud_loans / loans_disbursed * 100 for the baseline cohort")

    # ---- exceptions
    severity_counts = Counter(row.severity for row in exception_rows)
    add("exceptions_total", "Integrity & governance exceptions", len(exception_rows), "count",
        LABEL_NOT_APPLICABLE, "integrity_exceptions.csv", "count of exception rows")
    for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"):
        add(f"exceptions_{severity.lower()}", f"Exceptions: {severity}",
            severity_counts.get(severity, 0), "count", LABEL_NOT_APPLICABLE,
            "integrity_exceptions.csv", f"count of exception rows with severity = {severity}")
    add("unmapped_rule_gaps", "Rule/control mapping gaps reported", len(unmapped_rows),
        "count", LABEL_NOT_APPLICABLE, "unmapped_rules.csv", "count of unmapped_rules rows")

    return rows


def build_lineage_rows() -> list[LineageRow]:
    """Column-level lineage for every output file.

    Kept adjacent to the schemas it documents. ``test_fraud_control_view.py`` asserts
    that this table covers every emitted column, so it cannot silently drift.
    """
    rows: list[LineageRow] = []

    def add(output_file: str, column: str, source_files: str, source_columns: str,
            transform: str) -> None:
        rows.append(LineageRow(output_file, column, source_files, source_columns, transform))

    cv = "control_view.csv"
    add(cv, "period", "run_parameter", "--period", "Reporting month, echoed on every row")
    add(cv, "rule_id", "rule_fires|alerts|rule_registry", "rule_id",
        "Union of ACTIVE registry rule_ids and rule_ids observed firing or alerting")
    add(cv, "rule_name", "rule_registry", "rule_name", "Direct lookup by rule_id")
    add(cv, "rule_status", "rule_registry", "status",
        "Direct lookup; NOT_IN_REGISTRY when the rule_id is absent from the registry")
    add(cv, "risk_typology", "rule_registry", "risk_typology", "Direct lookup by rule_id")
    add(cv, "rule_owner", "rule_registry", "owner", "Direct lookup by rule_id")
    add(cv, "last_reviewed_date", "rule_registry", "last_reviewed_date",
        "Direct lookup by rule_id")
    add(cv, "days_since_rule_review", "rule_registry|run_parameter",
        "last_reviewed_date, --as-of", "as_of - last_reviewed_date, in days")
    add(cv, "control_ids", "rule_control_map|audit_checklist", "control_id",
        "Sorted, pipe-joined control_ids mapped to the rule and present in the checklist")
    add(cv, "control_titles", "audit_checklist", "control_title",
        "Titles of control_ids, in the same order as control_ids")
    add(cv, "control_families", "audit_checklist", "control_family",
        "Unique sorted families of the mapped controls")
    add(cv, "regulatory_references", "audit_checklist", "regulatory_reference",
        "Unique sorted regulatory references of the mapped controls")
    add(cv, "mapping_status", "rule_registry|rule_control_map", "rule_id, status, control_id",
        "MAPPED | UNMAPPED_ACTIVE_RULE | UNMAPPED_NON_ACTIVE_RULE | UNREGISTERED_RULE")
    add(cv, "fire_count", "rule_fires", "fire_id, rule_id, fired_at",
        "Count of de-duplicated fire rows for the rule with fired_at inside the period")
    add(cv, "distinct_applications_fired", "rule_fires", "application_id, rule_id",
        "Count of distinct application_id values the rule fired on in the period")
    add(cv, "share_of_period_fires_pct", "rule_fires", "fire_id",
        "fire_count / total period fire_count * 100")
    add(cv, "alert_count", "alerts", "alert_id, rule_id, created_at",
        "Count of de-duplicated alerts for the rule with created_at inside the period")
    add(cv, "alert_rate_pct", "alerts|rule_fires", "alert_id, fire_id",
        "alert_count / fire_count * 100; blank when fire_count = 0")
    add(cv, "reviews_decided", "review_outcomes|alerts", "outcome, decided_at, alert_id",
        "Alerts for the rule with an approved outcome code and decided_at <= as_of")
    add(cv, "outcome_confirmed_fraud", "review_outcomes|alerts", "outcome",
        f"Count of included dispositions with outcome = {OUTCOME_CONFIRMED}")
    add(cv, "outcome_cleared_false_positive", "review_outcomes|alerts", "outcome",
        f"Count of included dispositions with outcome = {OUTCOME_CLEARED}")
    add(cv, "outcome_insufficient_evidence", "review_outcomes|alerts", "outcome",
        f"Count of included dispositions with outcome = {OUTCOME_INSUFFICIENT}")
    add(cv, "outcome_pending", "alerts|review_outcomes", "alert_id, decided_at",
        "Period alerts for the rule with no approved disposition on or before as_of")
    add(cv, "review_coverage_pct", "review_outcomes|alerts", "alert_id, outcome",
        "reviews_decided / alert_count * 100; blank when alert_count = 0")
    add(cv, "alert_precision_pct", "review_outcomes", "outcome",
        "confirmed / (confirmed + cleared) * 100; inconclusive and pending excluded from "
        "the denominator; blank when that denominator is 0")
    add(cv, "distinct_reviewers", "review_outcomes", "reviewer_id",
        "Count of distinct reviewer_id values on the rule's included dispositions")
    add(cv, "linked_disbursed_loans", "rule_fires|loans", "application_id",
        "Period loans whose application_id appears in the rule's fire rows. "
        "NON-EXCLUSIVE: do not sum across rules")
    add(cv, "linked_confirmed_fraud_loans", "rule_fires|loans|fraud_labels",
        "application_id, loan_id, label_date",
        "Linked loans carrying a fraud label with label_date <= as_of. NON-EXCLUSIVE")
    add(cv, "linked_confirmed_fraud_net_loss_usd", "fraud_labels", "net_loss_amount",
        "Sum of net_loss_amount over linked confirmed-fraud loans. NON-EXCLUSIVE")
    add(cv, "fraud_label_status", "loans|fraud_labels|run_parameter",
        "disbursal_date, --as-of, --label-maturity-days",
        "FINAL when every period loan has passed the labelling window, else PROVISIONAL")
    add(cv, "rule_evidence_status", "rule_registry|rule_fires", "status, fire_id",
        f"{EV_EVIDENCED} for ACTIVE rules meeting the minimum fire threshold; "
        f"{EV_NO_ACTIVITY} for silent ACTIVE rules; {LABEL_NOT_APPLICABLE} otherwise")
    add(cv, "source_files", "manifest.json", "logical_name",
        "Logical source extracts behind the row; resolve to path and SHA-256 in the manifest")

    cc = "control_coverage.csv"
    add(cc, "period", "run_parameter", "--period", "Reporting month, echoed on every row")
    add(cc, "control_id", "audit_checklist", "control_id", "One row per checklist control")
    add(cc, "control_title", "audit_checklist", "control_title", "Direct lookup")
    add(cc, "control_family", "audit_checklist", "control_family", "Direct lookup")
    add(cc, "regulatory_reference", "audit_checklist", "regulatory_reference", "Direct lookup")
    add(cc, "evidence_requirement", "audit_checklist", "evidence_requirement", "Direct lookup")
    add(cc, "mapped_rules_total", "rule_control_map", "rule_id, control_id",
        "Distinct rules mapped to the control")
    add(cc, "mapped_rules_active", "rule_control_map|rule_registry", "rule_id, status",
        "Mapped rules whose registry status is ACTIVE")
    add(cc, "mapped_rules_non_active", "rule_control_map|rule_registry", "rule_id, status",
        "Mapped rules whose registry status is not ACTIVE (stale mapping)")
    add(cc, "active_rules_with_fires", "rule_control_map|rule_registry|rule_fires",
        "rule_id, status, fire_id",
        "Mapped ACTIVE rules meeting the minimum fire threshold in the period")
    add(cc, "fire_count", "rule_fires", "fire_id",
        "Sum of fire_count over mapped ACTIVE rules. Rules mapped to several controls "
        "contribute to each")
    add(cc, "alert_count", "alerts", "alert_id", "Sum of alert_count over mapped ACTIVE rules")
    add(cc, "reviews_decided", "review_outcomes", "outcome",
        "Sum of reviews_decided over mapped ACTIVE rules")
    add(cc, "confirmed_fraud_reviews", "review_outcomes", "outcome",
        f"Sum of outcome = {OUTCOME_CONFIRMED} over mapped ACTIVE rules")
    add(cc, "manual_review_evidence", "review_outcomes", "outcome",
        "Y when reviews_decided > 0, else N")
    add(cc, "evidence_status", "audit_checklist|rule_control_map|rule_registry|rule_fires",
        "control_id, rule_id, status, fire_id",
        f"{EV_EVIDENCED} | {EV_NO_ACTIVITY} | {EV_NO_ACTIVE_RULE} | {EV_UNMAPPED}")
    add(cc, "remediation_action", "derived", "evidence_status",
        "Fixed remediation text keyed off evidence_status; blank when EVIDENCED")
    add(cc, "source_files", "manifest.json", "logical_name",
        "Logical source extracts behind the row")

    ct = "control_coverage_trend.csv"
    add(ct, "current_period", "run_parameter", "--period", "Current reporting month")
    add(ct, "prior_period", "run_parameter", "--compare-period",
        "Prior comparison month supplied by --compare-period")
    add(ct, "control_id", "audit_checklist", "control_id",
        "One row per checklist control seen in either period")
    add(ct, "control_title", "audit_checklist", "control_title", "Direct lookup")
    add(ct, "control_family", "audit_checklist", "control_family", "Direct lookup")
    add(ct, "prior_status", "audit_checklist|rule_control_map|rule_registry|rule_fires",
        "evidence_status", "EVIDENCED | NOT_EVIDENCED | ABSENT, derived from the prior period")
    add(ct, "current_status", "audit_checklist|rule_control_map|rule_registry|rule_fires",
        "evidence_status", "EVIDENCED | NOT_EVIDENCED | ABSENT, derived from the current period")
    add(ct, "change_class", "derived", "prior_status, current_status",
        "NEWLY_EVIDENCED | REGRESSED | STILL_EVIDENCED | STILL_UNEVIDENCED | "
        "NEW_CONTROL | RETIRED_CONTROL")
    add(ct, "source_files", "manifest.json", "logical_name",
        "Logical source extracts behind the row")

    um = "unmapped_rules.csv"
    add(um, "period", "run_parameter", "--period", "Reporting month")
    add(um, "rule_id", "rule_registry|rule_fires", "rule_id", "Rules with a mapping gap")
    add(um, "rule_name", "rule_registry", "rule_name", "Direct lookup")
    add(um, "rule_status", "rule_registry", "status", "Direct lookup")
    add(um, "risk_typology", "rule_registry", "risk_typology", "Direct lookup")
    add(um, "rule_owner", "rule_registry", "owner", "Direct lookup; the remediation owner")
    add(um, "fire_count", "rule_fires", "fire_id", "As control_view.csv")
    add(um, "alert_count", "alerts", "alert_id", "As control_view.csv")
    add(um, "gap_type", "rule_registry|rule_control_map", "status, control_id",
        "UNMAPPED_ACTIVE_RULE | NON_ACTIVE_RULE_FIRING | UNREGISTERED_RULE_FIRING")
    add(um, "audit_risk", "derived", "gap_type", "Fixed severity keyed off gap_type")
    add(um, "recommended_action", "derived", "gap_type", "Fixed remediation text per gap_type")
    add(um, "source_files", "manifest.json", "logical_name", "Logical source extracts")

    ie = "integrity_exceptions.csv"
    add(ie, "period", "run_parameter", "--period", "Reporting month")
    add(ie, "exception_id", "derived", "-",
        "EX-nnnn, assigned after sorting by severity, code and entity - stable across runs")
    add(ie, "exception_code", "derived", "-", "Machine-readable defect class")
    add(ie, "severity", "derived", "-", "CRITICAL | HIGH | MEDIUM | LOW | INFO")
    add(ie, "entity_type", "derived", "-", "Entity the exception is raised against")
    add(ie, "entity_id", "derived", "-",
        "Entity identifier, or n=<count> for an aggregated record-level defect class")
    add(ie, "description", "derived", "-",
        "Human-readable finding, including up to five sample identifiers when aggregated")
    add(ie, "detected_by_check", "derived", "-", "Name of the check that raised the exception")
    add(ie, "source_files", "manifest.json", "logical_name", "Extracts the check compared")

    fl = "fraud_loss_baseline.csv"
    add(fl, "cohort_period", "loans", "disbursal_date", "Disbursal month of the cohort")
    add(fl, "is_reporting_period", "run_parameter", "--period",
        "Y for the reporting period cohort, else N")
    add(fl, "loans_disbursed", "loans", "loan_id", "De-duplicated loans in the cohort")
    add(fl, "principal_disbursed_usd", "loans", "principal_amount",
        "Sum of principal_amount for the cohort")
    add(fl, "label_window_days_elapsed", "run_parameter", "--as-of",
        "as_of - cohort month end, in days")
    add(fl, "matured_loans", "loans|run_parameter",
        "disbursal_date, --as-of, --label-maturity-days",
        "Loans where (as_of - disbursal_date) >= label_maturity_days")
    add(fl, "cohort_maturity_pct", "loans", "disbursal_date",
        "matured_loans / loans_disbursed * 100")
    add(fl, "confirmed_fraud_loans", "fraud_labels", "loan_id, label_date",
        "Cohort loans with a fraud label whose label_date <= as_of")
    add(fl, "fraud_incidence_pct", "loans|fraud_labels", "loan_id",
        "confirmed_fraud_loans / loans_disbursed * 100")
    add(fl, "gross_exposure_usd", "fraud_labels", "gross_exposure_amount",
        "Sum over the cohort's confirmed-fraud loans")
    add(fl, "recoveries_usd", "fraud_labels", "recovery_amount",
        "Sum over the cohort's confirmed-fraud loans")
    add(fl, "net_loss_usd", "fraud_labels", "net_loss_amount",
        "Sum over the cohort's confirmed-fraud loans; observed to date, never projected")
    add(fl, "net_loss_rate_bps", "loans|fraud_labels", "net_loss_amount, principal_amount",
        "net_loss_usd / principal_disbursed_usd * 10000")
    add(fl, "label_status", "loans|run_parameter", "disbursal_date, --label-maturity-days",
        "FINAL when cohort_maturity_pct = 100, else PROVISIONAL")
    add(fl, "source_files", "manifest.json", "logical_name", "Logical source extracts")

    sm = "summary.csv"
    add(sm, "metric_id", "derived", "-", "Stable machine-readable metric key")
    add(sm, "metric_label", "derived", "-", "Human-readable metric name")
    add(sm, "value", "see computation", "-", "Metric value; blank when undefined")
    add(sm, "unit", "derived", "-", "count | percent | bps | USD | days | date | ratio | YYYY-MM")
    add(sm, "label_status", "derived", "-",
        "FINAL | PROVISIONAL | NOT_APPLICABLE - PROVISIONAL means the figure will move "
        "as confirmed-fraud labels mature")
    add(sm, "source_files", "manifest.json", "logical_name", "Logical source extracts")
    add(sm, "computation", "derived", "-", "The arithmetic, stated so it can be re-derived")

    ml = "metric_lineage.csv"
    add(ml, "output_file", "derived", "-", "Output file the column belongs to")
    add(ml, "output_column", "derived", "-", "Column name as emitted")
    add(ml, "source_files", "derived", "-",
        "Logical source extracts, 'derived' for computed-only, or 'run_parameter'")
    add(ml, "source_columns", "derived", "-", "Source columns consumed")
    add(ml, "transform", "derived", "-", "The transform applied")

    return rows


def _trend_flag(evidence_status: str) -> str:
    return "EVIDENCED" if evidence_status == EV_EVIDENCED else "NOT_EVIDENCED"


def build_control_trend_rows(current: Report, compare: Report) -> list[ControlTrendRow]:
    """Classify each checklist control's coverage change between two periods.

    Reuses each period's already-computed ``control_rows`` so the current figure stays
    identical to the coverage card; output is sorted by control_id for determinism.
    """
    current_by_id = {row.control_id: row for row in current.control_rows}
    prior_by_id = {row.control_id: row for row in compare.control_rows}
    rows: list[ControlTrendRow] = []
    for control_id in sorted(set(current_by_id) | set(prior_by_id)):
        cur = current_by_id.get(control_id)
        pri = prior_by_id.get(control_id)
        if cur is not None and pri is not None:
            current_status = _trend_flag(cur.evidence_status)
            prior_status = _trend_flag(pri.evidence_status)
            cur_ev = current_status == "EVIDENCED"
            pri_ev = prior_status == "EVIDENCED"
            if cur_ev and pri_ev:
                change = TREND_STILL_EVIDENCED
            elif cur_ev:
                change = TREND_NEWLY_EVIDENCED
            elif pri_ev:
                change = TREND_REGRESSED
            else:
                change = TREND_STILL_UNEVIDENCED
            title, family = cur.control_title, cur.control_family
        elif cur is not None:
            current_status = _trend_flag(cur.evidence_status)
            prior_status = "ABSENT"
            change = TREND_NEW_CONTROL
            title, family = cur.control_title, cur.control_family
        else:
            current_status = "ABSENT"
            prior_status = _trend_flag(pri.evidence_status)
            change = TREND_RETIRED_CONTROL
            title, family = pri.control_title, pri.control_family
        rows.append(ControlTrendRow(
            current_period=current.params.period,
            prior_period=compare.params.period,
            control_id=control_id,
            control_title=title,
            control_family=family,
            prior_status=prior_status,
            current_status=current_status,
            change_class=change,
            source_files="audit_checklist|rule_control_map|rule_registry|rule_fires",
        ))
    return rows


# --------------------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------------------

CSV_OUTPUTS: tuple[tuple[str, str], ...] = (
    ("control_view.csv", "rule_rows"),
    ("control_coverage.csv", "control_rows"),
    ("control_coverage_trend.csv", "trend_rows"),
    ("unmapped_rules.csv", "unmapped_rows"),
    ("integrity_exceptions.csv", "exception_rows"),
    ("fraud_loss_baseline.csv", "loss_rows"),
    ("summary.csv", "summary_rows"),
    ("metric_lineage.csv", "lineage_rows"),
)

ROW_TYPES: dict[str, type] = {
    "rule_rows": RuleControlRow,
    "control_rows": ControlCoverageRow,
    "trend_rows": ControlTrendRow,
    "unmapped_rows": UnmappedRuleRow,
    "exception_rows": ExceptionRow,
    "loss_rows": FraudLossRow,
    "summary_rows": SummaryRow,
    "lineage_rows": LineageRow,
}


def columns_for(row_type: type) -> list[str]:
    return [f.name for f in fields(row_type)]


def write_csv(path: Path, row_type: type, rows: Sequence[Any]) -> None:
    """Write a dataclass row list as CSV. Field order is the column order.

    ``lineterminator="\\n"`` is explicit so output is byte-identical on every platform -
    a prerequisite for the idempotency claim.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    header = columns_for(row_type)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def write_csv_outputs(report: Report) -> list[Path]:
    run_dir = report.params.run_dir()
    written: list[Path] = []
    for filename, attribute in CSV_OUTPUTS:
        path = run_dir / filename
        write_csv(path, ROW_TYPES[attribute], getattr(report, attribute))
        written.append(path)
    return written


# ---------------------------------------- HTML evidence pack ----------------------------
#
# Palette, mark specs and the stat-tile / meter contracts follow the project data-viz
# reference: one categorical hue per series in fixed slot order, reserved status colours
# that always ship with an icon and a label, 2px surface gaps between touching marks,
# 4px rounded data-ends, direct labels outside the bar end (never clipped), and a
# selected dark mode stepped for the dark surface rather than an automatic flip.

HTML_STYLE = """
:root {
  color-scheme: light;
  --page:            #f9f9f7;
  --surface-1:       #fcfcfb;
  --text-primary:    #0b0b0b;
  --text-secondary:  #52514e;
  --text-muted:      #898781;
  --gridline:        #e1e0d9;
  --baseline:        #c3c2b7;
  --border:          rgba(11,11,11,0.10);
  --series-1:        #2a78d6;
  --series-2:        #eb6834;
  --series-3:        #1baf7a;
  --series-4:        #eda100;
  --track:           #cde2fb;
  --status-good:     #0ca30c;
  --status-warning:  #fab219;
  --status-serious:  #ec835a;
  --status-critical: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page:           #0d0d0d;
    --surface-1:      #1a1a19;
    --text-primary:   #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted:     #898781;
    --gridline:       #2c2c2a;
    --baseline:       #383835;
    --border:         rgba(255,255,255,0.10);
    --series-1:       #3987e5;
    --series-2:       #d95926;
    --series-3:       #199e70;
    --series-4:       #c98500;
    --track:          #184f95;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 32px 28px 64px;
  background: var(--page);
  color: var(--text-primary);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}
main { max-width: 1180px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 36px 0 10px; }
h3 { font-size: 14px; margin: 24px 0 8px; color: var(--text-secondary); }
p  { margin: 8px 0; color: var(--text-secondary); max-width: 80ch; }
code { font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; font-size: 12px; }
.sub { color: var(--text-secondary); margin: 0 0 20px; }
.card {
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 18px 20px;
  margin: 14px 0;
}
.banner {
  border-left: 4px solid var(--status-warning);
  background: var(--surface-1);
  border-radius: 6px;
  padding: 12px 16px;
  margin: 16px 0;
}
.banner.critical { border-left-color: var(--status-critical); }
.banner.good     { border-left-color: var(--status-good); }
.banner strong { color: var(--text-primary); }
.tiles { display: flex; flex-wrap: wrap; gap: 12px; margin: 16px 0 4px; }
.tile {
  flex: 1 1 172px;
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
}
.tile .label { color: var(--text-secondary); font-size: 12px; }
.tile .value { font-size: 28px; font-weight: 600; margin: 6px 0 2px; }
.tile .foot  { color: var(--text-muted); font-size: 11px; }
.hero { font-size: 48px; font-weight: 600; line-height: 1.1; margin: 4px 0; }
.meter-track {
  height: 14px; border-radius: 7px; background: var(--track); overflow: hidden;
  margin: 10px 0 6px;
}
.meter-fill { height: 100%; border-radius: 7px; }
.pill {
  display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px;
  border: 1px solid var(--border); color: var(--text-secondary);
}
.legend { display: flex; flex-wrap: wrap; gap: 16px; margin: 10px 0 0; }
.legend span { color: var(--text-secondary); font-size: 12px; }
.swatch {
  display: inline-block; width: 10px; height: 10px; border-radius: 2px;
  margin-right: 6px; vertical-align: baseline;
}
.table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 10px; }
table { border-collapse: collapse; width: 100%; background: var(--surface-1); }
caption {
  caption-side: top; text-align: left; padding: 12px 14px 10px;
  color: var(--text-secondary); font-size: 12px;
}
th, td {
  padding: 7px 10px; text-align: left; border-bottom: 1px solid var(--gridline);
  font-size: 12px; vertical-align: top; white-space: nowrap;
}
th { color: var(--text-secondary); font-weight: 600; background: var(--surface-1); }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
tr:last-child td { border-bottom: none; }
td.wrap { white-space: normal; min-width: 320px; }
.sev::before { margin-right: 6px; }
.sev-CRITICAL, .sev-HIGH { color: var(--text-primary); font-weight: 600; }
.sev-CRITICAL::before { content: "\\25C6"; color: var(--status-critical); }
.sev-HIGH::before     { content: "\\25B2"; color: var(--status-serious); }
.sev-MEDIUM::before   { content: "\\25CF"; color: var(--status-warning); }
.sev-LOW::before      { content: "\\25CB"; color: var(--text-muted); }
.sev-INFO::before     { content: "\\2139"; color: var(--text-muted); }
.ev::before { margin-right: 6px; }
.ev-yes::before { content: "\\2714"; color: var(--status-good); }
.ev-no::before  { content: "\\2716"; color: var(--status-critical); }
footer { margin-top: 40px; color: var(--text-muted); font-size: 11px; }
@media print { body { background: #fff; } .card, table { break-inside: avoid; } }
"""

#: Numeric-looking columns are right-aligned with tabular figures.
NUMERIC_HINTS = ("count", "_pct", "_bps", "_usd", "total", "_days", "loans", "reviewers",
                 "value", "active_rules_with_fires", "outcome_", "days_")
WRAP_COLUMNS = {"description", "remediation_action", "recommended_action", "computation",
                "transform", "control_title", "evidence_requirement", "metric_label",
                "regulatory_reference", "regulatory_references", "control_titles"}


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _fmt_int(value: Any) -> str:
    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return _esc(value)


def _is_numeric_column(column: str) -> bool:
    return any(hint in column for hint in NUMERIC_HINTS)


def _cell_class(column: str, value: Any) -> str:
    if column in WRAP_COLUMNS:
        return "wrap"
    if column == "severity":
        return f"sev sev-{value}"
    if _is_numeric_column(column):
        return "num"
    return ""


def _html_table(caption: str, row_type: type, rows: Sequence[Any], limit: int | None = None,
                note: str = "") -> str:
    """Render dataclass rows verbatim, so the HTML and the CSV cannot disagree."""
    columns = columns_for(row_type)
    shown = list(rows)[:limit] if limit else list(rows)
    parts = ['<div class="table-wrap"><table>']
    suffix = ""
    if limit and len(rows) > limit:
        suffix = (f" Showing the first {limit:,} of {len(rows):,} rows; the CSV holds the "
                  f"complete population.")
    parts.append(f"<caption>{_esc(caption)}{_esc(suffix)} {_esc(note)}</caption>")
    parts.append("<thead><tr>" +
                 "".join(f"<th>{_esc(column)}</th>" for column in columns) +
                 "</tr></thead><tbody>")
    if not shown:
        parts.append(f'<tr><td colspan="{len(columns)}">No rows - nothing to report.</td></tr>')
    for row in shown:
        data = asdict(row)
        cells = []
        for column in columns:
            value = data[column]
            css = _cell_class(column, value)
            cells.append(f'<td class="{css}">{_esc(value)}</td>' if css
                         else f"<td>{_esc(value)}</td>")
        parts.append("<tr>" + "".join(cells) + "</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def _meter(percent_value: str) -> str:
    """Coverage meter: fill carries severity, track is a lighter step of the same ramp."""
    try:
        value = float(percent_value)
    except (TypeError, ValueError):
        value = 0.0
    value = max(0.0, min(100.0, value))
    colour = ("var(--status-good)" if value >= 90
              else "var(--series-1)" if value >= 75
              else "var(--status-warning)" if value >= 50
              else "var(--status-critical)")
    return (f'<div class="meter-track"><div class="meter-fill" '
            f'style="width:{value:.2f}%;background:{colour}"></div></div>')


def _rounded_right_bar(x: float, y: float, width: float, height: float,
                       radius: float = 4.0) -> str:
    """Bar path: square at the baseline, 4px rounded at the data end."""
    radius = max(0.0, min(radius, width / 2))
    if width <= 0:
        return ""
    return (f"M{x:.1f},{y:.1f} H{x + width - radius:.1f} "
            f"A{radius:.1f},{radius:.1f} 0 0 1 {x + width:.1f},{y + radius:.1f} "
            f"V{y + height - radius:.1f} "
            f"A{radius:.1f},{radius:.1f} 0 0 1 {x + width - radius:.1f},{y + height:.1f} "
            f"H{x:.1f} Z")


def _alert_volume_chart(rule_rows: Sequence[RuleControlRow], top_n: int = 12) -> str:
    """Horizontal bar chart of alert volume by rule. Single series, so no legend box."""
    ranked = sorted([row for row in rule_rows if row.alert_count > 0],
                    key=lambda row: (-row.alert_count, row.rule_id))[:top_n]
    if not ranked:
        return "<p>No alerts were raised in the reporting period.</p>"

    label_width, bar_area, row_height, thickness = 250, 470, 26, 18
    height = len(ranked) * row_height + 16
    width = label_width + bar_area + 70
    peak = max(row.alert_count for row in ranked)

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
             f'role="img" aria-label="Alert volume by fraud rule">']
    # Recessive baseline; no gridlines needed because every bar is directly labelled.
    parts.append(f'<line x1="{label_width}" y1="8" x2="{label_width}" y2="{height - 8}" '
                 f'stroke="var(--baseline)" stroke-width="1" />')
    for index, row in enumerate(ranked):
        y = 8 + index * row_height + (row_height - thickness) / 2
        bar_width = row.alert_count / peak * bar_area
        control = row.control_ids or "UNMAPPED"
        parts.append(
            f'<text x="{label_width - 10}" y="{y + thickness / 2 + 4}" text-anchor="end" '
            f'font-size="11" fill="var(--text-secondary)">'
            f'{_esc(row.rule_id)} &#183; {_esc(control)}</text>'
        )
        parts.append(
            f'<path d="{_rounded_right_bar(label_width, y, bar_width, thickness)}" '
            f'fill="var(--series-1)"><title>{_esc(row.rule_id)}: '
            f'{row.alert_count:,} alerts from {row.fire_count:,} fires '
            f'({_esc(row.alert_rate_pct or "n/a")}% alert rate), control '
            f'{_esc(control)}</title></path>'
        )
        # Direct label outside the bar end - measured space, never clipped.
        parts.append(
            f'<text x="{label_width + bar_width + 8}" y="{y + thickness / 2 + 4}" '
            f'font-size="11" fill="var(--text-secondary)" '
            f'style="font-variant-numeric:tabular-nums">{row.alert_count:,}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _outcome_mix_chart(report: Report) -> str:
    """Single stacked bar: portfolio review outcome mix, with a legend (4 series)."""
    segments = [
        ("Confirmed fraud", int(report.summary_value("reviews_confirmed_fraud", "0") or 0),
         "var(--series-1)"),
        ("Cleared / false positive",
         int(report.summary_value("reviews_cleared_false_positive", "0") or 0),
         "var(--series-2)"),
        ("Insufficient evidence",
         int(report.summary_value("reviews_insufficient_evidence", "0") or 0),
         "var(--series-3)"),
        ("Pending at as-of date", int(report.summary_value("alerts_pending", "0") or 0),
         "var(--series-4)"),
    ]
    total = sum(count for _, count, _ in segments)
    if not total:
        return "<p>No alerts were raised in the reporting period.</p>"

    width, height, gap = 880, 26, 2
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img" '
             f'aria-label="Manual review outcome mix">']
    x = 0.0
    last = len(segments) - 1
    for index, (label, count, colour) in enumerate(segments):
        if count <= 0:
            continue
        segment_width = count / total * width
        # 2px surface gap between touching segments; only the data end is rounded.
        drawn = segment_width if index == last else max(segment_width - gap, 0.5)
        path = (_rounded_right_bar(x, 0, drawn, height) if index == last
                else f"M{x:.1f},0 H{x + drawn:.1f} V{height} H{x:.1f} Z")
        parts.append(f'<path d="{path}" fill="{colour}"><title>{_esc(label)}: '
                     f'{count:,} of {total:,} alerts ({count / total * 100:.1f}%)</title></path>')
        x += segment_width
    parts.append("</svg>")
    parts.append('<div class="legend">')
    for label, count, colour in segments:
        share = f"{count / total * 100:.1f}%" if total else "n/a"
        parts.append(f'<span><i class="swatch" style="background:{colour}"></i>'
                     f'{_esc(label)} &#183; {count:,} ({share})</span>')
    parts.append("</div>")
    return "".join(parts)


def _tile(label: str, value: str, foot: str) -> str:
    return (f'<div class="tile"><div class="label">{_esc(label)}</div>'
            f'<div class="value">{_esc(value)}</div>'
            f'<div class="foot">{_esc(foot)}</div></div>')


def _control_trend_card(report: Report) -> str:
    """Render the control-coverage-trend card, or a notice when no trend is available."""
    parts = ['<div class="card">', "<h3>Control coverage trend</h3>"]
    if not report.compare_usable:
        note = report.compare_note or ("No comparison period was specified "
                                       "(--compare-period); the coverage trend is not shown.")
        parts.append(f"<p>{_esc(note)}</p></div>")
        return "".join(parts)
    prior = report.compare_evidenced_pct or "0"
    current = report.summary_value("controls_evidenced_pct", "0")
    delta = to_float(current) - to_float(prior)
    sign = "+" if delta >= 0 else ""
    parts.append(
        f"<p>Checklist control coverage {_esc(report.compare_period)} &#8594; "
        f"{_esc(report.params.period)}: <strong>{_esc(prior)}%</strong> &#8594; "
        f"<strong>{_esc(current)}%</strong> "
        f"(<strong>{sign}{delta:.2f}</strong> pts)</p>"
    )
    counts: dict[str, int] = {}
    for row in report.trend_rows:
        counts[row.change_class] = counts.get(row.change_class, 0) + 1
    order = (TREND_NEWLY_EVIDENCED, TREND_REGRESSED, TREND_STILL_EVIDENCED,
             TREND_STILL_UNEVIDENCED, TREND_NEW_CONTROL, TREND_RETIRED_CONTROL)
    items = "".join(
        f"<li>{_esc(name.replace('_', ' ').title())}: {counts[name]}</li>"
        for name in order if counts.get(name))
    parts.append(f"<ul>{items}</ul></div>")
    return "".join(parts)


def render_html(report: Report, manifest: dict[str, Any]) -> str:
    """Render the single-file evidence pack.

    Contains no wall-clock value: the pack is a pure function of the inputs and the run
    parameters, so re-running the month reproduces it byte for byte.
    """
    params = report.params
    period = params.period
    evidenced_pct = report.summary_value("controls_evidenced_pct", "0")
    evidenced = report.summary_value("controls_evidenced", "0")
    controls_total = report.summary_value("controls_total", "0")
    maturity = report.summary_value("reporting_period_label_maturity_pct", "")
    baseline_period = report.summary_value("fraud_loss_baseline_period", "")
    baseline_bps = report.summary_value("fraud_loss_baseline_rate_bps", "")
    provisional_bps = report.summary_value("reporting_period_net_loss_rate_bps", "")
    critical = int(report.summary_value("exceptions_critical", "0") or 0)
    high = int(report.summary_value("exceptions_high", "0") or 0)
    days_to_audit = report.summary_value("days_to_audit", "")
    datasets_loaded = sum(1 for entry in manifest["inputs"] if entry["status"] == "LOADED")

    parts: list[str] = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>Audit-Readiness Fraud Control view - {_esc(period)}</title>",
        f"<style>{HTML_STYLE}</style></head><body><main>",
        f"<h1>Audit-Readiness Fraud Control view &#183; {_esc(period)}</h1>",
        f'<p class="sub">Read-only evidence pack &#183; snapshot as at '
        f'{_esc(params.as_of.isoformat())} &#183; '
        f'<span class="pill">{_esc(TOOL_NAME)} v{_esc(TOOL_VERSION)}</span> '
        f'<span class="pill">no credit decisions</span> '
        f'<span class="pill">payload '
        f'{_esc(manifest["determinism"]["deterministic_payload_sha256"][:12])}&#8230;</span> '
        f'<span class="pill">{datasets_loaded} datasets loaded</span>'
        + (f' <span class="pill">{_esc(days_to_audit)} days to audit</span>'
           if days_to_audit else "") + "</p>",
    ]

    # ---- provisional / integrity banners
    parts.append(
        f'<div class="banner"><strong>&#9888; Recent-period fraud metrics are '
        f'PROVISIONAL.</strong> Confirmed fraud is labelled 60&#8211;90 days after '
        f'disbursal, and {_esc(maturity or "0.00")}% of {_esc(period)} disbursals have '
        f'passed the {params.label_maturity_days}-day window as at '
        f'{_esc(params.as_of.isoformat())}. Every fraud-outcome and loss figure for '
        f'{_esc(period)} will move upward as labels land. The FINAL loss baseline below '
        f'is taken from cohort {_esc(baseline_period or "n/a")}, which is fully matured.'
        f'</div>'
    )
    if critical or high:
        parts.append(
            f'<div class="banner critical"><strong>&#9670; '
            f'{critical:,} critical and {high:,} high-severity exceptions.</strong> '
            f'See <em>Integrity &amp; governance exceptions</em>. Unregistered or '
            f'non-active rules in the production decision path, and active rules with no '
            f'mapped control, are the findings an examiner reaches first.</div>'
        )
    else:
        parts.append('<div class="banner good"><strong>&#10004; No critical or '
                     'high-severity exceptions.</strong></div>')

    # ---- KPI tiles + hero coverage meter
    parts.append('<div class="tiles">')
    parts.append(_tile("Controls evidenced", f"{evidenced} / {controls_total}",
                       "audit_checklist x rule activity"))
    parts.append(_tile("Active rules firing",
                       f'{report.summary_value("active_rules_with_activity", "0")} / '
                       f'{report.summary_value("active_rules_registered", "0")}',
                       f'{report.summary_value("rules_in_registry_total", "0")} rules in the '
                       f'legacy registry'))
    parts.append(_tile("Alerts raised", _fmt_int(report.summary_value("alerts_total", "0")),
                       f'{_fmt_int(report.summary_value("rule_fires_total", "0"))} rule fires'))
    parts.append(_tile("Review coverage",
                       f'{report.summary_value("review_coverage_pct", "")}%',
                       f'{_fmt_int(report.summary_value("alerts_pending", "0"))} alerts pending'))
    parts.append(_tile("Alert precision",
                       f'{report.summary_value("portfolio_alert_precision_pct", "")}%',
                       "PROVISIONAL · confirmed / (confirmed + cleared)"))
    parts.append(_tile("Fraud loss baseline", f"{baseline_bps or 'n/a'} bps",
                       f'FINAL · cohort {baseline_period or "n/a"}'))
    parts.append("</div>")

    parts.append('<div class="card">')
    parts.append("<h3>Percentage of audit checklist controls evidenced</h3>")
    parts.append(f'<div class="hero">{_esc(evidenced_pct)}%</div>')
    parts.append(_meter(evidenced_pct))
    parts.append(
        f'<p>{_esc(evidenced)} of {_esc(controls_total)} checklist controls have at least '
        f'one mapped ACTIVE rule that fired at least {params.min_fires_for_evidence} '
        f'time(s) in {_esc(period)}. Controls evidenced outside the rules engine (policy, '
        f'training, third-party attestations) are out of scope for this pack and must be '
        f'evidenced separately.</p>'
    )
    parts.append("</div>")

    parts.append(_control_trend_card(report))

    # ---- charts
    parts.append("<h2>Alert volume by fraud rule</h2>")
    parts.append(f'<p>Top 12 rules by alert volume, labelled with the audit checklist '
                 f'control each one evidences. Hover a bar for fire count and alert rate. '
                 f'Full population in <code>control_view.csv</code>.</p>')
    parts.append(f'<div class="card">{_alert_volume_chart(report.rule_rows)}</div>')

    parts.append("<h2>Manual review outcome mix</h2>")
    parts.append(f'<p>All {_fmt_int(report.summary_value("alerts_total", "0"))} alerts raised '
                 f'in {_esc(period)}, by disposition as at {_esc(params.as_of.isoformat())}. '
                 f'Confirmed-fraud dispositions are PROVISIONAL.</p>')
    parts.append(f'<div class="card">{_outcome_mix_chart(report)}</div>')

    # ---- tables (verbatim CSV content)
    parts.append("<h2>Summary metrics</h2>")
    parts.append("<p>Each metric carries its own source files and computation, so any "
                 "figure in this pack can be re-derived by hand from the extracts pinned "
                 "in the manifest.</p>")
    parts.append(_html_table("summary.csv - headline metrics with lineage", SummaryRow,
                             report.summary_rows))

    parts.append("<h2>Fraud loss baseline</h2>")
    parts.append("<p>Observed loss only - never projected. A cohort is FINAL once every "
                 "loan in it has passed the labelling window; younger cohorts are "
                 "PROVISIONAL and understate loss.</p>")
    parts.append(_html_table("fraud_loss_baseline.csv - per disbursal cohort", FraudLossRow,
                             report.loss_rows))

    parts.append("<h2>Control coverage against the audit checklist</h2>")
    parts.append(_html_table("control_coverage.csv - one row per checklist control",
                             ControlCoverageRow, report.control_rows))

    parts.append("<h2>Fraud rule control view</h2>")
    parts.append("<p>One row per rule in scope: every ACTIVE registry rule plus any rule "
                 "observed firing or alerting in the period, even when it is not ACTIVE or "
                 "not registered at all. Loan-linked columns are non-exclusive attribution "
                 "(several rules fire on one application) and must not be summed across "
                 "rules.</p>")
    parts.append(_html_table("control_view.csv - fraud rule metrics and mapped control",
                             RuleControlRow, report.rule_rows, limit=60))

    parts.append("<h2>Rule / control mapping gaps</h2>")
    parts.append(_html_table("unmapped_rules.csv - rules with no usable control mapping",
                             UnmappedRuleRow, report.unmapped_rows))

    parts.append("<h2>Integrity &amp; governance exceptions</h2>")
    parts.append(_html_table("integrity_exceptions.csv - reconciliation, change control "
                             "and governance findings", ExceptionRow, report.exception_rows))

    # ---- scope, lineage, manifest
    parts.append("<h2>Scope &amp; limitations</h2>")
    parts.append(
        "<ul>"
        "<li><strong>Read-only.</strong> Source extracts are opened for reading only. "
        "Input digests are captured on load and re-verified after the run; a mismatch "
        "aborts the run.</li>"
        "<li><strong>No credit decisions.</strong> This tool contains no scoring or "
        "approve/decline/refer logic. It describes decisions the rules engine already "
        "made and must never sit in a decisioning path.</li>"
        "<li><strong>Monthly idempotent.</strong> Output is a pure function of the "
        "reporting period, the as-of date and the input bytes. Dispositions decided after "
        "the as-of date are excluded, so re-running a closed month reproduces it exactly. "
        "No wall-clock value appears in any CSV or in this pack.</li>"
        "<li><strong>Provisional fraud metrics.</strong> Confirmed fraud is labelled 60-90 "
        "days after disbursal. Any figure marked PROVISIONAL will move; only fully matured "
        "cohorts are FINAL.</li>"
        "<li><strong>Non-exclusive attribution.</strong> Several rules fire on one "
        "application, so <code>linked_confirmed_fraud_loans</code> and "
        "<code>linked_confirmed_fraud_net_loss_usd</code> overlap across rules. Their sum "
        "exceeds portfolio loss by design; use the fraud loss baseline for portfolio "
        "totals.</li>"
        "<li><strong>Precision denominator.</strong> Alert precision excludes inconclusive "
        "and pending alerts from the denominator, so it is an upper bound on rule "
        "performance.</li>"
        "<li><strong>Evidence definition.</strong> 'Evidenced' means rule activity exists "
        "in the period for a mapped ACTIVE rule. It does not assert that the control is "
        "<em>effective</em> - effectiveness testing is a separate exercise.</li>"
        "<li><strong>Coverage boundary.</strong> Controls operated outside the rules engine "
        "cannot be evidenced by this pack and appear as "
        "NOT_EVIDENCED_NO_RULE_MAPPED.</li>"
        "</ul>"
    )

    parts.append("<h2>Metric lineage</h2>")
    parts.append(_html_table("metric_lineage.csv - output column to source column and "
                             "transform", LineageRow, report.lineage_rows))

    parts.append("<h2>Source manifest</h2>")
    parts.append("<p>Every figure in this pack derives from the extracts below. The SHA-256 "
                 "digest pins the exact file contents used; re-running against the same "
                 "digests reproduces this pack byte for byte.</p>")
    parts.append('<div class="table-wrap"><table>')
    parts.append("<caption>manifest.json - source extracts</caption>")
    parts.append("<thead><tr><th>logical_name</th><th>path</th><th>status</th>"
                 "<th>row_count</th><th>sha256</th></tr></thead><tbody>")
    for entry in manifest["inputs"]:
        status_class = "ev ev-yes" if entry["status"] == "LOADED" else "ev ev-no"
        parts.append(
            f'<tr><td>{_esc(entry["logical_name"])}</td>'
            f'<td>{_esc(entry["path"])}</td>'
            f'<td class="{status_class}">{_esc(entry["status"])}</td>'
            f'<td class="num">{entry["row_count"]:,}</td>'
            f'<td><code>{_esc(entry["sha256"] or "-")}</code></td></tr>'
        )
    parts.append("</tbody></table></div>")

    parts.append(
        f"<footer>Reporting period {_esc(period)} &#183; as-of "
        f"{_esc(params.as_of.isoformat())} &#183; label maturity window "
        f"{params.label_maturity_days} days &#183; deterministic payload digest "
        f'<code>{_esc(manifest["determinism"]["deterministic_payload_sha256"])}</code>. '
        f"Run timestamp and environment are recorded in <code>manifest.json</code>, "
        f"outside the hashed payload, so this pack stays byte-identical across runs."
        f"</footer>"
    )
    parts.append("</main></body></html>")
    return "".join(parts)


# --------------------------------------------------------------------------------------
# Manifest & orchestration
# --------------------------------------------------------------------------------------


def build_manifest(report: Report, output_files: Sequence[Path],
                   compare_sources: "SourceSet | None" = None) -> dict[str, Any]:
    """Assemble the run manifest.

    ``deterministic_payload_sha256`` covers every output except the manifest itself and
    is therefore stable across runs; the run timestamp deliberately sits outside it.
    """
    params = report.params
    outputs = []
    for path in sorted(output_files, key=lambda item: item.name):
        outputs.append({
            "file": path.name,
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        })
    payload = "\n".join(f'{entry["file"]}:{entry["sha256"]}' for entry in outputs)
    payload_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    manifest: dict[str, Any] = {
        "tool": {
            "name": TOOL_NAME,
            "version": TOOL_VERSION,
            "mode": "READ_ONLY",
            "makes_credit_decisions": False,
            "purpose": "Monthly audit-readiness evidence for fraud rule controls.",
        },
        "run": {
            "reporting_period": params.period,
            "as_of_date": params.as_of.isoformat(),
            "run_timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "python_version": sys.version.split()[0],
            "parameters": params.as_dict(),
        },
        "inputs": [report.sources.files[name].manifest_entry()
                   for name in sorted(report.sources.files)],
        "outputs": outputs,
        "row_counts": {
            "control_view.csv": len(report.rule_rows),
            "control_coverage.csv": len(report.control_rows),
            "control_coverage_trend.csv": len(report.trend_rows),
            "unmapped_rules.csv": len(report.unmapped_rows),
            "integrity_exceptions.csv": len(report.exception_rows),
            "fraud_loss_baseline.csv": len(report.loss_rows),
            "summary.csv": len(report.summary_rows),
            "metric_lineage.csv": len(report.lineage_rows),
        },
        "determinism": {
            "deterministic_payload_sha256": payload_digest,
            "note": "SHA-256 over 'filename:sha256' lines for every output except this "
                    "manifest. No wall-clock value enters those files, so an unchanged "
                    "input set reproduces this digest exactly.",
        },
        "read_only_verification": {
            "input_digests_reverified_after_run": True,
            "inputs_unchanged": True,
        },
    }
    if compare_sources is not None:
        manifest["compare_inputs"] = [compare_sources.files[name].manifest_entry()
                                      for name in sorted(compare_sources.files)]
    return manifest


def assert_output_dir_safe(params: RunParams) -> None:
    """Refuse to write anywhere inside the input directory."""
    in_dir = params.input_dir.resolve()
    out_dir = params.output_dir.resolve()
    if out_dir == in_dir or in_dir in out_dir.parents:
        raise ReadOnlyViolationError(
            f"output directory {out_dir} is inside the input directory {in_dir}; "
            f"refusing to write next to the source extracts"
        )


def verify_inputs_unchanged(sources: SourceSet, digests_at_load: dict[str, str]) -> None:
    """Re-hash every loaded input and fail if anything moved during the run."""
    changed: list[str] = []
    for name, expected in digests_at_load.items():
        source = sources.files[name]
        if not source.path.is_file() or sha256_file(source.path) != expected:
            changed.append(name)
    if changed:
        raise ReadOnlyViolationError(
            f"input extract(s) changed during the run: {sorted(changed)}; "
            f"the output cannot be trusted as a snapshot"
        )


def run(params: RunParams) -> Report:
    """Load, aggregate, write, then prove the inputs were untouched."""
    assert_output_dir_safe(params)

    sources = load_sources(params)
    digests_at_load = sources.digests()

    report = build_report(params, sources)

    compare_sources: SourceSet | None = None
    compare_digests: dict[str, str] = {}
    if params.compare_period:
        report.compare_period = params.compare_period
        compare_params = replace(params, period=params.compare_period, compare_period=None)
        try:
            compare_sources = load_sources(compare_params)
            compare_digests = compare_sources.digests()
            compare_report = build_report(compare_params, compare_sources)
        except SourceDataError as error:
            compare_sources = None
            report.compare_usable = False
            report.compare_note = (f"Comparison period {params.compare_period} is "
                                   f"unavailable: {error}")
        else:
            report.trend_rows = build_control_trend_rows(report, compare_report)
            report.compare_usable = True
            prior_pct = compare_report.summary_value("controls_evidenced_pct", "0")
            report.compare_evidenced_pct = prior_pct
            current_pct = report.summary_value("controls_evidenced_pct", "0")
            delta = to_float(current_pct) - to_float(prior_pct)
            report.summary_rows.append(SummaryRow(
                "controls_evidenced_pct_prior", "Controls evidenced (prior period)",
                prior_pct, "percent", LABEL_NOT_APPLICABLE,
                "audit_checklist|rule_control_map|rule_registry|rule_fires",
                f"controls_evidenced_pct recomputed for --compare-period "
                f"{params.compare_period}"))
            report.summary_rows.append(SummaryRow(
                "controls_evidenced_pct_delta", "Controls evidenced change vs prior period",
                f"{delta:.2f}", "percent", LABEL_NOT_APPLICABLE, "derived",
                "controls_evidenced_pct - controls_evidenced_pct_prior"))
    else:
        report.compare_note = ("No comparison period was specified (--compare-period); "
                               "the coverage trend is not shown.")

    written = write_csv_outputs(report)

    # The manifest hashes the CSVs, and the HTML embeds the manifest digest, so build in
    # this order: CSVs -> manifest -> HTML -> rewrite manifest with the HTML included.
    manifest = build_manifest(report, written, compare_sources)
    html_path = params.run_dir() / "evidence_pack.html"
    html_path.write_text(render_html(report, manifest), encoding="utf-8", newline="\n")
    manifest["outputs"].append({
        "file": html_path.name,
        "sha256": sha256_file(html_path),
        "bytes": html_path.stat().st_size,
    })
    manifest["outputs"].sort(key=lambda entry: entry["file"])

    verify_inputs_unchanged(sources, digests_at_load)
    if compare_sources is not None:
        verify_inputs_unchanged(compare_sources, compare_digests)

    manifest_path = params.run_dir() / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8",
                             newline="\n")
    return report


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fraud_control_view.py",
        description="Read-only Audit-Readiness Fraud Control view for one calendar month.",
        epilog="This tool makes no credit decisions and never writes to its input directory.",
    )
    parser.add_argument("--period", required=True,
                        help="reporting month, YYYY-MM (e.g. 2026-08)")
    parser.add_argument("--as-of", required=True, type=date.fromisoformat,
                        help="snapshot date, YYYY-MM-DD; nothing dated later is included")
    parser.add_argument("--input-dir", type=Path, default=Path("data"),
                        help="directory holding the source extracts (read-only)")
    parser.add_argument("--output-dir", type=Path, default=Path("out"),
                        help="directory for the evidence pack; must sit outside --input-dir")
    parser.add_argument("--label-maturity-days", type=int, default=90,
                        help="days after disbursal by which confirmed fraud is labelled")
    parser.add_argument("--baseline-lookback-months", type=int, default=3,
                        help="prior disbursal cohorts to load for the loss baseline")
    parser.add_argument("--expected-reviewer-count", type=int, default=6,
                        help="documented manual review team size")
    parser.add_argument("--min-fires-for-evidence", type=int, default=1,
                        help="fires a mapped active rule needs to evidence its control")
    parser.add_argument("--stale-review-days", type=int, default=365,
                        help="days after which an active rule's review is considered stale")
    parser.add_argument("--alert-ageing-sla-days", type=int, default=30,
                        help="days after which a pending alert is an exception")
    parser.add_argument("--audit-date", type=date.fromisoformat, default=None,
                        help="optional scheduled audit date, for the days-to-audit metric")
    parser.add_argument("--compare-period", default=None,
                        help="optional prior month, YYYY-MM, to compare control coverage "
                             "against (e.g. 2026-05)")
    parser.add_argument("--fail-on-critical", action="store_true",
                        help="exit 2 when any CRITICAL exception is reported")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    params = RunParams(
        period=args.period,
        as_of=args.as_of,
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        label_maturity_days=args.label_maturity_days,
        baseline_lookback_months=args.baseline_lookback_months,
        expected_reviewer_count=args.expected_reviewer_count,
        min_fires_for_evidence=args.min_fires_for_evidence,
        stale_review_days=args.stale_review_days,
        alert_ageing_sla_days=args.alert_ageing_sla_days,
        audit_date=args.audit_date,
        compare_period=args.compare_period,
    )
    period_bounds(params.period)  # fail fast on a malformed period
    if params.compare_period:
        period_bounds(params.compare_period)  # fail fast on a malformed compare period

    try:
        report = run(params)
    except (SourceDataError, ReadOnlyViolationError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    run_dir = params.run_dir()
    critical = sum(1 for row in report.exception_rows if row.severity == "CRITICAL")
    high = sum(1 for row in report.exception_rows if row.severity == "HIGH")
    print(f"Audit-Readiness Fraud Control view - {params.period} (as at {params.as_of})")
    print(f"  controls evidenced      : "
          f"{report.summary_value('controls_evidenced')}/"
          f"{report.summary_value('controls_total')} "
          f"({report.summary_value('controls_evidenced_pct')}%)")
    print(f"  active rules firing     : "
          f"{report.summary_value('active_rules_with_activity')}/"
          f"{report.summary_value('active_rules_registered')} "
          f"(of {report.summary_value('rules_in_registry_total')} in registry)")
    print(f"  unmapped active rules   : {report.summary_value('active_rules_unmapped')}")
    print(f"  alerts / dispositioned  : {report.summary_value('alerts_total')} / "
          f"{report.summary_value('alerts_dispositioned')} "
          f"({report.summary_value('review_coverage_pct')}%)")
    print(f"  fraud loss baseline     : "
          f"{report.summary_value('fraud_loss_baseline_rate_bps')} bps FINAL "
          f"(cohort {report.summary_value('fraud_loss_baseline_period')})")
    period_loss = report.summary("reporting_period_net_loss_rate_bps")
    print(f"  {params.period} loss to date   : "
          f"{period_loss.value if period_loss else ''} bps "
          f"{period_loss.label_status if period_loss else ''} "
          f"({report.summary_value('reporting_period_label_maturity_pct')}% label maturity)")
    print(f"  exceptions              : {len(report.exception_rows)} "
          f"({critical} critical, {high} high)")
    print(f"  evidence pack           : {run_dir / 'evidence_pack.html'}")

    if args.fail_on_critical and critical:
        print(f"ERROR: {critical} CRITICAL exception(s) reported", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
