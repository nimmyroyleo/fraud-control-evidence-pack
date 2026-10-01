# Activity 2 Summary: Spec-Kit Evaluation for the Fraud Control Evidence Pack

## 1. Objective

Evaluate GitHub Spec-Kit (concepts, tooling, and workflow) against this repository's
existing custom agent-based AIDLC workflow, and design a final, evidence-based
development workflow for the Fraud Control Evidence Pack prototype.

## 2. Work Completed

| Deliverable | Content |
|---|---|
| [concepts.md](spec-kit/concepts.md) | Definitions, purposes, and examples for the four Spec-Kit building blocks — Templates, Presets, Extensions, Bundles — each mapped to a potential application in this project, with facts and recommendations explicitly separated |
| [implementation-comparison.md](spec-kit/implementation-comparison.md) | Structured comparison of the current agent-based workflow, a full Spec-Kit workflow, and a hybrid, across workflow, benefits, drawbacks, governance, repeatability, and use cases |
| [findings.md](spec-kit/findings.md) | Required / Optional / Not-needed classification of the three custom agents, with reasoning, alternatives, and CI/CD opportunities |
| [workflow.md](spec-kit/workflow.md) | Final end-to-end workflow: diagram, ten steps, four git checkpoints, two validation gates, and five new CI checks |

## 3. Key Findings

All findings were verified against repository evidence:

- **No Spec-Kit footprint exists** — no `.specify/` directory, no `specify` CLI; the
  current workflow is three hand-authored agent contracts under
  [.github/agents/](.github/agents).
- **Spec shape drift is the observed problem**: the two shipped specs use materially
  different structures — [datasets-loaded-indicator.md](specs/datasets-loaded-indicator.md)
  has numbered FR-##/AC-##s and line-cited dependencies;
  [report-generation-summary.md](specs/report-generation-summary.md) is a short,
  unnumbered outline. Nothing enforces consistency.
- **The Architecture Discovery Agent has no Spec-Kit equivalent** — Spec-Kit's closest
  command (`/speckit.converge`) assesses code against existing spec/plan/tasks; it cannot
  reverse-engineer a brownfield repo into `context/` files. Classification: **Required**
  (periodic, on drift).
- **The Developer Agent is Required** — its rules are the only repo-local statement of
  the four hard constraints (read-only, traceable, idempotent, provisional metrics), but
  enforcement today rests entirely on the pytest suite run manually.
- **The Spec Enrichment Agent is Optional** — well-defined but not reliably followed, as
  the spec drift demonstrates; a mandatory template plus mechanical lint closes the gap
  more dependably than the agent contract alone.
- **No CI/CD exists** — `.github/workflows/` is absent; even the 93-test suite runs only
  when a human runs it locally.

## 4. Comparison Outcome

- **Agent-only (status quo)**: minimal and dependency-free, fits the brownfield repo,
  but has no enforcement — governance is prose, repeatability is convention.
- **Spec-Kit-only**: strong structural governance (constitution, versioned templates,
  analyze/converge gates) but adds a CLI + `.specify/` dependency at odds with the
  repo's advertised stdlib-only, examiner-reproducible posture, cannot replace
  Architecture Discovery, and is disproportionate to a one-module, two-feature
  prototype.
- **Hybrid**: keep all three agents; borrow Spec-Kit *concepts* as project-local files
  and checks — a `specs/_template.md`, an analyze-style spec gate, a converge-style
  merge gate, and stdlib-only CI.

## 5. Final Recommendation

> **SUPERSEDED.** This Hybrid recommendation was overtaken by the team's decision to adopt
> the Spec-Kit CLI directly. The workflow actually executed is recorded in
> [workflow-spec-kit-cli.md](workflow-spec-kit-cli.md); see Section 7 for the outcome.

**Adopt the Hybrid workflow** defined in [workflow.md](spec-kit/workflow.md):

1. Architecture Discovery runs on drift only (Gate 0), not per feature.
2. Spec Enrichment must fill a new `specs/_template.md` (first adoption task).
3. Two validation gates: spec analysis before implementation, convergence before merge.
4. Four git checkpoints separating context refresh, approved spec, implementation, and
   merge.
5. Five new CI checks, all stdlib + pytest: test suite on every PR, spec-shape lint,
   files-touched scope check, stdlib-only import scan, and a two-run determinism check.

The workflow deliberately mirrors `/speckit.specify`, `/speckit.analyze`, and
`/speckit.converge` in Steps 3, 4, and 9, preserving a clean migration path to the full
Spec-Kit CLI if the project later spans multiple repos or teams.

## 6. Learning Outcomes

- **Spec-Kit's four building blocks serve distinct roles**: templates shape artifacts,
  presets reshape process, extensions add capability, bundles distribute the whole stack
  — and the concepts are adoptable independently of the CLI.
- **Tool choice must follow the observed problem**: this repo's pain point was spec
  drift, not missing process — so the fix is one template plus mechanical checks, not a
  full toolchain.
- **Brownfield vs. greenfield matters**: spec-first tooling assumes specs precede code;
  a repo whose architecture must be discovered from evidence needs a discovery step no
  off-the-shelf SDD tool currently provides.
- **Governance-by-prose does not survive contact with practice**: the Developer Agent's
  written rules were sound, yet only the test suite actually enforced them —
  machine-checkable gates (CI) are what convert instructions into guarantees.
- **Fact/recommendation separation** — the same discipline the evidence pack applies to
  its metrics (verified vs. provisional) proved equally valuable when evaluating
  process tooling.

## 7. Actual Outcome: Spec-Kit CLI Adoption

The Hybrid recommendation (Sections 4-5) was **not** the path taken. The team decided to
**adopt the Spec-Kit CLI** and build a new, more complex feature through it, so the
comparison is now backed by a real implementation rather than analysis alone.

**What was done (facts, committed to Git):**

- Installed the Spec-Kit CLI (`specify` v1.0.13) and initialized it in-repo
  (`specify init --here --force --integration copilot --script py`).
- Ran the full SDD pipeline for the **Control Coverage Trend** feature
  ([../specs/001-control-coverage-trend/](../specs/001-control-coverage-trend/)):
  constitution -> specify -> plan -> tasks -> analyze -> implement -> converge.
- Shipped the feature in [../fraud_control_view.py](../fraud_control_view.py) with new
  tests in [../test_fraud_control_view.py](../test_fraud_control_view.py); the full pytest
  suite passes.
- Recorded the executed workflow in [workflow-spec-kit-cli.md](workflow-spec-kit-cli.md).

**What this validated (works / does not):**

- **Works**: the enforced template structure, the constitution as a plan/analyze-time
  gate, and the clean spec->code commit trail fit an audit-evidence project well; the
  `analyze` and `converge` gates added real cross-artifact checking.
- **Friction**: environment setup needed `--system-certs` for corporate TLS and
  `--script py` to avoid an unreliable interactive menu; the full pipeline is heavier than
  a one-line change would warrant.
- **Confirmed gap**: Spec-Kit has **no equivalent** to the custom Architecture Discovery
  agent (brownfield context generation), which is therefore kept as a periodic pre-step.
- **Constraint preserved**: the CLI, `uv`, and `pytest` are dev/process tooling only, so
  the shipped tool's stdlib-only runtime guarantee (Constitution Principle V) still holds.