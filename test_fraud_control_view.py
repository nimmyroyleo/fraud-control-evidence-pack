"""Tests for the Audit-Readiness Fraud Control view.

The suite is organised around the four properties the tool is required to hold, plus
the arithmetic that sits underneath them:

* ``TestReadOnly``      - inputs are never touched; writing beside them is refused.
* ``TestIdempotency``   - same period + same as-of + same bytes => identical bytes.
* ``TestTraceability``  - every column and metric resolves to a pinned source.
* ``TestProvisional``   - immature cohorts are flagged, not quietly reported as final.
* ``TestRuleMetrics`` / ``TestControlCoverage`` / ``TestExceptions`` / ``TestFraudLoss``
                        - the aggregation itself, asserted against a hand-computed fixture.

Nearly everything runs against ``tiny_dataset``: a seven-loan, six-rule fixture whose
expected figures were worked out by hand and are written into the assertions. A single
opt-in smoke test exercises the full ~30k-loan synthetic month if it has been generated.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fraud_control_view as fcv  # noqa: E402

PERIOD = "2026-08"
AS_OF = date(2026, 9, 15)
FULL_DATA_DIR = Path(__file__).resolve().parent / "data"


# ======================================================================================
# Fixture: a hand-computable dataset
# ======================================================================================


def _write(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


@pytest.fixture()
def tiny_dataset(tmp_path: Path) -> Path:
    """A six-rule, six-control, seven-loan fixture with every defect class represented.

    Expected figures (worked out by hand, asserted throughout the suite):

    Rules in scope      R001, R002, R003, R004, R005(RETIRED), R006, R999(unregistered)
    In-period fires     8   (R001=4, R002=1, R005=1, R006=1, R999=1; R003/R004 silent)
    In-period alerts    7   (R001=4, R002=1, R006=1, R999=1)
    Dispositions <= as-of 5 (2 confirmed, 2 cleared, 1 inconclusive), 2 pending
    Controls            6, of which 3 EVIDENCED => 50.00%
    2026-08 cohort      4 loans, USD 10,000 principal, 0% label maturity => PROVISIONAL
    2026-05 cohort      2 loans, USD 10,000 principal, 100% matured => FINAL, 400.0 bps
    """
    data = tmp_path / "data"

    _write(
        data / "rule_registry.csv",
        ["rule_id", "rule_name", "rule_version", "status", "risk_typology", "owner",
         "last_reviewed_date"],
        [
            ["R001", "Velocity burst", "v2.1", "ACTIVE", "VELOCITY_ABUSE", "fraud-ops",
             "2026-06-01"],
            ["R002", "Document tamper", "v1.4", "ACTIVE", "DOCUMENT_TAMPERING", "fraud-ops",
             "2026-05-01"],
            # Active but silent this month -> its control cannot be evidenced.
            ["R003", "Device anomaly", "v3.0", "ACTIVE", "DEVICE_ANOMALY", "fraud-ops",
             "2026-07-01"],
            # Active, fires nothing, and absent from the mapping workbook.
            ["R004", "Mule screen", "v1.0", "ACTIVE", "MULE_ACCOUNT", "fraud-strategy",
             "2026-07-01"],
            # Retired, yet present in the decision log -> change-control finding.
            ["R005", "Legacy PII mismatch", "v6.2", "RETIRED", "PII_MISMATCH", "fraud-ops",
             "2023-01-01"],
            # Active but last reviewed > 365 days ago -> governance finding.
            ["R006", "Geo anomaly", "v2.0", "ACTIVE", "GEO_ANOMALY", "fraud-ops",
             "2024-01-01"],
        ],
    )

    _write(
        data / "audit_checklist.csv",
        ["control_id", "control_title", "control_family", "regulatory_reference",
         "evidence_requirement"],
        [
            ["AC-01", "Velocity is monitored", "Transaction Monitoring", "REG-A",
             "Rule fire log"],
            ["AC-02", "Documents are checked", "Identity Verification", "REG-B",
             "Rule fire log"],
            ["AC-03", "Device anomalies are monitored", "Transaction Monitoring", "REG-C",
             "Rule fire log"],
            ["AC-04", "PII mismatches are investigated", "Identity Verification", "REG-D",
             "Rule fire log"],
            ["AC-05", "Geolocation is evaluated", "Transaction Monitoring", "REG-E",
             "Rule fire log"],
            ["AC-06", "Reviewer QA sampling", "Case Management", "REG-F", "QA sample"],
        ],
    )

    _write(
        data / "rule_control_map.csv",
        ["rule_id", "control_id", "mapped_by", "mapped_date"],
        [
            ["R001", "AC-01", "control-owner", "2026-07-15"],
            ["R002", "AC-02", "control-owner", "2026-07-15"],
            ["R003", "AC-03", "control-owner", "2026-07-15"],
            ["R005", "AC-04", "control-owner", "2024-02-01"],  # stale: rule is RETIRED
            ["R006", "AC-05", "control-owner", "2026-07-15"],
            # AC-06 is mapped to nothing at all.
        ],
    )

    _write(
        data / "loans_2026-08.csv",
        ["loan_id", "application_id", "customer_id", "disbursal_date", "principal_amount",
         "product_code", "origination_channel", "loan_status"],
        [
            ["L1", "APP1", "C1", "2026-08-02", "1000.00", "PL_SMALL_12M", "MOBILE_APP",
             "DISBURSED"],
            ["L2", "APP2", "C2", "2026-08-05", "2000.00", "PL_SMALL_12M", "WEB_DIRECT",
             "DISBURSED"],
            ["L3", "APP3", "C3", "2026-08-09", "3000.00", "PL_SMALL_24M", "AGGREGATOR",
             "DISBURSED"],
            ["L4", "APP4", "C4", "2026-08-20", "4000.00", "PL_SMALL_24M", "PARTNER_API",
             "DISBURSED"],
            # Disbursed in July but present in the August extract -> excluded.
            ["L9", "APP9", "C9", "2026-07-28", "9000.00", "PL_SMALL_12M", "MOBILE_APP",
             "DISBURSED"],
        ],
    )

    _write(
        data / "loans_2026-05.csv",
        ["loan_id", "application_id", "customer_id", "disbursal_date", "principal_amount",
         "product_code", "origination_channel", "loan_status"],
        [
            ["L5", "APP5", "C5", "2026-05-02", "5000.00", "PL_SMALL_12M", "MOBILE_APP",
             "DISBURSED"],
            ["L6", "APP6", "C6", "2026-05-03", "5000.00", "PL_SMALL_12M", "WEB_DIRECT",
             "DISBURSED"],
        ],
    )

    # Minimal activity for the matured May cohort, so the tool can also be run against a
    # fully labelled month (the FINAL path) from the same fixture.
    _write(
        data / "rule_fires_2026-05.csv",
        ["fire_id", "application_id", "rule_id", "rule_version", "fired_at", "engine_action"],
        [
            ["F51", "APP5", "R001", "v2.1", "2026-05-02T10:00:00", "REFER"],
            ["F52", "APP6", "R002", "v1.4", "2026-05-03T10:00:00", "REFER"],
        ],
    )
    _write(
        data / "alerts_2026-05.csv",
        ["alert_id", "application_id", "rule_id", "created_at", "queue",
         "assigned_reviewer_id"],
        [["A51", "APP5", "R001", "2026-05-02T10:05:00", "IDENTITY", "RV-01"]],
    )
    _write(
        data / "review_outcomes_2026-05.csv",
        ["review_id", "alert_id", "reviewer_id", "decided_at", "outcome",
         "outcome_reason_code"],
        [["V51", "A51", "RV-01", "2026-05-05T09:00:00", "CONFIRMED_FRAUD", "SYNTH_ID"]],
    )

    _write(
        data / "rule_fires_2026-08.csv",
        ["fire_id", "application_id", "rule_id", "rule_version", "fired_at", "engine_action"],
        [
            ["F1", "APP1", "R001", "v2.1", "2026-08-02T10:00:00", "REFER"],
            ["F2", "APP2", "R001", "v2.1", "2026-08-03T10:00:00", "REFER"],
            ["F3", "APP3", "R001", "v2.1", "2026-08-04T10:00:00", "REFER"],
            ["F4", "APP1", "R002", "v1.4", "2026-08-02T11:00:00", "REFER"],
            ["F5", "APP4", "R006", "v2.0", "2026-08-20T09:00:00", "REFER"],
            ["F6", "APP2", "R005", "v6.2", "2026-08-06T09:00:00", "MONITOR"],
            ["F7", "APP3", "R999", "v1.0", "2026-08-07T09:00:00", "REFER"],
            ["F8", "APP7", "R001", "v2.1", "2026-08-05T10:00:00", "REFER"],
            ["F9", "APP1", "R001", "v2.1", "2026-07-31T23:00:00", "REFER"],  # out of period
            ["F1", "APP4", "R001", "v2.1", "2026-08-08T10:00:00", "REFER"],  # duplicate id
        ],
    )

    _write(
        data / "alerts_2026-08.csv",
        ["alert_id", "application_id", "rule_id", "created_at", "queue",
         "assigned_reviewer_id"],
        [
            ["A1", "APP1", "R001", "2026-08-02T10:05:00", "IDENTITY", "RV-01"],
            ["A2", "APP2", "R001", "2026-08-03T10:05:00", "IDENTITY", "RV-02"],
            ["A3", "APP3", "R001", "2026-08-04T10:05:00", "IDENTITY", "RV-03"],
            ["A4", "APP1", "R002", "2026-08-02T11:05:00", "DOCUMENT", "RV-01"],
            ["A5", "APP4", "R006", "2026-08-20T09:05:00", "HIGH_VALUE", "RV-02"],
            # No (APP8, R001) row in the fire log -> unreconcilable alert, and old enough
            # at the as-of date to breach the pending-alert SLA.
            ["A6", "APP8", "R001", "2026-08-09T10:00:00", "IDENTITY", "RV-01"],
            ["A7", "APP3", "R999", "2026-08-07T09:05:00", "IDENTITY", "RV-05"],
            ["A8", "APP1", "R001", "2026-07-15T10:00:00", "IDENTITY", "RV-01"],  # out of period
        ],
    )

    _write(
        data / "review_outcomes_2026-08.csv",
        ["review_id", "alert_id", "reviewer_id", "decided_at", "outcome",
         "outcome_reason_code"],
        [
            ["V1", "A1", "RV-01", "2026-08-05T09:00:00", "CONFIRMED_FRAUD", "SYNTH_ID"],
            ["V2", "A2", "RV-02", "2026-08-06T09:00:00", "CLEARED_FALSE_POSITIVE", "ID_OK"],
            ["V3", "A3", "RV-03", "2026-08-07T09:00:00", "INSUFFICIENT_EVIDENCE", "NO_CONTACT"],
            ["V4", "A4", "RV-01", "2026-08-08T09:00:00", "CLEARED_FALSE_POSITIVE", "DOC_OK"],
            # Decided after the snapshot -> excluded, so A5 stays pending.
            ["V5", "A5", "RV-02", "2026-09-20T09:00:00", "CONFIRMED_FRAUD", "ATO"],
            ["V6", "A99", "RV-04", "2026-08-09T09:00:00", "CLEARED_FALSE_POSITIVE", "ID_OK"],
            ["V7", "A7", "RV-05", "2026-08-10T09:00:00", "CONFIRMED_FRAUD", "DOC_FORGED"],
        ],
    )

    _write(
        data / "fraud_labels.csv",
        ["loan_id", "cohort_period", "label_type", "label_date", "gross_exposure_amount",
         "recovery_amount", "net_loss_amount", "label_source"],
        [
            # Early confirmation on an August loan - real, but rare; most land at T+60..90.
            ["L1", "2026-08", "IDENTITY_THEFT", "2026-09-01", "300.00", "50.00", "250.00",
             "MANUAL_INVESTIGATION"],
            ["L5", "2026-05", "FIRST_PARTY_FRAUD", "2026-08-01", "500.00", "100.00", "400.00",
             "CHARGEOFF_FILE"],
            # Not yet observable at the as-of date -> excluded.
            ["L4", "2026-08", "FIRST_PARTY_FRAUD", "2026-11-01", "900.00", "0.00", "900.00",
             "CHARGEOFF_FILE"],
            # References a loan that exists in no ledger -> integrity exception.
            ["L99", "2026-08", "FIRST_PARTY_FRAUD", "2026-09-02", "100.00", "0.00", "100.00",
             "SAR_FILED"],
        ],
    )

    return data


@pytest.fixture()
def params(tiny_dataset: Path, tmp_path: Path) -> fcv.RunParams:
    return fcv.RunParams(
        period=PERIOD,
        as_of=AS_OF,
        input_dir=tiny_dataset,
        output_dir=tmp_path / "out",
        audit_date=date(2026, 10, 28),
    )


@pytest.fixture()
def report(params: fcv.RunParams) -> fcv.Report:
    return fcv.run(params)


def rule(report: fcv.Report, rule_id: str) -> fcv.RuleControlRow:
    row = next((item for item in report.rule_rows if item.rule_id == rule_id), None)
    assert row is not None, f"{rule_id} missing from control_view"
    return row


def control(report: fcv.Report, control_id: str) -> fcv.ControlCoverageRow:
    row = next((item for item in report.control_rows if item.control_id == control_id), None)
    assert row is not None, f"{control_id} missing from control_coverage"
    return row


def exception_codes(report: fcv.Report) -> set[str]:
    return {row.exception_code for row in report.exception_rows}


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def hash_tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


# ======================================================================================
# Numeric helpers
# ======================================================================================


class TestHelpers:
    def test_pct_rounds_to_two_places(self):
        assert fcv.pct(1, 3) == "33.33"
        assert fcv.pct(1, 2) == "50.00"

    def test_undefined_ratio_is_blank_not_zero(self):
        """A blank denominator must never be reported as 0 - they mean different things."""
        assert fcv.pct(0, 0) == ""
        assert fcv.bps(5, 0) == ""

    def test_bps(self):
        assert fcv.bps(400, 10_000) == "400.0"

    def test_period_bounds(self):
        assert fcv.period_bounds("2026-08") == (date(2026, 8, 1), date(2026, 8, 31))
        assert fcv.period_bounds("2026-12") == (date(2026, 12, 1), date(2026, 12, 31))
        assert fcv.period_bounds("2024-02") == (date(2024, 2, 1), date(2024, 2, 29))

    @pytest.mark.parametrize("bad", ["2026-8", "202608", "2026-13", "August"])
    def test_period_bounds_rejects_malformed(self, bad: str):
        with pytest.raises(ValueError):
            fcv.period_bounds(bad)

    def test_shift_period_crosses_year_boundary(self):
        assert fcv.shift_period("2026-01", -1) == "2025-12"
        assert fcv.shift_period("2026-12", 1) == "2027-01"
        assert fcv.shift_period("2026-08", -3) == "2026-05"

    def test_parse_date_handles_timestamps_and_junk(self):
        assert fcv.parse_date("2026-08-02T10:00:00") == date(2026, 8, 2)
        assert fcv.parse_date("") is None
        assert fcv.parse_date("not-a-date") is None


# ======================================================================================
# Requirement 1: read-only
# ======================================================================================


class TestReadOnly:
    def test_inputs_are_byte_identical_after_a_run(self, params: fcv.RunParams):
        before = hash_tree(params.input_dir)
        fcv.run(params)
        assert hash_tree(params.input_dir) == before

    def test_no_files_are_added_to_the_input_directory(self, params: fcv.RunParams):
        before = sorted(path.name for path in params.input_dir.iterdir())
        fcv.run(params)
        assert sorted(path.name for path in params.input_dir.iterdir()) == before

    def test_writing_into_the_input_directory_is_refused(self, tiny_dataset: Path):
        bad = fcv.RunParams(period=PERIOD, as_of=AS_OF, input_dir=tiny_dataset,
                            output_dir=tiny_dataset / "out")
        with pytest.raises(fcv.ReadOnlyViolationError):
            fcv.run(bad)

    def test_output_equal_to_input_directory_is_refused(self, tiny_dataset: Path):
        bad = fcv.RunParams(period=PERIOD, as_of=AS_OF, input_dir=tiny_dataset,
                            output_dir=tiny_dataset)
        with pytest.raises(fcv.ReadOnlyViolationError):
            fcv.run(bad)

    def test_input_changing_mid_run_is_detected(self, params: fcv.RunParams):
        """The end-of-run digest re-check is what makes the snapshot claim defensible."""
        sources = fcv.load_sources(params)
        digests = sources.digests()
        registry = params.input_dir / "rule_registry.csv"
        registry.write_text(registry.read_text(encoding="utf-8") +
                            "R007,Late addition,v1.0,ACTIVE,VELOCITY_ABUSE,fraud-ops,"
                            "2026-07-01\n", encoding="utf-8")
        with pytest.raises(fcv.ReadOnlyViolationError):
            fcv.verify_inputs_unchanged(sources, digests)

    def test_manifest_records_the_read_only_verification(self, report: fcv.Report):
        manifest = json.loads((report.params.run_dir() / "manifest.json").read_text("utf-8"))
        assert manifest["tool"]["mode"] == "READ_ONLY"
        assert manifest["tool"]["makes_credit_decisions"] is False
        assert manifest["read_only_verification"]["inputs_unchanged"] is True

    def test_module_exposes_no_decisioning_entry_point(self):
        """Guards the 'no credit decisions' constraint against future drift."""
        banned = ("approve", "decline", "adjudicate", "score_applicant", "underwrite",
                  "set_limit", "refer_application")
        names = [name.lower() for name in dir(fcv)]
        assert not [name for name in names if any(token in name for token in banned)]


# ======================================================================================
# Requirement 2: monthly idempotency
# ======================================================================================


class TestIdempotency:
    def test_reruns_produce_byte_identical_outputs(self, tiny_dataset: Path, tmp_path: Path):
        first = fcv.RunParams(period=PERIOD, as_of=AS_OF, input_dir=tiny_dataset,
                              output_dir=tmp_path / "run1")
        second = fcv.RunParams(period=PERIOD, as_of=AS_OF, input_dir=tiny_dataset,
                               output_dir=tmp_path / "run2")
        fcv.run(first)
        fcv.run(second)

        left = {name: digest for name, digest in hash_tree(first.run_dir()).items()
                if name != "manifest.json"}
        right = {name: digest for name, digest in hash_tree(second.run_dir()).items()
                 if name != "manifest.json"}
        assert left == right
        assert len(left) == 9  # 8 CSVs + the HTML pack

    def test_rerunning_in_place_does_not_change_the_payload_digest(self,
                                                                   params: fcv.RunParams):
        fcv.run(params)
        manifest_path = params.run_dir() / "manifest.json"
        first = json.loads(manifest_path.read_text("utf-8"))
        fcv.run(params)
        second = json.loads(manifest_path.read_text("utf-8"))
        assert (first["determinism"]["deterministic_payload_sha256"]
                == second["determinism"]["deterministic_payload_sha256"])
        assert first["outputs"] == second["outputs"]

    def test_run_timestamp_stays_out_of_the_hashed_payload(self, params: fcv.RunParams):
        """Only the manifest may carry wall-clock time, and it must not leak into the pack."""
        report = fcv.run(params)
        manifest = json.loads((params.run_dir() / "manifest.json").read_text("utf-8"))
        timestamp = manifest["run"]["run_timestamp_utc"]
        assert timestamp
        for path in params.run_dir().iterdir():
            if path.name == "manifest.json":
                continue
            assert timestamp not in path.read_text("utf-8"), f"{path.name} embeds a timestamp"
        assert report.params.period == PERIOD

    def test_dispositions_after_the_as_of_date_are_excluded(self, report: fcv.Report):
        """V5 is dated after the snapshot, so A5 must still read as pending."""
        r006 = rule(report, "R006")
        assert r006.reviews_decided == 0
        assert r006.outcome_pending == 1
        assert "E-REVIEW_AFTER_SNAPSHOT" in exception_codes(report)

    def test_a_later_as_of_date_admits_the_later_disposition(self, tiny_dataset: Path,
                                                             tmp_path: Path):
        later = fcv.RunParams(period=PERIOD, as_of=date(2026, 9, 30), input_dir=tiny_dataset,
                              output_dir=tmp_path / "later")
        r006 = rule(fcv.run(later), "R006")
        assert r006.reviews_decided == 1
        assert r006.outcome_confirmed_fraud == 1
        assert r006.outcome_pending == 0

    def test_rows_outside_the_reporting_period_are_excluded_and_flagged(self,
                                                                        report: fcv.Report):
        codes = exception_codes(report)
        assert "E-FIRE_OUTSIDE_REPORTING_PERIOD" in codes
        assert "E-ALERT_OUTSIDE_REPORTING_PERIOD" in codes
        assert "E-LOAN_OUTSIDE_REPORTING_PERIOD" in codes
        assert report.summary_value("loans_disbursed") == "4"  # L9 (July) excluded
        assert report.summary_value("rule_fires_total") == "8"  # F9 excluded, F1 dup dropped


# ======================================================================================
# Requirement 3: traceability
# ======================================================================================


class TestTraceability:
    def test_lineage_covers_every_emitted_column_exactly(self, report: fcv.Report):
        """Schema and lineage cannot drift apart without this test failing."""
        documented: dict[str, set[str]] = {}
        for row in report.lineage_rows:
            documented.setdefault(row.output_file, set()).add(row.output_column)

        for filename, attribute in fcv.CSV_OUTPUTS:
            emitted = set(fcv.columns_for(fcv.ROW_TYPES[attribute]))
            assert filename in documented, f"{filename} has no lineage rows"
            assert documented[filename] == emitted, (
                f"{filename} lineage drift: "
                f"undocumented={sorted(emitted - documented[filename])}, "
                f"stale={sorted(documented[filename] - emitted)}"
            )
        assert set(documented) == {filename for filename, _ in fcv.CSV_OUTPUTS}

    def test_every_csv_row_names_its_source_files(self, report: fcv.Report):
        for attribute in ("rule_rows", "control_rows", "unmapped_rows", "exception_rows",
                          "loss_rows", "summary_rows"):
            for row in getattr(report, attribute):
                assert row.source_files, f"{attribute} row without source_files: {row}"

    def test_source_names_resolve_to_a_manifest_entry(self, report: fcv.Report):
        manifest = json.loads((report.params.run_dir() / "manifest.json").read_text("utf-8"))
        known = {entry["logical_name"] for entry in manifest["inputs"]}
        # Pseudo-sources for values that are parameters, derived, or re-read from a
        # sibling output file rather than a source extract.
        known |= {"run_parameter", "derived", "manifest.json", "see computation", "-"}
        known |= {filename for filename, _ in fcv.CSV_OUTPUTS}
        for row in report.summary_rows + report.lineage_rows:
            for name in row.source_files.split("|"):
                assert name.strip() in known, f"unresolvable source {name!r} on {row}"

    def test_manifest_pins_every_input_with_a_digest_and_row_count(self,
                                                                   report: fcv.Report):
        manifest = json.loads((report.params.run_dir() / "manifest.json").read_text("utf-8"))
        loaded = [entry for entry in manifest["inputs"] if entry["status"] == "LOADED"]
        assert len(loaded) == 9  # 3 static + 4 period extracts + labels + 1 baseline cohort
        for entry in loaded:
            assert len(entry["sha256"]) == 64
            assert entry["row_count"] >= 0
            assert entry["columns"]
            on_disk = hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest()
            assert entry["sha256"] == on_disk

    def test_manifest_records_missing_optional_cohorts(self, report: fcv.Report):
        manifest = json.loads((report.params.run_dir() / "manifest.json").read_text("utf-8"))
        missing = [entry for entry in manifest["inputs"] if entry["status"] == "MISSING"]
        # 2026-06 and 2026-07 cohorts were never written, and must be recorded as looked-for.
        assert {entry["logical_name"] for entry in missing} == {
            "loans_baseline_2026-06", "loans_baseline_2026-07"}
        assert all(entry["required"] is False for entry in missing)

    def test_manifest_row_counts_match_the_csvs(self, report: fcv.Report):
        manifest = json.loads((report.params.run_dir() / "manifest.json").read_text("utf-8"))
        for filename, count in manifest["row_counts"].items():
            assert len(read_csv_rows(report.params.run_dir() / filename)) == count

    def test_every_summary_metric_states_its_computation(self, report: fcv.Report):
        for row in report.summary_rows:
            assert row.computation, f"{row.metric_id} has no computation"
            assert row.unit
            assert row.label_status in (fcv.LABEL_FINAL, fcv.LABEL_PROVISIONAL,
                                        fcv.LABEL_NOT_APPLICABLE)

    def test_summary_metric_ids_are_unique(self, report: fcv.Report):
        ids = [row.metric_id for row in report.summary_rows]
        assert len(ids) == len(set(ids))

    def test_missing_required_source_fails_loudly(self, params: fcv.RunParams):
        (params.input_dir / "alerts_2026-08.csv").unlink()
        with pytest.raises(fcv.SourceDataError, match="required source extract not found"):
            fcv.run(params)

    def test_source_with_missing_column_fails_loudly(self, params: fcv.RunParams):
        _write(params.input_dir / "rule_registry.csv", ["rule_id", "rule_name"],
               [["R001", "Velocity burst"]])
        with pytest.raises(fcv.SourceDataError, match="missing required column"):
            fcv.run(params)


# ======================================================================================
# Requirement 4: provisional fraud metrics
# ======================================================================================


class TestProvisional:
    def test_immature_reporting_period_is_marked_provisional(self, report: fcv.Report):
        assert report.summary_value("reporting_period_label_maturity_pct") == "0.00"
        loss = report.summary("reporting_period_net_loss_rate_bps")
        assert loss.label_status == fcv.LABEL_PROVISIONAL
        assert all(row.fraud_label_status == fcv.LABEL_PROVISIONAL
                   for row in report.rule_rows)
        assert "E-LABEL_COHORT_IMMATURE" in exception_codes(report)

    def test_matured_period_is_marked_final(self, tiny_dataset: Path, tmp_path: Path):
        matured = fcv.RunParams(period="2026-05", as_of=AS_OF, input_dir=tiny_dataset,
                                output_dir=tmp_path / "may")
        result = fcv.run(matured)
        assert result.summary_value("reporting_period_label_maturity_pct") == "100.00"
        assert result.summary("reporting_period_net_loss_rate_bps").label_status == \
            fcv.LABEL_FINAL
        assert "E-LABEL_COHORT_IMMATURE" not in exception_codes(result)

    @pytest.mark.parametrize("window_days, expected_maturity", [
        (90, "0.00"),    # the documented window: nothing in August has matured
        (30, "75.00"),   # L4 (disbursed 2026-08-20) is only 26 days old at the as-of date
        (20, "100.00"),  # every August loan clears a 20-day window
    ])
    def test_maturity_follows_the_label_window_parameter(self, tiny_dataset: Path,
                                                         tmp_path: Path, window_days: int,
                                                         expected_maturity: str):
        run_params = fcv.RunParams(
            period=PERIOD, as_of=AS_OF, input_dir=tiny_dataset,
            output_dir=tmp_path / f"window{window_days}", label_maturity_days=window_days)
        result = fcv.run(run_params)
        assert result.summary_value("reporting_period_label_maturity_pct") == expected_maturity
        assert result.summary("reporting_period_net_loss_rate_bps").label_status == (
            fcv.LABEL_FINAL if expected_maturity == "100.00" else fcv.LABEL_PROVISIONAL)

    def test_labels_dated_after_the_as_of_date_are_excluded(self, report: fcv.Report):
        """L4's label lands in November; it must not appear in a September snapshot."""
        assert report.summary_value("reporting_period_confirmed_fraud_loans") == "1"
        assert report.summary_value("reporting_period_net_loss_usd") == "250.00"

    def test_provisional_html_banner_is_present(self, report: fcv.Report):
        pack = (report.params.run_dir() / "evidence_pack.html").read_text("utf-8")
        assert "PROVISIONAL" in pack
        assert "60&#8211;90 days after" in pack or "60-90 days after" in pack


