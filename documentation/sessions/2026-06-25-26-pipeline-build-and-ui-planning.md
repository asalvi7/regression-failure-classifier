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

---

## 2026-06-26 — Dashboard Wired to Real Data, Speed Optimized

### Context

With the UI design direction decided, this session built the full working dashboard: Flask backend serving the Claude Design template, real classification data flowing through the API, and several data quality issues fixed that only appeared when looking at real rows in the UI.

### Decisions Made

- **Flask serves both static files and JSON API** — **Why:** No separate frontend build step, no webpack, no node. The template (`index.html` + `support.js`) is served as static files from `dashboard/`. The DC runtime (support.js) handles all React rendering in the browser. This keeps the backend trivially simple — two real API endpoints plus a catch-all static route. Considered FastAPI but Flask was already familiar and the API is tiny.

- **Switch Analyst model from llama3.1 (8B) to llama3.2:3b** — **Why:** Speed. llama3.1 averaged ~42s/row, llama3.2:3b averages ~23s/row — roughly 1.8× faster with no perceptible quality difference on structured classification with an explicit system prompt. The task is rule-following, not open-ended reasoning, so the smaller model is sufficient.

- **Fast-path: skip LLM entirely for empty-remarks rows** — **Why:** 55.1% of the 226K target rows (124,486 rows) have no FAILURE_REMARKS. For those rows, vector similarity retrieval is noise — there is nothing to embed, so cosine similarity scores cluster around 30% regardless of label. For rows where INTRIM_STATUS maps to a hard domain rule, we can apply that rule directly with 0.95 confidence and skip both retrieval and LLM. This cut projected total time from ~110 days to ~26 hours for the full dataset. The fast-path only fires when: (a) FAILURE_REMARKS is empty AND (b) INTRIM_STATUS has a hard rule.

- **Store all 5 individual FAISS scores, not just the top score** — **Why:** The initial pipeline stored only `top_similarity`. The UI needs per-neighbor scores to show the similarity bar chart in the expanded TC card. Adding `neighbor_ids` and `neighbor_scores` columns required a schema change and re-run, which is why this was deferred until the UI revealed the gap.

- **Approve/override is in-memory only (not persisted to DB)** — **Why:** The interaction model is designed but the backend write endpoint doesn't exist yet. The UI allows accept/override and shows a "Reviewed" confirmation, but closing the page loses that state. This is an accepted limitation at this stage — the primary goal was to demonstrate the classification output, not build a full human review workflow.

### Logic & Approach

Several data quality issues only surfaced when looking at real rows in the UI, not from inspecting the DB directly:

- **"nan" in Failure Remarks**: Python's `float('nan')` serializes to the string `"nan"`, which is truthy so the `or ""` fallback didn't catch it. Both the pipeline (save path) and Flask API needed an explicit `.lower() == "nan"` check.
- **KB-1/KB-2 placeholders**: The initial pipeline only stored the top neighbor's score. The detail card showed "KB-1, KB-2…" because there was no TC-ID data to show. Required storing the full list of neighbor TC-IDs.
- **Logo 404**: The logo at `agents/Logo/MO_Logo.png` is outside the `dashboard/` directory Flask served. Fixed with a dedicated `/uploads/MO_Logo.png` route pointing to the correct path.
- **"nan" status shown in Failure Remarks**: Empty remarks showed "nan" because `str(float('nan'))` passes a truthiness check that `"" or fallback` doesn't catch.

The pattern here is a principle worth noting: **the UI is a data quality checker**. Issues that were invisible in raw DB rows became immediately obvious when displayed. Running the UI after any pipeline change is a cheap way to catch silent data problems.

### What Was Done

- Built `dashboard/app.py` — Flask server with `/api/builds` (summary list) and `/api/builds/<exec_id>` (test case detail) endpoints, `parse_neighbors()` function that maps real TC-IDs and scores to the UI's neighbor format
- Added `neighbor_ids` and `neighbor_scores` columns to the SQLite schema in `pipeline_runner.py`
- Implemented fast-path in the pipeline runner (no LLM, no retrieval, direct domain-rule application)
- Fixed "nan" remarks in both pipeline save path and Flask API response
- Switched Analyst model to `llama3.2:3b`
- Wired JIRA URL to `https://mediaocean.atlassian.net/browse/`

### Tradeoffs

