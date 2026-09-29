# Audit-Readiness Fraud Control view

A read-only monthly evidence builder for a digital lender's fraud controls.

For one calendar month it joins the rules engine decision log, the fraud alert queue,
manual review dispositions, the loan disbursal ledger, the confirmed-fraud label file
and the regulatory audit checklist, and produces an evidence pack that internal
compliance can hand to an examiner.

For every fraud rule in scope it reports **fire count, alert volume, the manual review
outcome breakdown, and the audit checklist control the rule evidences**. It rolls that
up into the headline **percentage of checklist controls evidenced** and an **observed
fraud-loss baseline**.

> This is a prototype built against synthetic data. The regulatory references in the
> sample checklist are real frameworks a US digital lender would be examined against;
> the control text, the rules and every figure are fabricated.

---

## Quick start

```bash
# 1. Generate the synthetic source extracts (~30,000 loans/month x 4 cohorts, 340 rules)
python generate_sample_data.py --out-dir data

# 2. Build the evidence pack for the reporting month
python fraud_control_view.py --period 2026-08 --as-of 2026-09-15 \
    --input-dir data --output-dir out --audit-date 2026-10-28

# 3. Open the pack
start out/2026-08/evidence_pack.html     # Windows

# 4. Run the tests
python -m pip install pytest
python -m pytest
```

No third-party runtime dependency: the tool is standard library only, so an examiner
can run it in a locked-down environment and reproduce every number. `pytest` is needed
for the test suite only.

---

## What comes out

Everything lands in `out/<period>/`:

| File | Grain | What it answers |
|---|---|---|
| `control_view.csv` | one row per rule | Fire count, alert volume, outcome breakdown, precision, mapped control — the core deliverable |
| `control_coverage.csv` | one row per checklist control | Is this control evidenced this month, and by which rules? |
| `unmapped_rules.csv` | one row per mapping gap | Which rules have no usable control mapping, who owns them, what to do |
| `integrity_exceptions.csv` | one row per finding | Reconciliation, change-control and governance exceptions, ranked by severity |
| `fraud_loss_baseline.csv` | one row per disbursal cohort | Observed fraud loss and how mature each cohort's labels are |
| `summary.csv` | one row per metric | Headline figures, each with its own source files and computation |
| `metric_lineage.csv` | one row per output column | Output column → source file → source column → transform |
| `evidence_pack.html` | — | Single-file human-readable pack containing all of the above |
| `manifest.json` | — | SHA-256, row count and column list for every source extract; run parameters; determinism digest |

---

## The four design constraints, and how each is enforced

### 1. Read-only — it never writes to its sources, and proves it

- Source extracts are opened in text-read mode only.
- Input digests are captured at load and **re-verified after the run**; any change
  raises `ReadOnlyViolationError` and the run fails rather than emitting an
  untrustworthy snapshot.
- Writing anywhere inside `--input-dir` is refused up front.
- `manifest.json` records `mode: READ_ONLY` and the verification result.

It also makes **no credit decisions**: there is no scoring, no applicant thresholding,
no approve/decline/refer logic anywhere in the module. It describes decisions the rules
engine already made. A test asserts the module exposes no decisioning entry point, so
that stays true as the prototype grows.

### 2. Traceable — every figure resolves to a pinned file

Three layers, so a figure can be defended in an audit meeting without opening the code:

1. Every output row carries a `source_files` column naming its logical extracts.
2. `metric_lineage.csv` states, per output column, the source columns and the transform.
3. `manifest.json` resolves each logical name to a path, SHA-256, row count and column
   list.

A test fails if any emitted column loses its lineage row, or if any lineage row
references a column that no longer exists — the documentation cannot silently drift
from the schema.

### 3. Monthly idempotent — a closed month reproduces byte for byte

Output is a pure function of `(period, as-of date, input bytes)`:

- Rows dated outside the period are excluded **and flagged**, never silently dropped.
- Dispositions and fraud labels dated after `--as-of` are excluded, so re-running a
  closed month in six weeks' time gives the same answer it gave today.
- No wall-clock value enters any CSV or the HTML pack. The run timestamp lives only in
  `manifest.json`, outside the hashed payload.
- `manifest.json` carries a `deterministic_payload_sha256` over every other output.

Verified in practice: two consecutive runs of the 30,000-loan month produced identical
SHA-256 digests for all eight non-manifest outputs.

### 4. Provisional metrics — the 60-90 day labelling lag is made explicit

Confirmed fraud is labelled 60-90 days after disbursal, so a recent month's fraud
figures are structurally understated. Rather than report them as if they were final:

- Each cohort carries `cohort_maturity_pct` and a `label_status` of `FINAL` or
  `PROVISIONAL`.
- Every fraud-derived metric carries the same status, and the HTML pack leads with a
  provisional banner.
- The **defensible baseline is taken from the most recent fully matured cohort**, not
  from the reporting month. If no cohort has matured, the baseline is reported blank —
  the tool never falls back to an immature number.

The synthetic data shows exactly why this matters:

| cohort | label maturity | observed net loss rate | status |
|---|---|---|---|
| 2026-05 | 100% | **117.9 bps** | FINAL ← baseline |
| 2026-06 | 56.6% | 61.2 bps | PROVISIONAL |
| 2026-07 | 0% | 9.9 bps | PROVISIONAL |
| 2026-08 | 0% | 0.0 bps | PROVISIONAL |

Reading 9.9 bps off the July cohort would understate fraud loss by more than 90%.

---

## Definitions an examiner will ask about