# ======================================================================================
# Per-rule aggregation
# ======================================================================================


class TestRuleMetrics:
    def test_scope_is_active_rules_plus_anything_observed_firing(self, report: fcv.Report):
        assert {row.rule_id for row in report.rule_rows} == {
            "R001", "R002", "R003", "R004", "R005", "R006", "R999"}

    def test_fire_counts_exclude_duplicates_and_out_of_period_rows(self,
                                                                   report: fcv.Report):
        r001 = rule(report, "R001")
        assert r001.fire_count == 4           # F1, F2, F3, F8 (F9 out of period, F1 dup)
        assert r001.distinct_applications_fired == 4
        assert r001.share_of_period_fires_pct == "50.00"  # 4 of 8

    def test_alert_volume_and_rate(self, report: fcv.Report):
        r001 = rule(report, "R001")
        assert r001.alert_count == 4
        assert r001.alert_rate_pct == "100.00"
        assert rule(report, "R005").alert_count == 0
        assert rule(report, "R005").alert_rate_pct == "0.00"

    def test_outcome_breakdown_and_pending(self, report: fcv.Report):
        r001 = rule(report, "R001")
        assert (r001.outcome_confirmed_fraud, r001.outcome_cleared_false_positive,
                r001.outcome_insufficient_evidence) == (1, 1, 1)
        assert r001.reviews_decided == 3
        assert r001.outcome_pending == 1      # A6 never dispositioned
        assert r001.review_coverage_pct == "75.00"

    def test_precision_excludes_inconclusive_from_the_denominator(self,
                                                                  report: fcv.Report):
        # R001: 1 confirmed, 1 cleared, 1 inconclusive -> 1 / (1 + 1) = 50%
        assert rule(report, "R001").alert_precision_pct == "50.00"
        assert rule(report, "R002").alert_precision_pct == "0.00"

    def test_precision_is_blank_when_nothing_was_decided(self, report: fcv.Report):
        r006 = rule(report, "R006")
        assert r006.alert_precision_pct == ""
        assert r006.review_coverage_pct == "0.00"
        assert rule(report, "R005").review_coverage_pct == ""  # no alerts at all

    def test_distinct_reviewers_per_rule(self, report: fcv.Report):
        assert rule(report, "R001").distinct_reviewers == 3   # RV-01, RV-02, RV-03
        assert rule(report, "R002").distinct_reviewers == 1

    def test_loan_and_fraud_attribution(self, report: fcv.Report):
        r001 = rule(report, "R001")
        # Fired on APP1..APP3 and APP7; only APP1-3 were disbursed this period.
        assert r001.linked_disbursed_loans == 3
        assert r001.linked_confirmed_fraud_loans == 1          # L1
        assert r001.linked_confirmed_fraud_net_loss_usd == "250.00"

    def test_attribution_is_non_exclusive_across_rules(self, report: fcv.Report):
        """L1 is attributed to both rules that fired on APP1 - documented, not a bug."""
        assert rule(report, "R001").linked_confirmed_fraud_loans == 1
        assert rule(report, "R002").linked_confirmed_fraud_loans == 1
        attributed = sum(row.linked_confirmed_fraud_loans for row in report.rule_rows)
        assert attributed > int(report.summary_value(
            "reporting_period_confirmed_fraud_loans"))

    def test_rows_are_sorted_by_fire_count_then_rule_id(self, report: fcv.Report):
        keys = [(-row.fire_count, row.rule_id) for row in report.rule_rows]
        assert keys == sorted(keys)

    def test_rule_evidence_status(self, report: fcv.Report):
        assert rule(report, "R001").rule_evidence_status == fcv.EV_EVIDENCED
        assert rule(report, "R003").rule_evidence_status == fcv.EV_NO_ACTIVITY
        assert rule(report, "R005").rule_evidence_status == fcv.LABEL_NOT_APPLICABLE

    def test_control_context_is_carried_on_the_rule_row(self, report: fcv.Report):
        r001 = rule(report, "R001")
        assert r001.control_ids == "AC-01"
        assert r001.control_titles == "Velocity is monitored"
        assert r001.regulatory_references == "REG-A"
        assert r001.mapping_status == fcv.MAP_MAPPED

    def test_days_since_rule_review_is_computed_from_the_as_of_date(self,
                                                                    report: fcv.Report):
        assert rule(report, "R001").days_since_rule_review == "106"  # 2026-06-01 -> 2026-09-15


