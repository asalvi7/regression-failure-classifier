# Session — FAILURE_REMARKS Filter & Pipeline Simplification

> Domain update: empty FAILURE_REMARKS means the test passed directly, triggering a pipeline simplification that cuts target rows by ~76%.

---

## 2026-06-29 — Empty remarks = passed, pipeline simplified

### Context

During a review of how the pipeline handles the ~76% of target rows that have no `FAILURE_REMARKS`, a new domain clarification came from the team: **an empty `FAILURE_REMARKS` column is not a missing value — it is a signal that the test passed directly**. There is nothing to classify for those rows.

Previously, the system treated empty remarks as "weak signal" and tried to classify these rows anyway using `INTRIM_STATUS` domain rules (fast path) or a degraded LLM call with fallback `STATUS:<INTRIM_STATUS>` text.

### Decisions Made

- **Filter empty-remarks rows out at ingestion, not downstream** — Why: `IngestionAgent` is the single data source. One filter there propagates automatically to all agents without touching retriever, analyst, or decision logic. Any other placement (e.g. in the pipeline runner itself) would be a second enforcement point that could get out of sync.

- **Remove the fast path entirely** — Why: The fast path (`INTRIM_STATUS` → label, LLM skipped, confidence 0.95) existed solely to handle the empty-remarks case efficiently. With those rows gone at ingestion, the fast path is dead code. Keeping dead code with a clever optimization label creates confusion — future readers would wonder why it's there and whether it still fires. It was removed cleanly.

- **Do not carve out exceptions for any INTRIM_STATUS value** — Confirmed by the team that this applies to all empty-remarks rows without exception, including `INTRIM_STATUS=FAILED`. A `FAILED` status with no error text also means a direct pass. No nuance needed.

### Logic & Approach

The framing that unlocked the change: `FAILURE_REMARKS` being empty is not a data quality problem — it is a **domain fact**. The framework only writes to `FAILURE_REMARKS` when there is a failure to report. Empty = the framework had nothing to report = the test didn't fail in a way that produced an error message = classify as passed.

This shifts the question from "how do we classify rows with no signal?" to "should we even be looking at these rows?" The answer is no.

### What Was Done

- `IngestionAgent.get_target_rows()` now applies a third filter: `FAILURE_REMARKS` must be non-empty. Target row count drops from ~226,080 to ~55,000 (the ~24% of rows that have actual error text).
- `pipeline_runner.py` fast path removed. The per-row loop is now a single straight path: retrieve → LLM → decide → save. The `INTRIM_RULES` and `CATEGORY_MAP` imports (used only by the fast path) were also removed.
- `CLAUDE.md` updated to reflect the new single execution path.

### Tradeoffs

The main thing given up is speed for the rows that had a clear `INTRIM_STATUS` rule — those were previously classified in under a second without an LLM call. But those rows are now excluded entirely (they passed), so there's nothing to be fast about. No real tradeoff.

A risk worth noting: if the domain understanding is ever revised again (e.g. "empty remarks with `INTRIM_STATUS=FAILED` should still be classified"), the filter is in one place (`get_target_rows()`) and easy to adjust. The pipeline runner does not need to change.

### Relationships

- Connects to the `RetrieverAgent._get_text()` fallback: the `STATUS:<INTRIM_STATUS>` fallback text was introduced for KB rows with no remarks and is still valid there. It's now dead for target rows, but removing it from `_get_text()` would break KB indexing for any KB rows with empty remarks. Left in place intentionally.
- The `INTRIM_RULES` map in `decision_agent.py` is still active — `DecisionAgent` still uses it as a hard-override check on the LLM output. It was only the pipeline-runner-level fast path (which bypassed the LLM entirely) that was removed.
- Reduces the scale problem: 226K rows at 20–60s/row was going to be a blocker. 55K rows is still slow but materially more tractable. Speed optimization (Groq, batching) remains an open item but is less urgent.

### Open Questions

- The ~171K empty-remarks rows are now simply ignored. Should a separate pass write `PASSED` (or a new sentinel label) into their `AUTO_FAILURE_REASON` column so the DB reflects that they've been reviewed and skipped — rather than looking indefinitely unclassified?
- With all target rows now having FAILURE_REMARKS, the LOW-SIGNAL retrieval path (similarity < 0.80) in `AnalystAgent` is still possible (error text present but not matching anything in KB well). Is the current behavior — "rely on domain rules, reduce neighbor weight" — still the right fallback, or should low-similarity results now trigger `YET-TO-ANALYZE` directly?
