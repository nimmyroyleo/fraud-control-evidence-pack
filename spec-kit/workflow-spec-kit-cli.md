# Spec-Kit CLI Workflow (As Executed)

> **This supersedes the earlier recommendation in [workflow.md](workflow.md).**
> That document proposed a *Hybrid* workflow that deliberately avoided the Spec-Kit CLI.
> The team subsequently decided to **actually adopt the Spec-Kit CLI**, so this file
> records the workflow that was really executed — **derived from the implementation**, not
> designed up front. The Hybrid document is kept as a historical before/after for the
> Activity 2 comparison.

## Purpose

Document the end-to-end Spec-Driven Development workflow, using the GitHub **Spec-Kit
CLI**, that was run to build the **Control Coverage Trend** feature
([specs/001-control-coverage-trend/](../specs/001-control-coverage-trend/)). Every step
below was performed and committed; commit SHAs are cited as evidence.

## Tooling adopted (facts)

- **Spec-Kit CLI** `specify` v1.0.13, installed via `uv tool install specify-cli`.
- **uv** installed via `pip` (dev tooling only).
- Initialized in-repo with `specify init --here --force --integration copilot --script py`,
  which created `.specify/` and the `.github/skills/speckit-*` skill files.
- **Reconciliation with the stdlib-only rule**: the `specify` CLI, `uv`, and `pytest` are
  **development/process tooling**, never imported by the shipped tool at runtime. The
  constitution's Principle V (stdlib-only) therefore still holds — see
  [.specify/memory/constitution.md](../.specify/memory/constitution.md).

## Workflow Diagram

```text
┌──────────────────────────────────────────────────────────────────┐
│ specify init  (once per repo)  →  .specify/ + /speckit-* skills   │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ /speckit-constitution  (once)   →  .specify/memory/constitution.md│
└───────────────────────────────┬──────────────────────────────────┘
                                ▼  (per feature)
┌──────────────────────────────────────────────────────────────────┐
│ /speckit-specify   →  specs/NNN-feature/spec.md + checklist       │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ /speckit-clarify   (optional; skipped when spec is unambiguous)   │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ /speckit-plan      →  plan.md, research.md, data-model.md,        │
│                       contracts/, quickstart.md                   │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ /speckit-tasks     →  tasks.md (dependency-ordered, tests-first)  │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ /speckit-analyze   (read-only consistency gate across artifacts)  │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ /speckit-implement →  production code + tests; run pytest green   │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ /speckit-converge  (assess code vs spec/plan/tasks; append gaps)  │
└──────────────────────────────────────────────────────────────────┘
```

## Step-by-step (as run)

| Step | Skill / command | Output | Git checkpoint |
|---|---|---|---|
| 0. Baseline | — | committed prior Activity-2 work | `e6baba1` |
| 1. Init | `specify init --here --force --integration copilot --script py` | `.specify/`, `.github/skills/` | `a61ac90` |
| 2. Constitution | `/speckit-constitution` | `constitution.md` v1.0.0 (5 principles) | `002525f` |
| 3. Specify | `/speckit-specify` | `spec.md` + quality checklist | `ec95b40` |
| 4. Clarify | *(skipped — spec had no open questions)* | — | — |
| 5. Plan | `/speckit-plan` | `plan.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md` | `07911ea` |
| 6. Tasks | `/speckit-tasks` | `tasks.md` (29 tasks, tests-first) | `14801b1` |
| 7. Analyze | `/speckit-analyze` | read-only report (0 CRITICAL/HIGH) | *(no file change)* |
| 8. Implement | `/speckit-implement` | `fraud_control_view.py` + tests; full suite green | `74b969c` |
| 9. Converge | `/speckit-converge` | assessed converged; `tasks.md` unchanged | *(no file change)* |

Each artifact was committed on its own so the history reads as a clean spec → plan → tasks
→ code progression.

## How the three custom agents map onto Spec-Kit

The repo's pre-existing custom agents ([.github/agents/](../.github/agents)) were **not
discarded**; Spec-Kit covers most of their roles, with one genuine gap:

| Custom agent | Spec-Kit equivalent | Notes |
|---|---|---|
| `spec-enrichment` | `/speckit-specify` (+ `/speckit-clarify`) | Direct replacement; Spec-Kit enforces one template. |
| `developer` | `/speckit-implement` (+ `/speckit-analyze`) | Direct replacement; analyze adds a consistency gate. |
| `architecture-discovery` | **no equivalent** | Spec-Kit assumes greenfield authoring; it has no step that reverse-engineers `context/` from a brownfield repo. **Keep this agent** as a periodic pre-step, run on architectural drift. |

## When the Spec-Kit CLI is / isn't worth it (findings from this run)

- **Worth it** when you want enforced artifact structure, a governing constitution checked
  at plan/analyze time, and a repeatable, auditable spec→code trail — exactly what an
  audit-evidence project benefits from.
- **Overhead to accept**: a CLI + `.specify/` directory, and a few environment frictions
  encountered here (see Lessons). For a one-off single-file tweak, the full pipeline is
  heavier than the change.

## CI integration opportunities (recommendation, not yet implemented)

- Run `pytest` on every PR (the suite already enforces the four hard constraints).
- A stdlib-only import scan on `fraud_control_view.py` to protect Principle V.
- A determinism check (run twice, diff non-manifest outputs).
- A spec-shape / lineage lint so new specs and outputs stay consistent.
- A GitHub agent could open the PR and run `/speckit-analyze`/`/speckit-converge`
  non-destructively as a pre-merge report.

## Lessons learned (facts from this run)

- **Corporate TLS interception**: `uv` downloads failed with `UnknownIssuer` until run
  with `--system-certs`.
- **Non-interactive init**: always pass `--script py`; the interactive TUI menu cannot be
  driven reliably and a stray keystroke silently selected the PowerShell default.
- **`setup_tasks.py` vs `setup_plan.py`**: the plan script copies its template to disk; the
  tasks script only returns template content — `tasks.md` is written by the skill.
- **`pytest` is dev-only**: installing it does not affect the shipped tool's stdlib-only
  runtime guarantee.
- **`.specify/feature.json` is git-ignored** by Spec-Kit's own `.gitignore` — it is a local
  pointer, not a tracked artifact.
