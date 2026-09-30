# Audit-Readiness Fraud Control View Constitution

## Core Principles

### I. Read-Only Evidence Integrity (NON-NEGOTIABLE)
The tool MUST treat every source extract as read-only. Input files are opened for
reading only; their SHA-256 digests are captured on load and re-verified after the run,
and any mismatch MUST fail the run rather than emit a pack. The tool MUST NOT write,
move, or mutate any input. Rationale: the output is regulatory evidence handed to an
examiner; the inputs must remain provably untouched.

### II. Full Lineage & Traceability (NON-NEGOTIABLE)
Every figure in the evidence pack MUST be re-derivable by hand from the pinned source
extracts. Each output column MUST declare its source file, source column, and transform
(`metric_lineage.csv`), and each summary metric MUST carry its own source files and
computation. No output value may exist without a declared origin. Rationale: an examiner
must be able to reconstruct any number independently.

### III. Deterministic & Idempotent Output (NON-NEGOTIABLE)
For a fixed set of input bytes, the tool MUST produce byte-for-byte identical outputs
across reruns. No wall-clock timestamp or other non-deterministic value may enter the
hashed payload. Re-running the same period in place MUST NOT change the determinism
digest. Rationale: reproducibility is the core audit guarantee.

### IV. No Credit Decisioning; Metrics Labeled by Maturity
The tool performs analytical reporting only and MUST NOT make or imply credit decisions
about any borrower. Fraud-outcome and loss figures whose labelling window has not closed
MUST be labelled PROVISIONAL; only fully matured cohorts may be reported as FINAL.
Rationale: the pack evidences controls, it does not adjudicate individuals, and immature
metrics must never be mistaken for settled facts.

### V. Stdlib-Only Runtime, Test-Enforced
The **runtime imports** of the production modules (`fraud_control_view.py`,
`generate_sample_data.py`) MUST be limited to the Python standard library, so an examiner
can run the tool in a locked-down environment with no package installation. This rule
applies only to what the shipped tool imports and executes at run time; development and
process tooling that the shipped tool never imports — the pytest suite, the custom
`.github/agents/` contracts, and the Spec-Kit CLI — is explicitly out of scope and does
NOT count as a runtime dependency. The pytest suite is the enforcement mechanism for
Principles I–IV and MUST pass before any change is accepted; new behavior MUST add or
extend tests. Rationale: the locked-down reproducibility guarantee concerns the tool's
runtime only, and constraints that are merely written down are not guarantees — the tests
make them enforceable.

## Additional Constraints

- Inputs are CSV extracts for a single reporting period; no network access, no database,
  no third-party runtime package.
- Outputs land under `out/<period>/` and include per-grain CSVs, a single-file HTML pack,
  and a manifest recording digests, row counts, run parameters, and the determinism digest.
- The tool is a prototype built against synthetic data; regulatory framework references
  are real, but control text and figures are fabricated and MUST be presented as such.

## Development Workflow & Quality Gates

- Changes follow a spec-driven flow: a feature specification precedes implementation, and
  implementation is bounded to the files that specification declares.
- Every change MUST run the full pytest suite locally and keep it green; each new
  acceptance criterion MUST have corresponding test coverage.
- Unrelated changes MUST NOT be bundled into a feature change (scope discipline).
- A determinism check (run twice, compare non-manifest outputs) MUST pass for any change
  touching output generation.

## Governance

This constitution supersedes ad-hoc practice for this repository. Amendments MUST be
documented in the amending commit, versioned per semantic versioning (MAJOR = principle
removal/redefinition, MINOR = new principle/section, PATCH = clarification), and paired
with any needed migration notes. All reviews MUST verify compliance with Principles I–V;
any deviation MUST be justified in writing and approved before merge.

**Version**: 1.0.0 | **Ratified**: 2026-09-30 | **Last Amended**: 2026-09-30
