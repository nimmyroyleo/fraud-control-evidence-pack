# Final Workflow: Fraud Control Evidence Pack

## Purpose

This workflow is based on the conclusions documented in:

- [Spec-Kit Concepts](concepts.md)
- [Implementation Comparison](implementation-comparison.md)
- [Agent Evaluation Findings](findings.md)

The recommended approach is a **Hybrid Workflow**.

The following design decisions apply:

- The repository does not adopt the Spec-Kit CLI or a `.specify/` directory at this stage.
- Selected Spec-Kit concepts are implemented as project-local templates, checks, and validation gates.
- Architecture Discovery runs only when architectural drift is detected.
- Proposed CI checks use standard-library scripts and pytest, consistent with the repository's minimal-dependency approach.
- Steps marked **[Proposed CI]** are recommendations and are not currently implemented.

---

## Workflow Diagram

```text
┌──────────────────────────────────────────┐
│ New Feature Request                      │
└───────────────────┬──────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────┐
│ Gate 0: Is architecture context current? │
└──────────┬───────────────────────┬───────┘
           │ Drift                 │ Current
           ▼                       │
┌──────────────────────────────┐   │
│ Architecture Discovery Agent │   │
│ Refresh context files        │   │
└──────────────┬───────────────┘   │
               │                   │
               ▼                   │
┌──────────────────────────────┐   │
│ Commit refreshed context     │   │
└──────────────┬───────────────┘   │
               └─────────┬─────────┘
                         ▼
┌──────────────────────────────────────────┐
│ Spec Enrichment Agent                    │
│ Input: requirement + context             │
│ Template: specs/_template.md             │
│ Output: specs/<feature>.md               │
└───────────────────┬──────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────┐
│ Validation 1: Specification analysis     │
│ • Required sections present              │
│ • FRs and ACs numbered and testable      │
│ • Project constraints addressed          │
│ • Human review completed                 │
└──────────┬───────────────────────┬───────┘
           │ Fail                  │ Pass
           ▼                       ▼
┌───────────────────────┐   ┌──────────────────────────┐
│ Rework specification  │   │ Commit approved spec     │
└───────────┬───────────┘   └────────────┬─────────────┘
            └───────────────┐             │
                            │             ▼
                            │   ┌──────────────────────────┐
                            │   │ Developer Agent          │
                            │   │ Implement approved spec  │
                            │   │ Update tests             │
                            │   └────────────┬─────────────┘
                            │                │
                            │                ▼
                            │   ┌──────────────────────────┐
                            │   │ Local Testing            │
                            │   │ • Full pytest suite      │
                            │   │ • AC coverage            │
                            │   │ • Determinism check      │
                            │   └──────────┬───────┬───────┘
                            │              │ Fail  │ Pass
                            │              ▼       ▼
                            │   ┌──────────────┐  ┌─────────────────┐
                            │   │ Fix and test │  │ Commit feature  │
                            │   └──────┬───────┘  │ Push branch     │
                            │          └─────────►│ Open PR         │
                            │                     └────────┬────────┘
                            │                              │
                            │                              ▼
                            │   ┌──────────────────────────────────┐
                            │   │ [Proposed CI] PR checks          │
                            │   │ • pytest                         │
                            │   │ • spec-shape lint                │
                            │   │ • scope check                    │
                            │   │ • stdlib import scan             │
                            │   │ • determinism check              │
                            │   └──────────┬───────────────┬───────┘
                            │              │ Fail          │ Pass
                            │              ▼               ▼
                            │   ┌──────────────────┐  ┌─────────────┐
                            │   │ Fix on branch    │  │ Convergence │
                            │   │ CI runs again    │  │ validation  │
                            │   └──────────────────┘  └──────┬──────┘
                            │                                │
                            │                                ▼
                            │                         ┌─────────────┐
                            │                         │ Merge main  │
                            │                         └──────┬──────┘
                            │                                │
                            │                                ▼
                            │   ┌──────────────────────────────────┐
                            └───│ [Proposed CI] Post-merge checks  │
                                │ • Context-drift check            │
                                │ • Citation-staleness check       │
                                └──────────────────────────────────┘
```

---

## Step-by-Step Workflow