# ======================================================================================
# Control coverage and the headline percentage
# ======================================================================================


class TestControlCoverage:
    def test_one_row_per_checklist_control(self, report: fcv.Report):
        assert [row.control_id for row in report.control_rows] == [
            "AC-01", "AC-02", "AC-03", "AC-04", "AC-05", "AC-06"]

    def test_evidence_status_per_control(self, report: fcv.Report):
        assert control(report, "AC-01").evidence_status == fcv.EV_EVIDENCED
        assert control(report, "AC-03").evidence_status == fcv.EV_NO_ACTIVITY
        assert control(report, "AC-04").evidence_status == fcv.EV_NO_ACTIVE_RULE
        assert control(report, "AC-06").evidence_status == fcv.EV_UNMAPPED

    def test_headline_percentage(self, report: fcv.Report):
        assert report.summary_value("controls_total") == "6"
        assert report.summary_value("controls_evidenced") == "3"   # AC-01, AC-02, AC-05
        assert report.summary_value("controls_evidenced_pct") == "50.00"

    def test_control_rollups_sum_their_mapped_active_rules(self, report: fcv.Report):
        ac01 = control(report, "AC-01")
        assert (ac01.mapped_rules_total, ac01.mapped_rules_active) == (1, 1)
        assert (ac01.fire_count, ac01.alert_count) == (4, 4)
        assert ac01.reviews_decided == 3
        assert ac01.confirmed_fraud_reviews == 1
        assert ac01.manual_review_evidence == "Y"

    def test_stale_mapping_to_a_retired_rule_is_visible(self, report: fcv.Report):
        ac04 = control(report, "AC-04")
        assert (ac04.mapped_rules_active, ac04.mapped_rules_non_active) == (0, 1)
        assert "E-MAPPING_TO_NON_ACTIVE_RULE" in exception_codes(report)

    def test_unevidenced_controls_carry_a_remediation_action(self, report: fcv.Report):
        for row in report.control_rows:
            if row.evidence_status == fcv.EV_EVIDENCED:
                assert row.remediation_action == ""
            else:
                assert row.remediation_action

    def test_evidence_threshold_is_configurable(self, tiny_dataset: Path, tmp_path: Path):
        """Raising the bar to 2 fires drops the single-fire controls out of coverage."""
        strict = fcv.RunParams(period=PERIOD, as_of=AS_OF, input_dir=tiny_dataset,
                               output_dir=tmp_path / "strict", min_fires_for_evidence=2)
        result = fcv.run(strict)
        assert result.summary_value("controls_evidenced") == "1"   # only AC-01 (4 fires)
        assert result.summary_value("controls_evidenced_pct") == "16.67"


