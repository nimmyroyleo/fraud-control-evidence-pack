"""Deterministic synthetic data generator for the Audit-Readiness Fraud Control view.

This module exists ONLY to give the read-only reporting tool something realistic to
read. It is *not* part of the control environment and must never be run against
production paths. It fabricates the source extracts that ``fraud_control_view.py``
consumes:

===========================  ===================================================
File                         Represents (system of record in a real deployment)
===========================  ===================================================
rule_registry.csv            Legacy rules engine inventory (340 rules, ~40 active)
audit_checklist.csv          Regulatory audit checklist / control library
rule_control_map.csv         Control owner's rule -> control mapping workbook
loans_<period>.csv           Loan master / disbursal ledger
rule_fires_<period>.csv      Rules engine decision log (every rule evaluation hit)
alerts_<period>.csv          Fraud case management alert queue
review_outcomes_<period>.csv Manual review dispositions (6-reviewer team)
fraud_labels.csv             Confirmed-fraud / charge-off label file (T+60..90d)
===========================  ===================================================

The generator deliberately seeds a handful of realistic *defects* so the reporting
tool's exception logic has something to catch:

* active rules with no checklist control mapping (mapping gap);
* checklist controls with no active rule behind them (coverage gap);
* retired and shadow rules still firing in the decision log (change-control gap);
* a rule id in the decision log that is absent from the registry (integrity gap);
* stale rule governance reviews (> 365 days);
* alerts and review outcomes that reference unknown parents (referential gaps);
* a reporting period whose fraud labels are not yet mature (provisional metrics).

Usage
-----
    python generate_sample_data.py --out-dir data

Re-running with the same ``--seed`` produces byte-identical files.
"""

from __future__ import annotations

import argparse
import csv
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

# --------------------------------------------------------------------------------------
# Scenario constants (mirror the brief: ~30k loans/month, 340 rules, ~40 active, 6 reviewers)
# --------------------------------------------------------------------------------------

DEFAULT_SEED = 20260916
DEFAULT_LOANS_PER_MONTH = 30_000

#: Loan cohorts written to disk. The oldest is fully label-mature at the default
#: ``--as-of``; the newest is the audit reporting period and is still immature.
DEFAULT_COHORT_PERIODS = ("2026-05", "2026-06", "2026-07", "2026-08")

#: Periods for which rules-engine / case-management activity is generated. Two are
#: enough to demonstrate both a FINAL and a PROVISIONAL run of the tool.
DEFAULT_ACTIVITY_PERIODS = ("2026-05", "2026-08")

TOTAL_RULES = 340
ACTIVE_RULES = 40
SHADOW_RULES = 12
REVIEWER_IDS = tuple(f"RV-{i:02d}" for i in range(1, 7))  # six-person review team

APPROVAL_RATE = 0.40  # applications -> disbursed loans
FIRES_PER_APPLICATION = 0.8  # legacy engine average hits per application

RULE_OWNERS = (
    "fraud-strategy",
    "fraud-ops",
    "credit-risk",
    "financial-crime-compliance",
    "identity-platform",
)

TYPOLOGIES = (
    "SYNTHETIC_IDENTITY",
    "FIRST_PARTY_FRAUD",
    "IDENTITY_THEFT",
    "DEVICE_ANOMALY",
    "VELOCITY_ABUSE",
    "DOCUMENT_TAMPERING",
    "INCOME_MISREPRESENTATION",
    "ACCOUNT_TAKEOVER",
    "BOT_AUTOMATION",
    "MULE_ACCOUNT",
    "PII_MISMATCH",
    "GEO_ANOMALY",
)

PRODUCTS = ("PL_SMALL_12M", "PL_SMALL_24M", "PL_SMALL_36M")
CHANNELS = ("MOBILE_APP", "WEB_DIRECT", "AGGREGATOR", "PARTNER_API")
ALERT_QUEUES = ("IDENTITY", "TRANSACTION_MONITORING", "DOCUMENT", "HIGH_VALUE")