**"Evidenced"** means a checklist control has at least one mapped `ACTIVE` rule that
fired at least `--min-fires-for-evidence` times in the period. It asserts the control
**operated**, not that it is **effective** — effectiveness testing is a separate
exercise. Controls operated outside the rules engine (policy, training, third-party
attestations) cannot be evidenced by this pack and surface as
`NOT_EVIDENCED_NO_RULE_MAPPED`.

**Alert precision** is `confirmed / (confirmed + cleared)`. Inconclusive and pending
alerts are excluded from the denominator, which makes it an *upper bound* on rule
performance. Stated on every row and in the lineage.

**Loan-linked columns are non-exclusive attribution.** Several rules fire on one
application, so `linked_confirmed_fraud_loans` and
`linked_confirmed_fraud_net_loss_usd` overlap across rules and their sum exceeds
portfolio loss by design. Use `fraud_loss_baseline.csv` for portfolio totals.

**Undefined ratios are blank, not zero.** A rule with no alerts has a blank precision,
not `0.00` — "no denominator" and "zero performance" are different findings.

---

## What the tool flags

Mapping and coverage gaps, in `unmapped_rules.csv`:

| Gap | Risk | Meaning |
|---|---|---|
| `UNREGISTERED_RULE_FIRING` | CRITICAL | A rule is firing in production that does not exist in the registry |
| `UNMAPPED_ACTIVE_RULE` | HIGH | An active rule's output evidences no control |
| `NON_ACTIVE_RULE_FIRING` | HIGH | A retired or shadow rule is still in the decision path and unmapped |

Integrity, change-control and governance exceptions, in `integrity_exceptions.csv` —
27 check classes including duplicate primary keys, alerts that cannot be reconciled to
a fire, dispositions with no alert, labels referencing unknown loans, rules whose
annual review is overdue, alerts pending beyond SLA, reviewer roster drift against the
documented six-person team, and rows falling outside the reporting period.

Record-level defect classes are **aggregated into one bounded row** with a count and up
to five sample identifiers, so a malformed feed produces one legible finding rather
than 40,000 rows.

---

## Files

| File | Role |
|---|---|
| `fraud_control_view.py` | The tool. Stdlib only. |
| `test_fraud_control_view.py` | 93 pytest tests, organised by the four constraints above. |
| `generate_sample_data.py` | Deterministic synthetic extracts. **Not part of the control environment** — never point it at production paths. |
| `pytest.ini` | Test config. Disables the pytest cache, which cannot be written on synced OneDrive paths. |

The test suite runs against a seven-loan, six-rule fixture whose expected figures were
worked out by hand and written into the assertions, plus one opt-in smoke test against
the full 30,000-loan month (skipped automatically if `data/` has not been generated).

---

## Options

```
--period                   reporting month, YYYY-MM                    (required)
--as-of                    snapshot date; nothing later is included    (required)
--input-dir                source extracts, read-only                  (default: data)
--output-dir               evidence pack; must sit outside --input-dir (default: out)
--label-maturity-days      confirmed-fraud labelling window            (default: 90)
--baseline-lookback-months prior cohorts loaded for the baseline       (default: 3)
--expected-reviewer-count  documented manual review team size          (default: 6)
--min-fires-for-evidence   fires a mapped active rule needs            (default: 1)
--stale-review-days        annual rule review expectation              (default: 365)
--alert-ageing-sla-days    pending-alert ageing threshold              (default: 30)
--audit-date               scheduled audit date, for days-to-audit     (optional)
--fail-on-critical         exit 2 when any CRITICAL exception is found (CI gate)
```

---

## Expected source extracts

Static, plus one set per period. Extra columns are ignored; the listed ones are
required.

| File | Required columns |
|---|---|
| `rule_registry.csv` | `rule_id, rule_name, status, risk_typology, owner, last_reviewed_date` |
| `audit_checklist.csv` | `control_id, control_title, control_family, regulatory_reference, evidence_requirement` |
| `rule_control_map.csv` | `rule_id, control_id` |
| `loans_<period>.csv` | `loan_id, application_id, disbursal_date, principal_amount` |
| `rule_fires_<period>.csv` | `fire_id, application_id, rule_id, fired_at` |
| `alerts_<period>.csv` | `alert_id, application_id, rule_id, created_at, assigned_reviewer_id` |
| `review_outcomes_<period>.csv` | `review_id, alert_id, reviewer_id, decided_at, outcome` |
| `fraud_labels.csv` | `loan_id, label_date, net_loss_amount` (+ optional `gross_exposure_amount`, `recovery_amount`) |

Approved `outcome` codes: `CONFIRMED_FRAUD`, `CLEARED_FALSE_POSITIVE`,
`INSUFFICIENT_EVIDENCE`. Anything else is excluded and raises an exception.

---

## Taking this to production

The prototype is deliberately file-based and single-process. Before it feeds a real
audit:

1. **Point it at systems of record**, not hand-cut CSV extracts. Swap `read_table` for
   warehouse reads that return the same row dicts, and keep the digest-and-row-count
   discipline by hashing the extract query results.
2. **Have the control owner sign off `rule_control_map.csv`** and version it. The
   mapping is the weakest link in the chain: the tool can prove a rule fired, but only
   a human can assert which control that evidences.
3. **Run it monthly on a schedule and archive `out/<period>/` immutably.** The
   determinism digest is only worth something if last month's pack still exists to
   compare against.
4. **Add a period-over-period view.** A control that was evidenced in June and is not
   in August is a stronger signal than either month alone, and needs the archive from
   point 3.
5. **Agree the evidence definition with audit before the fieldwork**, not during it —
   particularly the treatment of controls operated outside the rules engine.