# ======================================================================================
# Unmapped rules and exceptions
# ======================================================================================


class TestUnmappedRules:
    def test_unmapped_active_rule_is_reported(self, report: fcv.Report):
        gaps = {row.rule_id: row for row in report.unmapped_rows}
        assert "R004" in gaps
        assert gaps["R004"].gap_type == fcv.MAP_UNMAPPED_ACTIVE
        assert gaps["R004"].audit_risk == "HIGH"
        assert gaps["R004"].recommended_action
        assert gaps["R004"].rule_owner == "fraud-strategy"  # the remediation owner

    def test_unregistered_firing_rule_is_the_top_gap(self, report: fcv.Report):
        gaps = {row.rule_id: row for row in report.unmapped_rows}
        assert gaps["R999"].gap_type == "UNREGISTERED_RULE_FIRING"
        assert gaps["R999"].audit_risk == "CRITICAL"
        assert gaps["R999"].fire_count == 1

    def test_mapped_rules_are_not_listed_as_gaps(self, report: fcv.Report):
        """R005 is retired but *is* mapped, so it is a change-control finding, not a
        mapping gap. The two reports must not double-count the same rule."""
        assert {"R001", "R002", "R003", "R005", "R006"}.isdisjoint(
            {row.rule_id for row in report.unmapped_rows})
        assert "E-NON_ACTIVE_RULE_FIRING" in exception_codes(report)

    def test_unmapped_non_active_firing_rule_is_reported(self, params: fcv.RunParams):
        """Drop R005's mapping: it is then both non-active and unmapped, and firing."""
        mapping = params.input_dir / "rule_control_map.csv"
        mapping.write_text(
            "\n".join(line for line in mapping.read_text("utf-8").splitlines()
                      if not line.startswith("R005,")) + "\n", encoding="utf-8")
        gaps = {row.rule_id: row for row in fcv.run(params).unmapped_rows}
        assert gaps["R005"].gap_type == "NON_ACTIVE_RULE_FIRING"
        assert gaps["R005"].audit_risk == "HIGH"
        assert gaps["R005"].rule_status == "RETIRED"

    def test_gaps_are_ordered_by_audit_risk(self, report: fcv.Report):
        risks = [fcv.SEVERITY_ORDER[row.audit_risk] for row in report.unmapped_rows]
        assert risks == sorted(risks)

    def test_summary_counts_match_the_gap_file(self, report: fcv.Report):
        assert report.summary_value("active_rules_unmapped") == "1"       # R004
        assert report.summary_value("active_rules_mapped") == "4"         # R001-R003, R006
        assert report.summary_value("active_rules_mapped_pct") == "80.00"
        assert report.summary_value("unmapped_rule_gaps") == str(len(report.unmapped_rows))