OUTCOME_REASON_CODES = {
    "CONFIRMED_FRAUD": ("SYNTH_ID_CONFIRMED", "DOC_FORGED", "ATO_CONFIRMED", "MULE_CONFIRMED"),
    "CLEARED_FALSE_POSITIVE": ("ID_VERIFIED", "DOC_VALID", "CUSTOMER_CONTACTED", "KNOWN_DEVICE"),
    "INSUFFICIENT_EVIDENCE": ("NO_CONTACT", "DATA_UNAVAILABLE", "AGED_OUT"),
}

LABEL_TYPES = ("FIRST_PARTY_FRAUD", "THIRD_PARTY_SYNTHETIC", "IDENTITY_THEFT")
LABEL_SOURCES = ("MANUAL_INVESTIGATION", "CHARGEOFF_FILE", "SAR_FILED")

#: Illustrative control library. Regulatory references are real frameworks a US
#: digital lender would be examined against, but the control text is synthetic.
CHECKLIST = (
    ("AC-01", "Applicant identity is verified before disbursal", "Identity Verification",
     "FCRA 615(e) / 16 CFR 681.1 Red Flags Rule", "Rule fire log + review disposition sample"),
    ("AC-02", "Synthetic identity indicators are detected and dispositioned", "Identity Verification",
     "NIST SP 800-63A IAL2", "Rule fire log + confirmed-fraud linkage"),
    ("AC-03", "Government ID documents are checked for tampering", "Identity Verification",
     "16 CFR 681.1 Appendix A II(a)", "Rule fire log + document review evidence"),
    ("AC-04", "PII mismatches against bureau data are investigated", "Identity Verification",
     "FCRA 611", "Rule fire log + reviewer notes"),
    ("AC-05", "Device and session anomalies are monitored", "Transaction Monitoring",
     "FFIEC Authentication & Access Guidance (2021)", "Rule fire log"),
    ("AC-06", "Application velocity is monitored across identity attributes", "Transaction Monitoring",
     "FFIEC BSA/AML Manual - Suspicious Activity Monitoring", "Rule fire log + alert volume"),
    ("AC-07", "Automated / bot application traffic is detected", "Transaction Monitoring",
     "FFIEC Authentication & Access Guidance (2021)", "Rule fire log"),
    ("AC-08", "Account takeover attempts are detected and escalated", "Transaction Monitoring",
     "GLBA Safeguards Rule 16 CFR 314.4(c)", "Rule fire log + escalation evidence"),
    ("AC-09", "Mule / third-party disbursal accounts are screened", "Transaction Monitoring",
     "31 CFR 1020.320", "Rule fire log + review disposition"),
    ("AC-10", "Geolocation inconsistencies are evaluated", "Transaction Monitoring",
     "FFIEC BSA/AML Manual - Customer Due Diligence", "Rule fire log"),
    ("AC-11", "Stated income misrepresentation is tested", "Underwriting Integrity",
     "12 CFR 1026.43(c)(2) - ability to repay verification", "Rule fire log + verification evidence"),
    ("AC-12", "First-party fraud patterns are monitored post-disbursal", "Underwriting Integrity",
     "Interagency Guidance on Credit Risk Review", "Confirmed-fraud label file"),
    ("AC-13", "Every fraud alert is dispositioned by a trained reviewer", "Case Management",
     "FFIEC BSA/AML Manual - Suspicious Activity Reporting", "Review outcome file + reviewer roster"),
    ("AC-14", "Alert dispositions are recorded with a reason code", "Case Management",
     "SOX ITGC - completeness of records", "Review outcome file"),
    ("AC-15", "Review queue ageing is monitored", "Case Management",
     "FFIEC BSA/AML Manual - Alert Management", "Alert file + review timestamps"),
    ("AC-16", "Reviewer decisions are subject to quality assurance sampling", "Case Management",
     "OCC Bulletin 2011-12 - independent review", "QA sample + reviewer distribution"),
    ("AC-17", "Confirmed fraud is escalated for SAR consideration", "Reporting & Escalation",
     "31 CFR 1020.320", "Confirmed-fraud label file + SAR log"),
    ("AC-18", "Fraud losses are reported to the risk committee monthly", "Reporting & Escalation",
     "Interagency Guidance on Model Risk Management (SR 11-7)", "Fraud loss baseline"),
    ("AC-19", "Active rule inventory is complete and owned", "Rule Governance",
     "SR 11-7 Section V - model inventory", "Rule registry"),
    ("AC-20", "Rule changes follow documented change control", "Rule Governance",
     "SOX ITGC - change management", "Rule registry versions + fire log"),
    ("AC-21", "Rule performance is reviewed at least annually", "Rule Governance",
     "SR 11-7 Section VI - ongoing monitoring", "Rule registry review dates + precision metrics"),
    ("AC-22", "Retired rules are removed from the production decision path", "Rule Governance",
     "SOX ITGC - change management", "Rule registry + fire log reconciliation"),
    ("AC-23", "Source data feeding fraud controls is reconciled", "Data Integrity",
     "SOX ITGC - data integrity", "Manifest row counts + hashes"),
    ("AC-24", "Fraud metrics are reproducible for an examiner", "Data Integrity",
     "SR 11-7 Section VII - documentation", "Run manifest + metric lineage"),
)