### Step 0: Feature Request Intake

A new feature request is received as an issue, ticket, enhancement request, or business requirement.

---

### Step 1: Architecture Context Validation

Verify that the existing architecture context accurately reflects the current repository.

Review:

- Components
- Constraints
- Data sources
- Application workflow
- Testing strategy

**Decision:**

- If the context is current, proceed to Step 3.
- If architectural drift is detected, proceed to Step 2.

---

### Step 2: Architecture Discovery Agent

Run this step only when architectural drift is detected.

Execute the Architecture Discovery Agent to regenerate:

- `context/architecture-overview.md`
- `context/workflow.md`
- `context/constraints.md`
- `context/testing-strategy.md`

The regenerated context must distinguish among:

- Verified facts
- Inferred observations
- Unknowns requiring validation

#### Git Checkpoint 1

**Commit message:**

```text
chore(context): refresh architecture context
```

Keep this commit separate so that architecture-context changes are not mixed with specification or implementation changes.

---

### Step 3: Spec Enrichment Agent

Run the Spec Enrichment Agent using:

- The feature request
- The validated architecture context
- The project-local specification template

The agent creates:

```text
specs/<feature>.md
```

The specification must follow:

```text
specs/_template.md
```

The template should include:

- Feature summary
- Numbered Functional Requirements (`FR-##`)
- Numbered Acceptance Criteria (`AC-##`)
- Dependencies
- Files impacted
- Project constraints
- Test strategy
- Open questions

> **Adoption task:** `specs/_template.md` does not currently exist and must be created when this workflow is implemented.

---

### Step 4: Specification Analysis

Validate the specification before implementation begins. This step applies the Spec-Kit **analyze** concept without using the Spec-Kit CLI.

#### Structural Validation

Confirm that:

- All required template sections are present.
- Functional Requirements are numbered.
- Acceptance Criteria are numbered.
- Each Acceptance Criterion is measurable and testable.
- Dependencies and impacted files are identified.

#### Constraint Validation

Confirm that the specification addresses the project constraints documented in `context/constraints.md`, including:

- Read-only processing
- Deterministic behavior
- No credit decisioning
- Reproducible outputs

#### Human Review

A reviewer must confirm that the specification is:

- Clear
- Complete
- Within scope
- Architecturally compliant
- Testable

**Decision:**

- If validation fails, return to Step 3.
- If validation passes, approve and commit the specification.

#### Git Checkpoint 2

**Commit message:**

```text
docs(spec): add approved spec for <feature>
```

Commit the approved specification before implementation so that the specification becomes the fixed implementation contract.

---

### Step 5: Developer Agent

Run the Developer Agent using:

- The approved feature specification
- The validated architecture context

The Developer Agent must:

- Implement the approved requirements.
- Update or add tests.
- Respect all repository constraints.
- Avoid unrelated changes.
- Modify only files declared in the specification unless an exception is reviewed and approved.

Expected implementation targets may include:

- `fraud_control_view.py`
- `test_fraud_control_view.py`
- Other files explicitly listed under `Files Impacted`

If the generated code is inaccurate, update the Developer Agent instructions and rerun the agent rather than repeatedly asking the same version to correct the code.

---

### Step 6: Local Testing

Run the complete test suite:

```bash
python -m pytest
```

Confirm that:

- The complete test suite passes.
- Each Acceptance Criterion has corresponding test coverage.
- Existing behavior remains unchanged unless modification is explicitly required.
- No unrelated functionality is affected.

Perform a determinism check by running the application twice against the same fixture inputs and comparing the relevant non-manifest outputs.

**Decision:**

- If testing fails, update the Developer Agent as needed and return to Step 5.
- If testing passes, proceed to Step 7.

---

### Step 7: Feature Commit and Pull Request

Commit the implementation on the feature branch.

#### Git Checkpoint 3

**Commit message:**

```text
feat: implement <feature> per specs/<feature>.md
```

Push the branch and open a pull request.

The pull request should reference:

- The approved specification
- The relevant architecture context
- Test results
- Any agent changes made during implementation
- Any known limitations

---

### Step 8: Pull Request Validation

The following checks are recommended as new CI controls.

#### Proposed CI Checks