class TestExceptions:
    def test_every_seeded_defect_class_is_detected(self, report: fcv.Report):
        assert {
            "E-UNREGISTERED_RULE_FIRING",
            "E-NON_ACTIVE_RULE_FIRING",
            "E-UNMAPPED_ACTIVE_RULE",
            "E-CONTROL_NOT_EVIDENCED",
            "E-MAPPING_TO_NON_ACTIVE_RULE",
            "E-ACTIVE_RULE_NO_FIRES",
            "E-STALE_RULE_GOVERNANCE_REVIEW",
            "E-DUPLICATE_FIRE_ID",
            "E-FIRE_OUTSIDE_REPORTING_PERIOD",
            "E-ALERT_OUTSIDE_REPORTING_PERIOD",
            "E-ALERT_WITHOUT_MATCHING_FIRE",
            "E-ALERT_RULE_NOT_IN_REGISTRY",
            "E-ALERT_PENDING_BEYOND_SLA",
            "E-REVIEW_WITHOUT_ALERT",
            "E-REVIEW_AFTER_SNAPSHOT",
            "E-REVIEWER_ROSTER_DRIFT",
            "E-LABEL_LOAN_NOT_IN_LEDGER",
            "E-LABEL_COHORT_IMMATURE",
            "E-LOAN_OUTSIDE_REPORTING_PERIOD",
        } <= exception_codes(report)

    def test_unregistered_rule_is_the_critical_finding(self, report: fcv.Report):
        critical = [row for row in report.exception_rows if row.severity == "CRITICAL"]
        assert [row.exception_code for row in critical] == ["E-UNREGISTERED_RULE_FIRING"]
        assert critical[0].entity_id == "R999"

    def test_exceptions_are_sorted_by_severity_with_stable_ids(self, report: fcv.Report):
        ranks = [fcv.SEVERITY_ORDER[row.severity] for row in report.exception_rows]
        assert ranks == sorted(ranks)
        assert [row.exception_id for row in report.exception_rows] == [
            f"EX-{index:04d}" for index in range(1, len(report.exception_rows) + 1)]

    def test_record_level_defects_are_aggregated_not_enumerated(self, report: fcv.Report):
        """Keeps the exception report bounded when a whole feed is malformed."""
        row = next(item for item in report.exception_rows
                   if item.exception_code == "E-ALERT_WITHOUT_MATCHING_FIRE")
        assert row.entity_id == "n=1"
        assert "A6" in row.description

    def test_every_exception_names_the_check_that_raised_it(self, report: fcv.Report):
        for row in report.exception_rows:
            assert row.detected_by_check
            assert row.description
            assert row.severity in fcv.SEVERITY_ORDER

    def test_severity_counts_reconcile_with_the_exception_file(self, report: fcv.Report):
        total = sum(int(report.summary_value(f"exceptions_{severity.lower()}"))
                    for severity in fcv.SEVERITY_ORDER)
        assert total == len(report.exception_rows)
        assert report.summary_value("exceptions_total") == str(len(report.exception_rows))

    def test_reviewer_roster_drift_against_the_documented_team(self, report: fcv.Report):
        # RV-01, RV-02, RV-03, RV-05 dispositioned alerts; the narrative documents six.
        assert report.summary_value("distinct_reviewers") == "4"
        assert report.summary_value("expected_reviewers") == "6"
        assert "E-REVIEWER_ROSTER_DRIFT" in exception_codes(report)

    def test_clean_reviewer_roster_raises_no_drift_exception(self, tiny_dataset: Path,
                                                             tmp_path: Path):
        clean = fcv.RunParams(period=PERIOD, as_of=AS_OF, input_dir=tiny_dataset,
                              output_dir=tmp_path / "clean", expected_reviewer_count=4)
        assert "E-REVIEWER_ROSTER_DRIFT" not in exception_codes(fcv.run(clean))


