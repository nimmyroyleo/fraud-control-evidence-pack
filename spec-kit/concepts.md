# Spec-Kit Concepts

## Purpose

This document captures key Spec-Kit concepts and evaluates how they could be applied to the Fraud Control Evidence Pack project.

Current State:

- The repository currently implements a custom AIDLC workflow.
- The repository does not currently use the Spec-Kit CLI.
- No `.specify` directory exists in the repository.
- The concepts below are being evaluated for future integration into the workflow.

---

## 1. Templates

**Definition** (fact): Templates are the document scaffolds and command prompts Spec Kit
ships with — e.g. `spec-template.md`, `plan-template.md`, `tasks-template.md`,
`checklist-template.md`, `constitution-template.md` — plus the AI-agent command prompts
(`speckit.specify`, `speckit.plan`, etc.). They are resolved at runtime through a priority
stack: project-local overrides → installed presets → extension-provided templates → core
templates shipped with Spec Kit.

**Purpose** (fact): Give every feature's spec/plan/tasks artifacts a consistent, predictable
structure so commands like `/speckit.specify` and `/speckit.tasks` always produce
documents with the same required sections (e.g. Functional Requirements, Success Criteria,
User Stories).

**Example** (fact): The core `spec-template.md` mandates a `## Requirements` section with
`### Functional Requirements` and a `## Success Criteria` section with measurable outcomes
(`SC-001`, `SC-002`, ...) — every generated spec inherits that shape unless a preset or
project override replaces it.

**How it could apply to Fraud Control Evidence Pack (Recommendation)**: Your existing
[specs/datasets-loaded-indicator.md](specs/datasets-loaded-indicator.md) and
[specs/report-generation-summary.md](specs/report-generation-summary.md) already follow a
consistent hand-written shape (`Feature Summary`, `Functional Requirements` FR-##,
`Acceptance Criteria` AC-##, `Dependencies`, `Constraints`). A Spec Kit `spec-template.md`
override could codify that exact shape — including sections asserting the four project
constraints (read-only, traceable, idempotent, provisional) — so every future feature spec
is generated in that format automatically instead of by convention.

---

## 2. Presets

**Definition** (fact): Presets override templates and commands supplied by the core **and
by installed extensions**, without adding new tooling. Multiple presets can be stacked with
priority ordering, and composition uses one of four strategies: `replace` (default),
`prepend`, `append`, or `wrap` (wraps around `{CORE_TEMPLATE}`).

**Purpose** (fact): Change *how a process works* — enforce organizational or regulatory
standards in existing templates, adapt the methodology (Agile, Kanban, Waterfall, DDD),
require test-first task ordering, add security review gates, or localize terminology —
without writing new commands.

**Example** (fact): Spec Kit's own bundled `lean` preset overrides the five core workflow
commands (`speckit.specify`, `speckit.plan`, `speckit.tasks`, `speckit.implement`,
`speckit.constitution`) to skip template boilerplate and produce a single focused Markdown
file per command. The `constitution-sync` preset instead uses the `wrap` strategy to layer
extra propagation logic around the core `/constitution` command.

**How it could apply to Fraud Control Evidence Pack (Recommendation)**: A
`fraud-control-governance` preset could override `spec-template.md` and `plan-template.md`
to *require* the sections this repo already treats as non-negotiable — a `source_files`/
lineage declaration, a read-only assertion, and a determinism statement — so no spec can be
approved without addressing the four constraints documented in [README.md](README.md).

---

## 3. Extensions

**Definition** (fact): Extensions add new capabilities to Spec Kit — domain-specific
commands, external tool integrations, or quality gates. An extension manifest
(`extension.yml`) declares a free-form `category` (common values: `docs`, `code`, `process`,
`integration`, `visibility`) and an `effect` (`read-only` or `read-write`).

**Purpose** (fact): Introduce genuinely new behavior/tooling, as opposed to presets, which
only reshape existing templates and commands.

**Example** (fact): The `agent-context` extension (used in Spec Kit's own
`product-manager` bundle example) keeps an agent context file in sync as part of the
workflow — new tooling behavior, not just a template change.

**How it could apply to Fraud Control Evidence Pack (Recommendation)**: A
`lineage-check` extension with `effect: read-only` could add a new command/quality gate that
validates a drafted spec against `metric_lineage.csv`-style traceability rules — e.g.
rejecting a spec that references an output column with no declared source-file mapping —
before `/speckit.plan` is allowed to run.

---

## 4. Bundles

**Definition** (fact): Bundles compose existing extensions, presets, workflows, and
workflow steps into a single, versioned, installable unit, described by a `bundle.yml`
manifest. They add no new runtime behavior of their own — they are a distribution/
composition layer that installs each component through that component's own existing
machinery (pinned versions, conflict checks, provenance tracking for clean removal/update).

**Purpose** (fact): Provision a complete, role-based or team-based setup in one operation
(`specify bundle install <bundle-id>`) instead of installing several extensions and presets
by hand.

**Example** (fact): Spec Kit's example `product-manager` bundle installs the
`agent-context` extension, the `product-discovery` preset (priority 10, `append`), two
workflow steps (`draft-spec`, `review-spec`), and a `spec-to-roadmap` workflow — all in one
`specify bundle install` call.

**How it could apply to Fraud Control Evidence Pack (Recommendation)**: A
`fraud-control-evidence-pack` bundle could package the `fraud-control-governance` preset,
the `lineage-check` extension, and a `spec → plan → tasks → implement → converge` workflow
with a mandatory human review gate — so a new contributor onboarding to this repo gets the
whole compliance-aware spec-driven setup with a single install command, rather than
configuring each piece manually.