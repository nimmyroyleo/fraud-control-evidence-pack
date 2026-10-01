# Implementation Comparison: Agent-Based vs. Spec-Kit vs. Hybrid

> **UPDATE (supersedes the recommendation at the end).** Since this comparison was written,
> the team **adopted the Spec-Kit CLI** and built the Control Coverage Trend feature through
> it, so a `.specify/` directory now exists. The analysis below is retained, but the final
> decision is **full Spec-Kit CLI adoption**, not the Hybrid recommended at the bottom — see
> [workflow-spec-kit-cli.md](workflow-spec-kit-cli.md) and Section 7 of
> [activity2-summary.md](activity2-summary.md).

Scope: this repository only. No `.specify/` directory exists and the Spec Kit CLI
(`specify`) is not installed here (Fact). Everything under "Spec-Kit-Based Workflow"
below describes what adopting it *would* look like, based on public Spec Kit
documentation — it is not a description of anything currently running in this repo.

---

## 1. Current Agent-Based Workflow

**What it is (Fact)**: Three custom agent-role files under
[.github/agents/](.github/agents), each a short markdown contract, run in sequence by a
human or coding agent:

1. [architecture-discovery.agent.md](.github/agents/architecture-discovery.agent.md) — reads
   repository evidence only, writes [architecture-overview.md](context/architecture-overview.md),
   [workflow.md](context/workflow.md), [constraints.md](context/constraints.md),
   [testing-strategy.md](context/testing-strategy.md).
2. [spec-enrichment.agent.md](.github/agents/spec-enrichment.agent.md) — turns a
   requirement into an implementation-ready spec under [specs/](specs), using the
   architecture context as input.
3. [developer.agent.md](.github/agents/developer.agent.md) — implements strictly from the
   approved spec + architecture context, updates tests, avoids unrelated changes.

### Workflow
Human/agent runs discovery once (or on drift) → context/ is produced → each new
requirement goes through spec-enrichment → a spec lands in specs/ → developer agent
implements against that spec + context.

### Benefits
- Minimal — three markdown files, no dependency, no CLI, no new directory convention
  beyond `context/` and `specs/`. Consistent with this repo's stated stdlib-only,
  examiner-reproducible posture (see [README.md](README.md)).
- Architecture Discovery step has no direct Spec-Kit equivalent (see §2) — it's a
  genuine gap-filler for a *brownfield* repo where architecture must first be
  reverse-engineered from evidence, not authored from scratch.
- Full control over spec shape — can be as light ([report-generation-summary.md](specs/report-generation-summary.md))
  or as detailed ([datasets-loaded-indicator.md](specs/datasets-loaded-indicator.md)) as
  the author chooses.

### Drawbacks
- No enforcement: the two existing specs use **different shapes** (one has FR-##/AC-##
  numbering, dependencies, and constraints citing exact source lines; the other has a
  much shorter, unnumbered structure). Nothing in the agent files requires consistency.
- No versioning, no priority/override stack, no packaging — every project that wants
  this workflow copies the three `.agent.md` files by hand.
- No built-in cross-artifact consistency check (Spec Kit's `/speckit.analyze`
  equivalent), no gap-closing pass (`/speckit.converge` equivalent) — verifying that an
  implementation actually satisfies its spec is manual.
- No constitution-equivalent: the four hard constraints (read-only, traceable,
  idempotent, provisional metrics) live in prose in [context/constraints.md](context/constraints.md)
  and the README, not in a governing document every agent is required to re-check
  against.

### Governance
Governance is implicit and prose-based: the Developer Agent's rules ("Follow
architecture", "Respect constraints", "Avoid unrelated changes") are instructions to an
AI agent, not machine-checked gates. Enforcement in practice comes from the pytest
suite ([test_fraud_control_view.py](test_fraud_control_view.py)), not from the agent
workflow itself.

### Repeatability
Repeatable within *this* repo by convention (an agent or contributor reads the three
`.agent.md` files and follows them), but not portable — nothing installs, versions, or
updates this workflow in another repository.

### Best use cases
Small, single-team, single-repo prototypes where the newest requirement is "extend an
existing brownfield codebase" and where discovering architecture from evidence is the
first real problem to solve — exactly this repo's shape today.

---

## 2. Spec-Kit-Based Workflow

**What it is (Fact, per github/spec-kit docs)**: The `specify` CLI scaffolds a
`.specify/` directory with a constitution, core templates, and slash commands
(`/speckit.constitution`, `/speckit.specify`, `/speckit.plan`, `/speckit.tasks`,
`/speckit.implement`, `/speckit.converge`, plus optional `/speckit.clarify`,
`/speckit.checklist`, `/speckit.analyze`). Templates/presets/extensions/bundles (see
[concepts.md](spec-kit/concepts.md)) let teams override structure, add capabilities, and
package role-based setups, all resolved through a documented priority stack.

### Workflow
`/speckit.constitution` (once) → `/speckit.specify` → `/speckit.plan` →
`/speckit.tasks` → `/speckit.implement` → `/speckit.converge` (repeat
implement→converge until "Converged"). Optional `/speckit.analyze` runs a non-destructive
cross-artifact consistency check across spec.md/plan.md/tasks.md before implementation.