# ======================================================================================
# Fraud loss baseline
# ======================================================================================


class TestFraudLoss:
    def test_one_row_per_loaded_cohort_oldest_first(self, report: fcv.Report):
        assert [row.cohort_period for row in report.loss_rows] == ["2026-05", "2026-08"]
        assert [row.is_reporting_period for row in report.loss_rows] == ["N", "Y"]

    def test_matured_cohort_figures(self, report: fcv.Report):
        may = report.loss_rows[0]
        assert may.loans_disbursed == 2
        assert may.principal_disbursed_usd == "10000.00"
        assert may.matured_loans == 2
        assert may.cohort_maturity_pct == "100.00"
        assert may.confirmed_fraud_loans == 1
        assert may.fraud_incidence_pct == "50.000"
        assert (may.gross_exposure_usd, may.recoveries_usd) == ("500.00", "100.00")
        assert may.net_loss_usd == "400.00"
        assert may.net_loss_rate_bps == "400.0"
        assert may.label_status == fcv.LABEL_FINAL

    def test_reporting_cohort_is_provisional_and_understated(self, report: fcv.Report):
        august = report.loss_rows[1]
        assert august.matured_loans == 0
        assert august.cohort_maturity_pct == "0.00"
        assert august.net_loss_usd == "250.00"        # L1 only; L4's label is not yet visible
        assert august.net_loss_rate_bps == "250.0"
        assert august.label_status == fcv.LABEL_PROVISIONAL

    def test_baseline_is_the_most_recent_fully_matured_cohort(self, report: fcv.Report):
        assert report.summary_value("fraud_loss_baseline_period") == "2026-05"
        assert report.summary_value("fraud_loss_baseline_rate_bps") == "400.0"
        assert report.summary_value("fraud_loss_baseline_net_loss_usd") == "400.00"
        assert report.summary("fraud_loss_baseline_rate_bps").label_status == fcv.LABEL_FINAL

    def test_baseline_is_blank_when_no_cohort_has_matured(self, tiny_dataset: Path,
                                                          tmp_path: Path):
        """Never fall back to an immature cohort - report no baseline instead."""
        early = fcv.RunParams(period=PERIOD, as_of=AS_OF, input_dir=tiny_dataset,
                              output_dir=tmp_path / "early", label_maturity_days=400)
        result = fcv.run(early)
        assert result.summary_value("fraud_loss_baseline_period") == ""
        assert result.summary_value("fraud_loss_baseline_rate_bps") == ""
        assert result.summary("fraud_loss_baseline_period").label_status == \
            fcv.LABEL_NOT_APPLICABLE

    def test_loss_is_observed_never_projected(self, report: fcv.Report):
        """Sum of the emitted cohort losses must equal the labels visible at as-of."""
        total = sum(float(row.net_loss_usd) for row in report.loss_rows)
        assert total == 650.00  # L1 (250) + L5 (400); L4 and L99 excluded


