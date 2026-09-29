# Feature: Datasets Loaded Indicator

## Feature Summary

Add a new metadata pill, `"<count> datasets loaded"`, to the evidence-pack HTML
header, alongside the existing `version`, `payload <digest>`, and
`<n> days to audit` pills. The count is derived at render time from the
number of `LOADED` entries in the run's source manifest (`manifest["inputs"]`),
so it always agrees with the `status` column of the **Source manifest**
table rendered later in the same document. No new data is collected; the
feature only surfaces a count that already exists in the manifest.

## Functional Requirements

- FR-01: `render_html()` shall compute a `datasets_loaded` count equal to the
  number of entries in `manifest["inputs"]` whose `status` field equals
  `"LOADED"`.
- FR-02: The header (`<p class="sub">...</p>`) shall render an additional
  `<span class="pill">` containing the text `"{datasets_loaded} datasets
  loaded"`, using the same `_esc()` escaping convention used by the other
  pills.
- FR-03: The new pill shall be positioned in the existing pill sequence
  (tool/version, "no credit decisions", payload digest, days-to-audit),
  either immediately after the payload-digest pill or immediately after the
  days-to-audit pill (implementation's choice), without removing or
  reordering the other pills.
- FR-04: The count shall be computed from `manifest["inputs"]` (or an
  equivalent source-of-truth already available to `render_html`, e.g.
  `report.sources.files`), not from a literal/hard-coded integer and not by
  re-deriving it independently of the Source Manifest data used later in the
  same function.
- FR-05: The pill shall always render (unlike the days-to-audit pill, which
  is conditional on `params.audit_date`), since the source manifest is always
  present and non-empty for a valid run.
- FR-06: No other computed value in `render_html`, `build_manifest`, or the
  `Report`/`SourceSet` pipeline shall be modified.

## Acceptance Criteria

- AC-01: The report header displays a metadata indicator showing the total
  number of source datasets loaded for the report run, formatted as
  `"<count> datasets loaded"`.
- AC-02: The datasets-loaded indicator appears alongside the existing
  version, payload-digest, and days-to-audit metadata pills without altering
  their content, order-independence, or the report's existing calculations
  or layout/CSS behavior.
- AC-03: The displayed count equals the number of rows in the **Source
  manifest** table (`manifest["inputs"]`) whose `status` column reads
  `LOADED`.

## Dependencies

- `manifest["inputs"]`, produced by `build_manifest()` from
  `report.sources.files[name].manifest_entry()` (see
  [fraud_control_view.py](../fraud_control_view.py#L197) and
  [fraud_control_view.py](../fraud_control_view.py#L2260)) — already
  available as a parameter to `render_html(report, manifest)`.
- `SourceFile.status` values (`LOADED` | `MISSING`), set in `read_table()`
  (see [fraud_control_view.py](../fraud_control_view.py#L191)).
- Existing `_esc()` HTML-escaping helper and `.pill` CSS class (`HTML_STYLE`),
  reused as-is — no new CSS or helper functions required.
- No dependency on any new source file, CLI argument, or third-party
  package.

## Constraints

- Must not hard-code the count; it must be computed from loaded manifest
  entries at render time.
- Must not introduce any wall-clock timestamp or non-deterministic value
  into the HTML payload (consistent with the tool's "no wall-clock in hashed
  payload" guarantee — see [fraud_control_view.py](../fraud_control_view.py#L28)).
- Must preserve monthly idempotency and deterministic byte-for-byte output:
  for a fixed set of input bytes, the new pill's text must not change
  between reruns, and must not change the
  `deterministic_payload_sha256` calculation logic itself (only the payload
  content it hashes, as with any other header change).
- Must not alter existing report calculations, summary metrics, table
  contents, or the Source Manifest table's existing columns/rows.
- Must not change the signature of `render_html()` or introduce new
  parameters — the manifest data needed is already passed in.
- Must remain a pure function of `report` and `manifest` (no I/O, no
  `datetime.now()`/`utcnow()` calls).

## Test Approach

Extend `test_fraud_control_view.py` (existing suite already asserts against
`manifest["inputs"]`, e.g. lines ~486–517) with tests that:

1. **Visibility**: render the HTML for a sample report/manifest and assert
   the string `"datasets loaded"` appears in the header markup (e.g. within
   the same `<p class="sub">` block as the other pills).
2. **Correctness against Source Manifest**: independently count entries in
   `manifest["inputs"]` with `status == "LOADED"`, and assert the rendered
   pill text matches `f"{expected_count} datasets loaded"`.
3. **Dynamic derivation (not hard-coded)**: construct two manifests with
   differing numbers of `LOADED` entries (e.g. by toggling one optional
   source to `MISSING`, mirroring the existing missing-file test fixtures)
   and assert the rendered count differs accordingly between the two runs.
4. **Regression / non-interference**: assert existing header content
   (tool name/version pill, "no credit decisions" pill, payload-digest pill,
   days-to-audit pill when `audit_date` is set) is still present and
   unchanged, and that the Source Manifest table's rows/columns are
   unaffected.
5. **Determinism**: render the same report/manifest twice and assert the
   produced HTML strings are byte-identical (extending the existing
   `test_rerunning_in_place_does_not_change_the_payload_digest`-style
   coverage) to confirm no new non-determinism was introduced.

## Implementation Considerations

- Compute the count once near the top of `render_html()`, alongside the
  other `report.summary_value(...)` lookups (e.g.
  `datasets_loaded = sum(1 for entry in manifest["inputs"] if entry["status"] == "LOADED")`),
  and reuse it both in the new pill and — since the value already exists in
  `manifest["inputs"]` — implicitly validate it against the later Source
  Manifest table render loop (same underlying list, single source of truth).
- Follow the existing unconditional-pill pattern (like the payload-digest
  pill) rather than the conditional days-to-audit pattern, since the count
  should always be shown.
- Keep the change localized to `render_html()`; no changes to
  `build_manifest()`, `load_sources()`, `SourceFile`, or CLI argument parsing
  are required.
- Files impacted: `fraud_control_view.py` (render_html), `test_fraud_control_view.py`
  (new/extended tests). No changes anticipated to `generate_sample_data.py`
  or the `data/` fixtures.