1. **Test execution**
   - Run the complete pytest suite on every pull request.

2. **Specification-shape validation**
   - Confirm that changed specification files contain all required sections.

3. **Scope validation**
   - Compare files changed in the pull request with the specification's `Files Impacted` section.
   - Flag unexpected files for reviewer attention.

4. **Standard-library import validation**
   - Verify that `fraud_control_view.py` does not introduce unapproved third-party runtime imports.

5. **Determinism validation**
   - Run the application twice against the same inputs.
   - Compare SHA-256 digests of relevant non-manifest outputs.

**Decision:**

- If a check fails, update the branch and rerun CI.
- If all checks pass, proceed to Step 9.

---

### Step 9: Convergence Validation

Perform a final review using the Spec-Kit **converge** concept.

Confirm that:

- Every Functional Requirement is implemented.
- Every Acceptance Criterion is satisfied.
- Every Acceptance Criterion has adequate test coverage.
- The implementation respects the architecture and project constraints.
- The implementation contains no unrelated changes.
- Required documentation is updated.

**Decision:**

- If gaps remain, return to Step 5.
- If the implementation has converged with the approved specification, merge the pull request.

#### Git Checkpoint 4

Merge the approved pull request into:

```text
main
```

---

### Step 10: Post-Merge Maintenance

Run periodic or post-merge validation to detect repository drift.

#### Proposed Post-Merge Checks

1. **Architecture-context drift**
   - Compare documented components with the current repository structure.
   - Flag added, removed, or substantially changed components.

2. **Specification-reference staleness**
   - Check whether source references recorded in specifications remain valid.
   - Prefer stable symbols or named sections over fragile line-number references where practical.

3. **Documentation consistency**
   - Confirm that architecture, specifications, tests, and implementation remain aligned.

If drift is detected, record it as a maintenance item and require Architecture Discovery at Step 2 during the next applicable development cycle.

---

## Workflow Evaluation and Recommendation

### Comparison with the Agent-Only Workflow

The current agent-based workflow is functional, but specification consistency is not enforced. Existing specifications under `specs/` use different structures, which shows that specification quality currently depends on convention and author discipline.

The Hybrid Workflow retains the existing agents while addressing these gaps through selected Spec-Kit concepts:

- A mandatory specification template
- Specification analysis before implementation
- Convergence validation before merge
- CI-based enforcement of workflow rules

The expected benefits are:

- Consistent specification structure
- Improved governance
- Better traceability
- Earlier validation
- Automated quality checks
- No additional application runtime dependencies

The initial adoption requires a project-local specification template and a limited set of validation scripts.

---

### Comparison with the Spec-Kit-Only Workflow

Full Spec-Kit adoption was evaluated but not selected for the current repository.

#### 1. Architecture Discovery Remains Necessary

The Architecture Discovery Agent reverse-engineers architecture, workflows, constraints, and testing strategy from an existing repository.

A specification-driven workflow begins with requirements and specification artifacts. It does not replace the repository-specific Architecture Discovery capability required by this project.

#### 2. Repository Design Philosophy

The repository emphasizes:

- Standard-library-based implementation
- Reproducibility
- Minimal dependencies
- A straightforward contributor experience

Introducing the `specify` CLI and a `.specify/` directory would add contributor-facing tooling and process overhead. That overhead is not currently justified by the repository's scale.

#### 3. Proportionality

The repository currently has:

- One primary production module
- One primary test suite
- A small number of implemented features

A complete process that generates separate specification, plan, and task artifacts for every feature may create more maintenance overhead than value at the current scale.

---

## Final Recommendation

**Recommended approach: Hybrid Workflow**

The Hybrid Workflow combines:

- Architecture Discovery Agent
- Validated architecture context
- Specification templates inspired by Spec-Kit
- Spec Enrichment Agent
- Developer Agent
- Human approval gates
- Automated testing and validation
- Git-based traceability

This approach provides an appropriate balance of:

- Flexibility
- Maintainability
- Governance
- Traceability
- Reproducibility
- Low operational overhead

It also preserves a future migration path. If the repository grows into a multi-team or multi-repository solution, the project can adopt more of the actual Spec-Kit tooling without redesigning the overall workflow.