# ======================================================================================
# Output files and the HTML pack
# ======================================================================================


class TestOutputs:
    def test_all_deliverables_are_written(self, report: fcv.Report):
        expected = {filename for filename, _ in fcv.CSV_OUTPUTS} | {
            "evidence_pack.html", "manifest.json"}
        assert {path.name for path in report.params.run_dir().iterdir()} == expected

    def test_outputs_are_partitioned_by_period(self, report: fcv.Report):
        assert report.params.run_dir().name == PERIOD

    def test_csv_headers_match_the_dataclass_schemas(self, report: fcv.Report):
        for filename, attribute in fcv.CSV_OUTPUTS:
            with (report.params.run_dir() / filename).open("r", encoding="utf-8") as handle:
                header = next(csv.reader(handle))
            assert header == fcv.columns_for(fcv.ROW_TYPES[attribute])

    def test_csvs_use_unix_line_endings_for_cross_platform_determinism(self,
                                                                       report: fcv.Report):
        raw = (report.params.run_dir() / "summary.csv").read_bytes()
        assert b"\r\n" not in raw

    def test_html_pack_reports_the_headline_figures(self, report: fcv.Report):
        pack = (report.params.run_dir() / "evidence_pack.html").read_text("utf-8")
        assert "Audit-Readiness Fraud Control view" in pack
        assert "50.00%" in pack                      # controls evidenced
        assert "no credit decisions" in pack
        assert "400.0 bps" in pack                   # fraud loss baseline
        assert "R999" in pack                        # the critical finding is visible
        assert "Scope &amp; limitations" in pack

    def test_html_header_shows_datasets_loaded_count_matching_manifest(self,
                                                                       report: fcv.Report):
        manifest = json.loads((report.params.run_dir() / "manifest.json").read_text("utf-8"))
        loaded_count = sum(1 for entry in manifest["inputs"] if entry["status"] == "LOADED")
        assert loaded_count == 9  # 3 static + 4 period extracts + labels + 1 baseline cohort
        pack = (report.params.run_dir() / "evidence_pack.html").read_text("utf-8")
        assert f"{loaded_count} datasets loaded" in pack

    def test_datasets_loaded_count_is_derived_not_hardcoded(self, params: fcv.RunParams):
        """Adding an extra loadable cohort must change the rendered count, proving it is
        computed from the manifest rather than a fixed literal."""
        baseline = fcv.run(params)
        baseline_pack = (baseline.params.run_dir() / "evidence_pack.html").read_text("utf-8")
        assert "9 datasets loaded" in baseline_pack

        # 2026-06 is an optional baseline cohort that the tiny_dataset fixture never
        # writes (see test_manifest_records_missing_optional_cohorts); adding it flips
        # that entry from MISSING to LOADED.
        _write(
            params.input_dir / "loans_2026-06.csv",
            ["loan_id", "application_id", "customer_id", "disbursal_date",
             "principal_amount", "product_code", "origination_channel", "loan_status"],
            [["L7", "APP7", "C7", "2026-06-02", "1000.00", "PL_SMALL_12M", "MOBILE_APP",
              "DISBURSED"]],
        )
        richer = fcv.run(fcv.RunParams(
            period=params.period, as_of=params.as_of, input_dir=params.input_dir,
            output_dir=params.output_dir.parent / "richer", audit_date=params.audit_date,
        ))
        richer_pack = (richer.params.run_dir() / "evidence_pack.html").read_text("utf-8")
        assert "10 datasets loaded" in richer_pack
        assert "9 datasets loaded" not in richer_pack

    def test_html_escapes_source_text(self, params: fcv.RunParams):
        registry = params.input_dir / "rule_registry.csv"
        registry.write_text(
            registry.read_text("utf-8").replace("Velocity burst",
                                                "Velocity <script>alert(1)</script>"),
            encoding="utf-8")
        pack = (fcv.run(params).params.run_dir() / "evidence_pack.html").read_text("utf-8")
        assert "<script>alert(1)</script>" not in pack
        assert "&lt;script&gt;" in pack

    def test_html_has_no_double_escaped_entities(self, report: fcv.Report):
        """A pre-encoded entity passed through the escaper renders as literal '&#183;'."""
        pack = (report.params.run_dir() / "evidence_pack.html").read_text("utf-8")
        assert "&amp;#" not in pack

    def test_html_chart_bars_stay_inside_the_plot(self, report: fcv.Report):
        """The longest bar must not overflow the SVG viewBox."""
        pack = (report.params.run_dir() / "evidence_pack.html").read_text("utf-8")
        assert 'viewBox="0 0 790' in pack  # 250 label + 470 bar area + 70 for the end label

    def test_cli_runs_end_to_end(self, tiny_dataset: Path, tmp_path: Path, capsys):
        exit_code = fcv.main([
            "--period", PERIOD, "--as-of", AS_OF.isoformat(),
            "--input-dir", str(tiny_dataset), "--output-dir", str(tmp_path / "cli"),
            "--audit-date", "2026-10-28",
        ])
        assert exit_code == 0
        output = capsys.readouterr().out
        assert "controls evidenced      : 3/6 (50.00%)" in output
        assert "400.0 bps FINAL" in output

    def test_cli_fail_on_critical_exits_two(self, tiny_dataset: Path, tmp_path: Path):
        exit_code = fcv.main([
            "--period", PERIOD, "--as-of", AS_OF.isoformat(),
            "--input-dir", str(tiny_dataset), "--output-dir", str(tmp_path / "cli2"),
            "--fail-on-critical",
        ])
        assert exit_code == 2  # R999 is firing without being registered

    def test_cli_reports_a_missing_source_without_a_traceback(self, tiny_dataset: Path,
                                                              tmp_path: Path, capsys):
        (tiny_dataset / "loans_2026-08.csv").unlink()
        exit_code = fcv.main([
            "--period", PERIOD, "--as-of", AS_OF.isoformat(),
            "--input-dir", str(tiny_dataset), "--output-dir", str(tmp_path / "cli3"),
        ])
        assert exit_code == 1
        assert "ERROR:" in capsys.readouterr().err

    def test_days_to_audit_is_derived_from_the_as_of_date(self, report: fcv.Report):
        assert report.summary_value("days_to_audit") == "43"  # 2026-09-15 -> 2026-10-28