#: Typology -> checklist control. Drives the (deliberately incomplete) mapping file.
TYPOLOGY_TO_CONTROL = {
    "SYNTHETIC_IDENTITY": "AC-02",
    "FIRST_PARTY_FRAUD": "AC-12",
    "IDENTITY_THEFT": "AC-01",
    "DEVICE_ANOMALY": "AC-05",
    "VELOCITY_ABUSE": "AC-06",
    "DOCUMENT_TAMPERING": "AC-03",
    "INCOME_MISREPRESENTATION": "AC-11",
    "ACCOUNT_TAKEOVER": "AC-08",
    "BOT_AUTOMATION": "AC-07",
    "MULE_ACCOUNT": "AC-09",
    "PII_MISMATCH": "AC-04",
    "GEO_ANOMALY": "AC-10",
}


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    rule_id: str
    rule_name: str
    rule_version: str
    status: str
    risk_typology: str
    owner: str
    last_reviewed_date: date
    alert_probability: float
    fire_weight: float


def _period_bounds(period: str) -> tuple[date, date]:
    """Return (first_day, last_day) for a ``YYYY-MM`` period string."""
    year, month = (int(part) for part in period.split("-"))
    first = date(year, month, 1)
    last = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return first, last


def _write_csv(path: Path, header: list[str], rows: list[list[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _ts(day: date, rng: random.Random) -> str:
    """Random intra-day timestamp, ISO-8601 seconds precision."""
    seconds = rng.randrange(6 * 3600, 23 * 3600)
    return (datetime(day.year, day.month, day.day) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )


# --------------------------------------------------------------------------------------
# Generators
# --------------------------------------------------------------------------------------


def build_rules(rng: random.Random, as_of: date) -> list[Rule]:
    """Build the 340-rule legacy registry: ~40 ACTIVE, 12 SHADOW, remainder RETIRED."""
    ids = [f"R{i:04d}" for i in range(1, TOTAL_RULES + 1)]
    shuffled = ids[:]
    rng.shuffle(shuffled)
    active = set(shuffled[:ACTIVE_RULES])
    shadow = set(shuffled[ACTIVE_RULES:ACTIVE_RULES + SHADOW_RULES])

    rules: list[Rule] = []
    for index, rule_id in enumerate(ids):
        typology = TYPOLOGIES[index % len(TYPOLOGIES)]
        if rule_id in active:
            status = "ACTIVE"
            # Most active rules were reviewed recently; a few are deliberately stale
            # so the tool's annual-review governance check has something to report.
            age_days = rng.randrange(20, 320) if rng.random() > 0.15 else rng.randrange(400, 900)
        elif rule_id in shadow:
            status = "SHADOW"
            age_days = rng.randrange(20, 400)
        else:
            status = "RETIRED"
            age_days = rng.randrange(400, 2200)
        rules.append(
            Rule(
                rule_id=rule_id,
                rule_name=f"{typology.title().replace('_', ' ')} check {index + 1:03d}",
                rule_version=f"v{rng.randrange(1, 8)}.{rng.randrange(0, 10)}",
                status=status,
                risk_typology=typology,
                owner=RULE_OWNERS[index % len(RULE_OWNERS)],
                last_reviewed_date=as_of - timedelta(days=age_days),
                # Alert routing differs sharply by rule: some are pure telemetry.
                alert_probability=round(rng.choice([0.0, 0.02, 0.05, 0.12, 0.25, 0.4]), 3),
                fire_weight=round(rng.paretovariate(1.4), 4),
            )
        )
    return rules


def write_rule_registry(path: Path, rules: list[Rule]) -> None:
    _write_csv(
        path,
        ["rule_id", "rule_name", "rule_version", "status", "risk_typology", "owner",
         "last_reviewed_date"],
        [[r.rule_id, r.rule_name, r.rule_version, r.status, r.risk_typology, r.owner,
          r.last_reviewed_date.isoformat()] for r in rules],
    )


def write_audit_checklist(path: Path) -> None:
    _write_csv(
        path,
        ["control_id", "control_title", "control_family", "regulatory_reference",
         "evidence_requirement"],
        [list(row) for row in CHECKLIST],
    )


def write_rule_control_map(path: Path, rules: list[Rule], rng: random.Random) -> None:
    """Map most (not all) active rules to controls; also leave a stale retired mapping.

    Gaps are intentional: unmapped active rules and unevidenced controls are exactly
    what the audit-readiness tool must surface.
    """
    rows: list[list[object]] = []
    active = [r for r in rules if r.status == "ACTIVE"]
    unmapped_target = 6  # active rules deliberately left off the mapping workbook
    skip = set(rng.sample(range(len(active)), unmapped_target))
    for index, rule in enumerate(active):
        if index in skip:
            continue
        rows.append([rule.rule_id, TYPOLOGY_TO_CONTROL[rule.risk_typology],
                     "control-owner-fraud", "2026-07-15"])
    # Stale mapping: two retired rules still referenced by the workbook.
    for rule in [r for r in rules if r.status == "RETIRED"][:2]:
        rows.append([rule.rule_id, TYPOLOGY_TO_CONTROL[rule.risk_typology],
                     "control-owner-fraud", "2024-02-01"])
    rows.sort(key=lambda row: (str(row[1]), str(row[0])))
    _write_csv(path, ["rule_id", "control_id", "mapped_by", "mapped_date"], rows)


def write_loans(path: Path, period: str, loans_per_month: int, rng: random.Random) -> list[str]:
    """Write the disbursal ledger for one period; return every evaluated application id."""
    first, last = _period_bounds(period)
    span = (last - first).days
    compact = period.replace("-", "")
    total_applications = int(loans_per_month / APPROVAL_RATE)

    rows: list[list[object]] = []
    disbursed = 0
    for n in range(1, total_applications + 1):
        if rng.random() > APPROVAL_RATE:
            continue
        disbursed += 1
        disbursal = first + timedelta(days=rng.randrange(span + 1))
        principal = float(rng.randrange(60, 601) * 25)  # USD 1,500 - 15,000 in 25s
        rows.append([
            f"LN-{compact}-{disbursed:06d}",
            f"APP-{compact}-{n:06d}",
            f"CUST-{rng.randrange(1, 900_000):07d}",
            disbursal.isoformat(),
            f"{principal:.2f}",
            rng.choice(PRODUCTS),
            rng.choice(CHANNELS),
            "DISBURSED",
        ])
    _write_csv(
        path,
        ["loan_id", "application_id", "customer_id", "disbursal_date", "principal_amount",
         "product_code", "origination_channel", "loan_status"],
        rows,
    )
    # Applications that were evaluated but not disbursed still generate rule fires.
    return [f"APP-{compact}-{n:06d}" for n in range(1, total_applications + 1)]


def write_activity(
    out_dir: Path,
    period: str,
    rules: list[Rule],
    application_ids: list[str],
    rng: random.Random,
    as_of: date,
) -> dict[str, set[str]]:
    """Write rule fires, alerts and review outcomes for one period.

    Returns ``rule_id -> set(application_id)`` for fires, which the label generator
    uses to make confirmed fraud correlate with rule activity.
    """
    first, last = _period_bounds(period)
    span = (last - first).days
    compact = period.replace("-", "")

    firing = [r for r in rules if r.status == "ACTIVE"]
    # Change-control defects: shadow and retired rules present in the decision log.
    firing += [r for r in rules if r.status == "SHADOW"][:3]
    firing += [r for r in rules if r.status == "RETIRED"][:2]
    weights = [r.fire_weight for r in firing]

    total_fires = int(len(application_ids) * FIRES_PER_APPLICATION)
    fire_rows: list[list[object]] = []
    alert_rows: list[list[object]] = []
    review_rows: list[list[object]] = []
    fired_apps: dict[str, set[str]] = {}

    alert_seq = 0
    review_seq = 0
    for n in range(1, total_fires + 1):
        rule = rng.choices(firing, weights=weights, k=1)[0]
        application_id = rng.choice(application_ids)
        fired_on = first + timedelta(days=rng.randrange(span + 1))
        fired_at = _ts(fired_on, rng)
        fire_rows.append([
            f"FIR-{compact}-{n:07d}", application_id, rule.rule_id, rule.rule_version, fired_at,
            "REFER" if rule.alert_probability > 0 else "MONITOR",
        ])
        fired_apps.setdefault(rule.rule_id, set()).add(application_id)

        if rng.random() >= rule.alert_probability:
            continue
        alert_seq += 1
        alert_id = f"ALT-{compact}-{alert_seq:06d}"
        reviewer = rng.choice(REVIEWER_IDS)
        alert_rows.append([
            alert_id, application_id, rule.rule_id, fired_at,
            ALERT_QUEUES[alert_seq % len(ALERT_QUEUES)], reviewer,
        ])

        # ~93% of alerts are dispositioned by the snapshot date; the rest stay pending.
        if rng.random() > 0.93:
            continue
        decided_on = min(fired_on + timedelta(days=rng.randrange(0, 12)), as_of)
        roll = rng.random()
        if roll < 0.18:
            outcome = "CONFIRMED_FRAUD"
        elif roll < 0.90:
            outcome = "CLEARED_FALSE_POSITIVE"
        else:
            outcome = "INSUFFICIENT_EVIDENCE"
        review_seq += 1
        review_rows.append([
            f"RVW-{compact}-{review_seq:06d}", alert_id, reviewer, _ts(decided_on, rng), outcome,
            rng.choice(OUTCOME_REASON_CODES[outcome]),
        ])

    # Seeded integrity defects (one of each, so the exception report is non-empty).
    fire_rows.append([
        f"FIR-{compact}-{total_fires + 1:07d}", rng.choice(application_ids), "R0999", "v1.0",
        _ts(last, rng), "REFER",
    ])
    alert_rows.append([
        f"ALT-{compact}-{alert_seq + 1:06d}", "APP-UNKNOWN-000001", "R0999", _ts(last, rng),
        "IDENTITY", REVIEWER_IDS[0],
    ])
    review_rows.append([
        f"RVW-{compact}-{review_seq + 1:06d}", f"ALT-{compact}-999999", REVIEWER_IDS[1],
        _ts(last, rng), "CLEARED_FALSE_POSITIVE", "ID_VERIFIED",
    ])

    _write_csv(
        out_dir / f"rule_fires_{period}.csv",
        ["fire_id", "application_id", "rule_id", "rule_version", "fired_at", "engine_action"],
        fire_rows,
    )
    _write_csv(
        out_dir / f"alerts_{period}.csv",
        ["alert_id", "application_id", "rule_id", "created_at", "queue", "assigned_reviewer_id"],
        alert_rows,
    )
    _write_csv(
        out_dir / f"review_outcomes_{period}.csv",
        ["review_id", "alert_id", "reviewer_id", "decided_at", "outcome", "outcome_reason_code"],
        review_rows,
    )
    return fired_apps


def write_fraud_labels(
    path: Path,
    cohorts: dict[str, list[tuple[str, str, date, float]]],
    fired_apps_by_period: dict[str, dict[str, set[str]]],
    rng: random.Random,
    as_of: date,
) -> None:
    """Write the confirmed-fraud label file (labels land 60-90 days after disbursal).

    Loans whose application triggered a high-signal rule are labelled fraudulent more
    often, so per-rule fraud attribution in the report is not pure noise.
    """
    high_signal: dict[str, set[str]] = {}
    for period, by_rule in fired_apps_by_period.items():
        flagged: set[str] = set()
        for rule_id, apps in by_rule.items():
            # Treat a deterministic subset of rules as genuinely predictive.
            if rule_id[1:].isdigit() and int(rule_id[1:]) % 4 == 0:
                flagged |= apps
        high_signal[period] = flagged

    rows: list[list[object]] = []
    for period, loans in sorted(cohorts.items()):
        flagged = high_signal.get(period, set())
        for loan_id, application_id, disbursal, principal in loans:
            base = 0.011
            probability = base * 6.0 if application_id in flagged else base * 0.75
            if rng.random() >= probability:
                continue
            label_date = disbursal + timedelta(days=rng.randrange(60, 91))
            if label_date > as_of:
                continue  # not yet observable at the snapshot date
            gross = principal * rng.uniform(0.85, 1.0)
            recovery = gross * rng.uniform(0.0, 0.25)
            rows.append([
                loan_id, period, rng.choice(LABEL_TYPES), label_date.isoformat(),
                f"{gross:.2f}", f"{recovery:.2f}", f"{gross - recovery:.2f}",
                rng.choice(LABEL_SOURCES),
            ])
    rows.sort(key=lambda row: str(row[0]))
    _write_csv(
        path,
        ["loan_id", "cohort_period", "label_type", "label_date", "gross_exposure_amount",
         "recovery_amount", "net_loss_amount", "label_source"],
        rows,
    )


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def generate(
    out_dir: Path,
    seed: int = DEFAULT_SEED,
    loans_per_month: int = DEFAULT_LOANS_PER_MONTH,
    cohort_periods: tuple[str, ...] = DEFAULT_COHORT_PERIODS,
    activity_periods: tuple[str, ...] = DEFAULT_ACTIVITY_PERIODS,
    as_of: date = date(2026, 9, 15),
) -> None:
    """Write a complete, deterministic set of source extracts into ``out_dir``."""
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)

    rules = build_rules(rng, as_of)
    write_rule_registry(out_dir / "rule_registry.csv", rules)
    write_audit_checklist(out_dir / "audit_checklist.csv")
    write_rule_control_map(out_dir / "rule_control_map.csv", rules, rng)

    cohorts: dict[str, list[tuple[str, str, date, float]]] = {}
    fired_apps_by_period: dict[str, dict[str, set[str]]] = {}

    for period in cohort_periods:
        loans_path = out_dir / f"loans_{period}.csv"
        application_ids = write_loans(loans_path, period, loans_per_month, rng)

        # Re-read what we just wrote so the label file is built from the file of record.
        with loans_path.open("r", newline="", encoding="utf-8") as handle:
            cohorts[period] = [
                (row["loan_id"], row["application_id"],
                 date.fromisoformat(row["disbursal_date"]), float(row["principal_amount"]))
                for row in csv.DictReader(handle)
            ]

        if period in activity_periods:
            fired_apps_by_period[period] = write_activity(
                out_dir, period, rules, application_ids, rng, as_of
            )

    write_fraud_labels(out_dir / "fraud_labels.csv", cohorts, fired_apps_by_period, rng, as_of)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate synthetic fraud-control source data.")
    parser.add_argument("--out-dir", type=Path, default=Path("data"))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--loans-per-month", type=int, default=DEFAULT_LOANS_PER_MONTH)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 9, 15))
    args = parser.parse_args(argv)

    generate(
        out_dir=args.out_dir,
        seed=args.seed,
        loans_per_month=args.loans_per_month,
        as_of=args.as_of,
    )
    print(f"Wrote synthetic source extracts to {args.out_dir.resolve()}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
