# 2026-06-25/26 — Pipeline Build, Output Storage, UI Planning

> End-to-end pipeline completed and validated; taxonomy upgraded; output storage decided; UI requirements defined.

---

## 2026-06-25 — Agents 2 & 3 Built, Pipeline Running

### Context

Ingestion and Retriever agents were complete. This session built the remaining two agents (Analyst and Decision Maker), wired them into a pipeline runner, chose an output storage mechanism, and ran the first real end-to-end classification on 20 target rows.

### Decisions Made

- **Ollama + Llama 3.1 for the Analyst Agent (Agent 2)** — **Why:** Free, runs entirely locally on the user's Mac, no API key or cost. The alternative (Anthropic Claude API) would cost ~$175–$1,300 to classify all 226K rows. Llama 3.1 8B is sufficient for structured classification with explicit domain rules in the system prompt.

- **SQLite for output storage** — **Why:** The user has DBeaver installed and wanted a local DB separate from the company's Oracle instance. SQLite is zero-setup — just a file. DBeaver connects to it directly. No server, no credentials, fully portable. PostgreSQL and Oracle XE were considered but rejected as over-engineered for a local results store.

- **Two-agent reasoning split: Analyst suggests, Decision confirms** — **Why:** Separating suggestion from decision makes the pipeline independently testable at each stage. The Analyst can change models without touching the confidence gate logic. The Decision Agent applies hard INTRIM_STATUS rules as a final override — a business rule layer that must not be mixed with LLM inference.

- **Hard INTRIM_STATUS overrides in Decision Agent** — **Why:** Some INTRIM_STATUS values are near-certain signals regardless of what the LLM suggests. OOS → APP-CHANGE, MAINTAINED → SYNC-ISSUE, BLOCKED → PERF-ISSUE. These override the Analyst to prevent the LLM from arguing against domain facts. This created a visible tension: Row 1 (TC_ID 32394) had OOS status but 5 perfect-match DATA-ISSUE neighbors — the rule overrode the neighbors. Whether this is correct needs manager validation.

- **Confidence gate: ACCEPT ≥ 0.85, REVIEW 0.60–0.84, REJECT < 0.60** — **Why:** Not all predictions are equally trustworthy. REVIEW rows get labeled but flagged for human spot-check. REJECT rows get YET-TO-ANALYZE and must be investigated manually. This prevents low-confidence labels from silently entering the system.

### Logic & Approach

The system prompt for the Analyst encodes all domain rules explicitly — it is not asking the LLM to infer rules from examples, it is giving the LLM rules and asking it to apply them. This is a deliberate choice: the rules are known (from manager conversations), so they should be stated, not learned. The retrieval examples exist to handle cases the rules don't cover cleanly.

The Decision Agent is intentionally rule-based, not LLM-based. It is a deterministic layer. If the Analyst returns a label the domain rules disagree with, the rules win. This keeps the most critical business logic out of the probabilistic LLM path.

### What Was Done

- Built `agents/analyst_agent.py` — Ollama client, structured system prompt with domain rules, JSON output parsing with fallback handling
- Built `agents/decision_agent.py` — confidence gate, hard INTRIM_STATUS overrides, category mapping
- Built `pipeline_runner.py` — end-to-end loop, SQLite init, progress logging, summary stats
- Ran first 20-row classification: 15 ACCEPT, 5 REVIEW, 0 REJECT. Labels: APP-CHANGE (8), SCRIPT-ISSUE (7), APP-ISSUE (5)
- Output visible in DBeaver at `results/classifications.db`

### Tradeoffs

Llama 3.1 on CPU takes ~40 seconds per row. For 226K rows that is ~104 days — not practical at full scale. The 20-row validation run was the right first step, but the speed problem must be solved before scaling. Three approaches identified: (1) fast path skipping LLM for clear INTRIM_STATUS cases, (2) smaller/faster model (llama3.2:3b), (3) Groq free tier (~1-2s per row).

The hard override rule creates a tension: for rows where INTRIM_STATUS and retrieval neighbors disagree, the rule wins unconditionally. This is correct for clear cases (OOS is almost always APP-CHANGE) but may be wrong for edge cases like Row 1 where the error text and all neighbors point to DATA-ISSUE despite OOS status. The threshold for when rules should yield to evidence is an open question.

### Open Questions

1. Should the OOS hard rule override retrieval when all 5 neighbors agree on a different label with score 1.000? Row 1 (TC_ID 32394) is the test case for this.
2. What is the right confidence threshold? 0.85/0.60 are hypotheses — should be validated on labeled rows where we know the answer.