# ======================================================================================
# Feature: control coverage trend (--compare-period)
# ======================================================================================


def _write_compare_period_may(data_dir: Path) -> None:
    """Write full 2026-05 extracts so its coverage differs from 2026-08.

    2026-05 fires R002 (AC-02) and R003 (AC-03); 2026-08 fires R001 (AC-01),
    R002 (AC-02) and R006 (AC-05). So AC-01/AC-05 are NEWLY_EVIDENCED, AC-03 REGRESSED,
    AC-02 STILL_EVIDENCED, AC-04/AC-06 STILL_UNEVIDENCED.
    """
    _write(
        data_dir / "rule_fires_2026-05.csv",
        ["fire_id", "application_id", "rule_id", "fired_at"],
        [
            ["F-05-1", "APP-05-1", "R002", "2026-05-10T09:00:00"],
            ["F-05-2", "APP-05-2", "R003", "2026-05-11T09:00:00"],
        ],
    )
    _write(
        data_dir / "alerts_2026-05.csv",
        ["alert_id", "application_id", "rule_id", "created_at", "assigned_reviewer_id"],
        [],
    )
    _write(
        data_dir / "review_outcomes_2026-05.csv",
        ["review_id", "alert_id", "reviewer_id", "decided_at", "outcome"],
        [],
    )


class TestControlCoverageTrend:
    def test_no_compare_period_writes_header_only_and_notes_it(self, params: fcv.RunParams):
        report = fcv.run(params)
        assert report.compare_usable is False
        assert report.trend_rows == []
        assert read_csv_rows(report.params.run_dir() / "control_coverage_trend.csv") == []
        pack = (report.params.run_dir() / "evidence_pack.html").read_text("utf-8")
        assert "Control coverage trend" in pack
        assert "--compare-period" in pack

    def test_unusable_compare_period_degrades_without_failing(self, params: fcv.RunParams):
        # 2026-01 has no extracts at all, so the comparison cannot be computed.
        bad = fcv.RunParams(period=params.period, as_of=params.as_of,
                            input_dir=params.input_dir, output_dir=params.output_dir,
                            audit_date=params.audit_date, compare_period="2026-01")
        report = fcv.run(bad)
        assert report.compare_usable is False
        assert report.compare_note
        assert read_csv_rows(report.params.run_dir() / "control_coverage_trend.csv") == []

    def test_self_comparison_is_zero_delta_with_stable_classes(self, params: fcv.RunParams):
        same = fcv.RunParams(period=params.period, as_of=params.as_of,
                             input_dir=params.input_dir, output_dir=params.output_dir,
                             audit_date=params.audit_date, compare_period=params.period)
        report = fcv.run(same)
        assert report.compare_usable is True
        assert report.summary_value("controls_evidenced_pct_prior") == "50.00"
        assert report.summary_value("controls_evidenced_pct_delta") == "0.00"
        assert len(report.trend_rows) == 6
        assert {row.change_class for row in report.trend_rows} <= {
            fcv.TREND_STILL_EVIDENCED, fcv.TREND_STILL_UNEVIDENCED}

    def test_flip_yields_newly_evidenced_and_regressed(self, params: fcv.RunParams):
        _write_compare_period_may(params.input_dir)
        compared = fcv.RunParams(period=params.period, as_of=params.as_of,
                                 input_dir=params.input_dir, output_dir=params.output_dir,
                                 audit_date=params.audit_date, compare_period="2026-05")
        report = fcv.run(compared)
        assert report.compare_usable is True
        by_id = {row.control_id: row.change_class for row in report.trend_rows}
        assert by_id["AC-01"] == fcv.TREND_NEWLY_EVIDENCED
        assert by_id["AC-03"] == fcv.TREND_REGRESSED
        assert by_id["AC-02"] == fcv.TREND_STILL_EVIDENCED
        assert by_id["AC-04"] == fcv.TREND_STILL_UNEVIDENCED
        # The signed delta reconciles: 50.00 current - 33.33 prior.
        assert report.summary_value("controls_evidenced_pct_prior") == "33.33"
        assert report.summary_value("controls_evidenced_pct_delta") == "16.67"
        # Current-evidenced count reconciles with the existing coverage metric.
        evidenced_now = sum(1 for row in report.trend_rows
                            if row.current_status == "EVIDENCED")
        assert evidenced_now == int(report.summary_value("controls_evidenced"))

    def test_trend_csv_is_deterministic_across_runs(self, params: fcv.RunParams):
        _write_compare_period_may(params.input_dir)
        common = dict(period=params.period, as_of=params.as_of,
                      input_dir=params.input_dir, compare_period="2026-05")
        first = fcv.run(fcv.RunParams(output_dir=params.output_dir.parent / "t1", **common))
        second = fcv.run(fcv.RunParams(output_dir=params.output_dir.parent / "t2", **common))
        name = "control_coverage_trend.csv"
        assert ((first.params.run_dir() / name).read_bytes()
                == (second.params.run_dir() / name).read_bytes())

    def test_compare_inputs_are_pinned_in_the_manifest(self, params: fcv.RunParams):
        _write_compare_period_may(params.input_dir)
        compared = fcv.RunParams(period=params.period, as_of=params.as_of,
                                 input_dir=params.input_dir, output_dir=params.output_dir,
                                 audit_date=params.audit_date, compare_period="2026-05")
        report = fcv.run(compared)
        manifest = json.loads(
            (report.params.run_dir() / "manifest.json").read_text("utf-8"))
        assert "compare_inputs" in manifest
        logical = {entry["logical_name"] for entry in manifest["compare_inputs"]}
        assert {"rule_fires", "alerts", "review_outcomes", "loans"} <= logical


# ======================================================================================
# Full-scale smoke test (opt-in: requires the generated 30k-loan month)
# ======================================================================================


@pytest.mark.skipif(not (FULL_DATA_DIR / "loans_2026-08.csv").exists(),
                    reason="run `python generate_sample_data.py --out-dir data` first")
class TestFullScaleSmoke:
    def test_thirty_thousand_loan_month_reconciles(self, tmp_path: Path):
        result = fcv.run(fcv.RunParams(
            period="2026-08", as_of=date(2026, 9, 15), input_dir=FULL_DATA_DIR,
            output_dir=tmp_path / "full", audit_date=date(2026, 10, 28)))

        assert result.summary_value("rules_in_registry_total") == "340"
        assert result.summary_value("active_rules_registered") == "40"
        assert 29_000 <= int(result.summary_value("loans_disbursed")) <= 31_000

        # Portfolio rollups must reconcile with the per-rule rows they come from.
        assert int(result.summary_value("alerts_total")) == sum(
            row.alert_count for row in result.rule_rows)
        assert int(result.summary_value("rule_fires_total")) == sum(
            row.fire_count for row in result.rule_rows)
        assert (int(result.summary_value("alerts_dispositioned"))
                + int(result.summary_value("alerts_pending"))
                == int(result.summary_value("alerts_total")))

        # The reporting month is far too young to be labelled; the baseline is not.
        assert result.summary("reporting_period_net_loss_rate_bps").label_status == \
            fcv.LABEL_PROVISIONAL
        assert result.summary("fraud_loss_baseline_rate_bps").label_status == fcv.LABEL_FINAL
        assert float(result.summary_value("fraud_loss_baseline_rate_bps")) > 0