The fast-path skips retrieval entirely for empty-remarks rows. This is correct when INTRIM_STATUS is decisive (OOS, MAINTAINED, BLOCKED), but could be wrong for rows where INTRIM_STATUS is ambiguous (SCHEDULED, IN-PROGRESS, NOT-STARTED) and there's no FAILURE_REMARKS. Those rows currently fall through to the full LLM path with empty context, which produces low-signal classifications. Worth revisiting whether USER_REMARKS alone can serve as the embedding text for those cases.

Approve/override not persisting to DB is a real gap. If this tool is used for regular regression runs, engineers will want their review decisions to carry forward and potentially feed back into the knowledge base as new labeled examples.

### Relationships

- Speed decisions connect directly to the 226K scale problem discussed in the previous session
- The fast-path uses `INTRIM_RULES` and `CATEGORY_MAP` imported from `decision_agent.py` — this creates a direct coupling between the pipeline runner and the decision agent's domain rules. If rules change, fast-path behavior changes automatically.
- `neighbor_ids` / `neighbor_scores` schema change is backwards-incompatible — the DB must be re-created from scratch if you have existing results (the pipeline drops and recreates the table on each run, so this is safe)

### Open Questions

1. Should USER_REMARKS be used as the embedding text for rows where FAILURE_REMARKS is empty but INTRIM_STATUS is ambiguous? This could improve classification quality for ~30% of empty-remarks rows.
2. The approve/override needs a `PATCH /api/classifications/<id>` endpoint to persist decisions. Should confirmed rows be added back to the FAISS knowledge base?
3. All 20 sample rows were classified ACCEPT. Running on 100+ rows will likely surface REVIEW and REJECT cases — those are the interesting edge cases to examine.

---

## 2026-06-26 — Dashboard UI Readability Polish

### Context

After the dashboard was working and showing real data, two rounds of size increases were applied to make all text legible at normal browser zoom. The initial Claude Design template used sizes (9–12px) appropriate for a dense analytics tool on a large monitor — too small for comfortable reading in a demo or review context.

### Decisions Made

- **Increase every text layer systematically, not just headlines** — **Why:** The first ask was "increase card size." We increased headline numbers (to 72px) but left labels, sublabels, column headers, row text, and expanded-card content at their original small sizes. This created a mismatch: huge numbers, tiny context. The correct fix was to increase all layers proportionally. Final sizes: card labels 16px, card subtext 15px, column headers 13–15px, table row text 13–15px, expanded detail 13–14px.

- **Widen the Status column from 85px → 120px, add `white-space:nowrap`** — **Why:** "NOT-STARTED" is 11 characters and was wrapping to two lines inside the 85px column. This looked broken and misaligned the entire row. The fix is both the column width (to fit the longest possible status value) and explicit `white-space:nowrap` on the badge so it can never wrap regardless of column width.

- **Increase TC row height from 54px → 68px** — **Why:** With 14–15px text the previous 54px row was too cramped. 68px gives comfortable vertical padding without making the table feel sparse.

### What Was Done

- Main dashboard view: card label text 11px → 16px, card subtext 11px → 15px, column headers 10px → 13px, build name 14px → 20px, build ID/date 11–12px → 14–16px, failed count subtext 9px → 13px, AI % 12px → 18px, "need review" text 11px → 14px, taxonomy legend 10–11px → 13–14px
- Detail view: build header name 15px → 20px, build stats 18px → 24px, sidebar section headers 10px → 13px, inputs/selects 12px → 14px, table column headers 10px → 15px, TC row cells 11–12px → 13–15px, expanded card headers 10px → 13px, reasoning text 12px → 14px, action buttons 12px → 14px
- Status column: 85px → 120px, `white-space:nowrap` on badge

### Tradeoffs

Larger text means the table needs more horizontal space. The `min-width` on the table container was increased from 920px to 960px, and the page will scroll horizontally on narrow viewports. This is acceptable — this tool is meant to be used on a laptop or monitor, not on a phone.

The TC row grid columns are now fixed pixel widths (not flexible). If very long test names appear, they truncate with ellipsis. This is the right behavior for a data table but means some names may not be fully visible without expanding the row.

### Open Questions

1. At the current sizes, the Status column still shows badges inline. If a longer status value is added in future (the DB has "IN-PROGRESS", "NOT-STARTED", "SCHEDULED" etc.), 120px should still be sufficient — but worth verifying when running more data.
2. The sidebar is currently fixed at 248px. With larger text in sidebar labels, it may start feeling tight. May need to widen to 280px when more filter options are added.