---

## 2026-06-25 — Taxonomy Upgraded: ENV-ISSUE Split

### Context

The manager transcript confirmed that "System Issue" has two distinct sub-types — Performance and Sync — that the team tracks separately. The existing knowledge base stores both as ENV-ISSUE. This session split them.

### Decisions Made

- **Split ENV-ISSUE → PERF-ISSUE + SYNC-ISSUE** — **Why:** The manager explicitly distinguished: Performance = intermittent, passes on rerun/locally, timing-related. Sync = definitive failure, fix was applied, INTRIM_STATUS=MAINTAINED. Keeping them merged loses information the team cares about.

- **Split logic in Ingestion Agent, not downstream** — **Why:** The same reason all filtering lives in IngestionAgent — one place to own the rule. The split uses INTRIM_STATUS (MAINTAINED → SYNC-ISSUE) and USER_REMARKS keyword matching (passed locally/on rerun → PERF-ISSUE, sync → SYNC-ISSUE).

- **Default ambiguous ENV-ISSUE rows to PERF-ISSUE** — **Why:** Performance issues are more common (intermittent failures are the most frequent ENV-ISSUE sub-type). When neither INTRIM_STATUS nor USER_REMARKS provides a clear signal, PERF-ISSUE is the safer default. This is a heuristic and may need tuning.

- **Two-level output: category + root_cause** — **Why:** The report portal the team uses already groups by these two levels (Product Bug → APP-ISSUE/APP-CHANGE, etc.). Mirroring that structure makes our output immediately readable to engineers who know the portal. The SQLite table and UI both store both fields.

### Logic & Approach

The ENV-ISSUE split is the most uncertain part of the taxonomy change. The 21,546 ENV-ISSUE rows in the knowledge base were split using heuristics — INTRIM_STATUS and keyword matching — not by re-reading each case. This means some rows will be misclassified between PERF and SYNC. The FAISS index was rebuilt with the new labels.

Notably, the first 20-row run produced zero PERF-ISSUE or SYNC-ISSUE results. This is because the 20 sampled rows happened to have statuses (OOS, MAINTAIN, FAILED, SCHEDULED) that map to other categories. PERF/SYNC labels will appear on rows with MAINTAINED or BLOCKED status, or with relevant USER_REMARKS text.

### Relationships

- Directly triggered by the manager transcript shared in this session
- Affects the FAISS index — rebuilt from scratch with new label distribution
- Affects the Analyst system prompt — now includes PERF-ISSUE vs SYNC-ISSUE differentiation rules
- Affects the Decision Agent — MAINTAINED → SYNC-ISSUE, BLOCKED → PERF-ISSUE

---

## 2026-06-26 — UI Direction Decided

### Context

The user shared screenshots of their company's ReportPortal instance to give a reference for the UI layout. This session decided what to build and what to show.

### Decisions Made

- **Build a separate UI, do not push to ReportPortal** — **Why:** The company's portal is a live production tool with other active projects. Pushing AI classifications into it would mix validated human labels with AI predictions. The user explicitly confirmed: no modifications to the company portal, new UI only.

- **Mirror the portal's two-view structure** — **Why:** Engineers already know the layout (launches list → drill into test cases). Using the same mental model reduces adoption friction. The portal validated that this is the right UX for this type of data.

- **Show both category AND root cause in the UI** — **Why:** Category (Product Bug / Auto Bug / System Issue / To Investigate) gives the high-level picture for managers. Root cause (APP-ISSUE, SCRIPT-ISSUE, etc.) gives engineers the actionable detail. The report portal does the same.

- **Show confidence, reasoning, and top-5 neighbors in the drill-down** — **Why:** This is what makes our UI different from the portal. Engineers can see *why* the AI chose a label, not just *what* it chose. This is essential for the human review workflow — an engineer can't meaningfully approve or override a label they can't explain.

### What Was Done

- Defined full UI requirements: two views (launches + test case detail), filter panel, confidence badges, inline expansion with reasoning + neighbor evidence + approve/override
- Written Claude Design prompt for UI mockup

### Open Questions

1. Grouping by build requires EXEC_ID. Our current 20 results all have EXEC_ID populated — confirm this holds for all 226K target rows before building the launches view.
2. The approve/override workflow needs a backend endpoint to update the SQLite record and potentially re-add confirmed rows to the knowledge base. Not yet designed.
3. Speed problem still unresolved — fast path or Groq needed before the UI can show real data at scale.