### Benefits
- Enforced, versioned template structure (spec/plan/tasks all machine-resolved, not
  copy-pasted) — would eliminate the shape drift seen between this repo's two specs.
- A real governance document (`constitution.md`) that plan-time gates check against,
  instead of constraints living only in prose files.
- Built-in consistency (`/speckit.analyze`) and gap-closing (`/speckit.converge`) passes
  with no equivalent in the current agent files.
- Presets/extensions/bundles (see [concepts.md](spec-kit/concepts.md)) give a path to
  share this setup across other regulated-data projects instead of re-authoring agent
  files each time.

### Drawbacks
- Requires installing and maintaining the `specify` CLI and a `.specify/` directory —
  a new dependency and process surface this repo does not currently have, and its
  README explicitly advertises **no third-party runtime dependency** as a feature for
  examiner reproducibility. Adding CLI tooling to the *workflow* doesn't affect the
  *shipped tool*, but it does add contributor-facing process weight.
- No first-class "discover an existing repo's architecture from evidence" step — Spec
  Kit's flow assumes a spec is being authored (greenfield-oriented), and its closest
  analog, `/speckit.converge`, assesses code against spec/plan/tasks **that already
  exist** — it does not generate `architecture-overview.md`/`workflow.md`/
  `constraints.md`/`testing-strategy.md` from repository evidence the way this repo's
  Architecture Discovery Agent does.
- Heavier for a project of this size: one production module, one test file, one
  contributor persona, two features shipped so far.

### Governance
Strong and structural: constitution + templates + optional `/speckit.analyze` gate
provide machine-checkable consistency, not just written instructions.

### Repeatability
High and portable: versioned templates/presets/bundles are designed to be installed
identically across repositories and updated centrally.

### Best use cases
Multi-repo or multi-team settings where spec/plan/tasks consistency must be enforced
uniformly, where the project is greenfield or spec-first, and where the overhead of a
CLI and `.specify/` directory is justified by scale.

---

## 3. Hybrid Workflow

**What it is (Recommendation)**: Keep the three custom agents — none of them are
redundant with core Spec Kit, and Architecture Discovery in particular has no direct
equivalent — but borrow Spec Kit's *template discipline* without adopting its CLI or
directory structure.

### Workflow
Same three-agent sequence as today, plus:
- A single, project-local `specs/_template.md` (borrowing the *concept* of a template,
  not the tool) that both existing specs are reconciled against, so
  [report-generation-summary.md](specs/report-generation-summary.md) and
  [datasets-loaded-indicator.md](specs/datasets-loaded-indicator.md) converge on one
  shape (FR-##/AC-##/Dependencies/Constraints).
- A lightweight "analyze" step folded into the Developer Agent or run manually before
  implementation: check the spec against [context/constraints.md](context/constraints.md)
  (read-only, deterministic, no credit decisions, reproducible) the same way
  `/speckit.analyze` checks spec/plan/tasks consistency.

### Benefits
- Fixes the concrete, observed problem (inconsistent spec shape) without adding a CLI
  dependency, preserving the "stdlib only, runs in a locked-down environment" property
  the README calls out as a feature.
- Keeps the Architecture Discovery Agent, which nothing in core Spec Kit replaces.
- Cheap to adopt incrementally — a template file and a checklist, not new tooling.

### Drawbacks
- Still no CLI-level versioning/packaging (presets/extensions/bundles) — sharing this
  setup with another repo still means copying files by hand.
- Governance is still convention-based, not machine-enforced the way a constitution +
  `/speckit.analyze` gate would be — a template file can still be skipped.

### Governance
Medium: consistent structure is encouraged by having one template to point at, but
nothing blocks a spec that ignores it (same limitation as today, reduced in likelihood
rather than eliminated).

### Repeatability
Medium-high within this repo (one template to follow); still low across repos (no
package manager for the agent files themselves).

### Best use cases
A single, evidence-driven, dependency-averse project — like this one — that wants
consistency between specs without taking on process tooling disproportionate to its
size.

---

## Recommendation

**Hybrid Workflow.**

This repository is a single-module, stdlib-only, audit-reproducibility prototype (see
[README.md](README.md)) whose four hard constraints (read-only, traceable, idempotent,
provisional metrics) are already enforced primarily by its pytest suite, not by process
tooling. Its current pain point, visible directly in [specs/](specs), is **spec shape
drift** — not a lack of process, governance, or discovery capability. Full Spec-Kit CLI
adoption would fix that drift but would also introduce a `.specify/` directory and CLI
dependency this project's own design philosophy argues against, and it has no built-in
replacement for the Architecture Discovery Agent, which is doing real, repo-specific
work here (reverse-engineering an existing brownfield codebase rather than authoring a
greenfield spec).

The Hybrid approach fixes the actual observed problem — add one project-local spec
template and an explicit constraints-check step — while keeping the three agents that
already fit this repo's brownfield, dependency-averse nature. Revisit full Spec-Kit
adoption only if this project grows into multiple repos or teams that need centrally
versioned, machine-enforced templates.