# 2026-06-24 — Data Analysis & Training Data Filters

> What we learned about the real shape of the data, the platform filter requirement, and why our training set is smaller than we thought.

---

### Context

We had the DB tables and a working understanding of the schema from the previous session. This session was about going deeper — actually running numbers, building the analysis notebook, connecting to the live Oracle DB, and extracting a second round of requirements from a manager transcript that hadn't been fully processed yet.

Two things changed our mental model significantly: the FAILURE_REMARKS sparsity is real (not a snapshot issue), and the training data needs to be filtered by platform before use.

---

### Decisions Made

- **Use only Prisma/Ignitia EXEC_IDs as training data** — **Why:** The 1P platform had a different execution model where reruns happened on separate VMs whose records were never written back to the DB. This means the DB has only the initial failure for 1P tests, not the full investigation result. Training on these rows would teach the classifier incomplete patterns. Manager explicitly said to exclude 1P.

- **Exclude rows where J_COMPONENT starts with "OP"** — **Why:** OP components belong to the 1P execution model. Even if the EXEC_ID isn't obviously "1P", the OP component tag is a reliable signal that the record has the same incompleteness problem.

- **Do not predict DATA-ISSUE from FAILURE_REMARKS text** — **Why:** DATA-ISSUE labels were assigned *after* engineers reran tests locally and found the data was wrong. The initial FAILURE_REMARKS for these rows shows a Java exception — which looks like SCRIPT-ISSUE. The label diverges from the raw error text because it reflects post-investigation knowledge, not the framework output. Attempting to classify DATA-ISSUE from FAILURE_REMARKS will systematically misclassify it as SCRIPT-ISSUE.

- **FAILURE_REMARKS sparsity is permanent, not a snapshot problem** — **Why:** The CSV was downloaded directly from the live DB. The 59-row coverage is the real state. FAILURE_REMARKS is only populated by the framework for specific failure types, not all runs. The architecture must be built around this reality, not the hope that it improves.

---

### Logic & Approach

The key mental shift this session: we stopped thinking of the 6,104 labeled rows as a clean training set and started treating them as raw data that needs its own filtering pass before it can be used.

The platform issue is subtle. On the surface, the DB has labeled records with `AUTO_FAILURE_REASON` filled in — so they look usable. But the label was assigned based on a local rerun whose results were never captured in the DB. The record in the DB reflects only the first execution, and the label reflects a second investigation done outside the system. Using these rows would teach the classifier to associate the wrong failure text with a label.

The DATA-ISSUE case is a concrete example of this: the FAILURE_REMARKS shows a Java throwable (SCRIPT-ISSUE signal), but the AUTO_FAILURE_REASON is DATA-ISSUE. This isn't a labeling error — it's correct — but the classifier can't learn this mapping from FAILURE_REMARKS alone because the signal that justified the DATA-ISSUE label (the local rerun behavior) is invisible in the DB.

The practical consequence: the FAILURE_REMARKS-based similarity approach only works reliably for categories where the framework error text genuinely predicts the label (APP-ISSUE, APP-CHANGE, SCRIPT-ISSUE to some extent). DATA-ISSUE classification needs USER_REMARKS or INTRIM_STATUS, even though USER_REMARKS is "manual" — it's the only place the investigation evidence lives.

---

### What Was Done

- Built `analysis.ipynb` with step-by-step derivation of the 24 target rows, fill rate tables, and classification status breakdown
- Connected to the live Oracle DB (`AUTOBOT_DEV` on `ny-oracle-ee11:1521`) using `oracledb` in thin mode
- Credentials stored in `.env`, excluded from git via `.gitignore`
- Extracted and documented the platform filter requirement (Prisma/Ignitia vs 1P) and component filter (OP exclusion)

---

### Tradeoffs

The platform filter will reduce the usable training set below 6,104. We don't yet know by how much — that depends on how many of the labeled rows come from 1P EXEC_IDs vs Prisma. If a large proportion are 1P, the labeled knowledge base shrinks significantly, which hurts the retrieval agent's coverage.

Not filtering would be simpler but would corrupt the classifier with incomplete-label rows. Filtering is the right call even if it costs training data volume.

---

### Relationships

- Builds on: [2026-06-23 Requirements & Design](2026-06-23-requirements-and-design.md)
- Affects: the Retriever agent's knowledge base size (was assumed to be 6,104, now smaller after filtering)
- Affects: the Decision on [Multi-Agent over ML](../decisions/multi-agent-over-ml.md) — the DATA-ISSUE finding reinforces why pure text-based ML doesn't work: the signal that determines the label isn't always in the text

---

### Open Questions

1. How many of the 6,104 labeled rows survive the Prisma/Ignitia filter? Need to check EXEC_ID patterns against known platform identifiers.
2. Which EXEC_IDs specifically correspond to 1P? The manager referenced 1P vs Prisma but didn't give a prefix or list — need to confirm with the DB or manager.
3. DATA-ISSUE classification: if FAILURE_REMARKS can't predict it and USER_REMARKS is "manual", how do we handle DATA-ISSUE for new failures where no engineer has written remarks yet? May need to flag these as "Yet to be analyzed" by default.

---

## 2026-06-24 — IngestionAgent: Decisions Codified into Code

### Context

After establishing the filtering rules conceptually, we built the `IngestionAgent` — the single data-access layer that owns all DB connectivity and serves clean, filtered DataFrames to every downstream agent. The goal was to close the gap between "decisions we understand" and "decisions the system enforces."

### Decisions Made

- **Single agent owns all DB access** — **Why:** Every downstream agent (retriever, classifier, reporter) needs data. Centralizing the connection and query logic in one place means filtering rules are applied consistently and can't drift. No agent fetches raw data directly.

- **`get_knowledge_base()` implements the three filters as code, not docs** — **Why:** The Prisma/OP/PASSED exclusions are load-bearing decisions — getting them wrong corrupts the retrieval reference. Encoding them directly in the method (not leaving them as "the caller should filter") ensures they're always applied. The filters are: (1) must have `AUTO_FAILURE_REASON`, (2) exclude `J_COMPONENT` starting with `OP`, (3) exclude `INTRIM_STATUS = PASSED`.

- **`build_reference_table()` as a left join, not inner** — **Why:** `TC_MASTER` rows may not have a corresponding `HIS_EXEC_REPORT` entry, but we want to preserve all execution rows. A left join from `HIS_EXEC_REPORT` onto `TC_MASTER` ensures no execution record is dropped due to a missing master entry.

- **Standalone `__main__` block for sanity checks** — **Why:** The agent needs to be testable without a full multi-agent pipeline. A runnable script that fetches both views and prints samples lets us verify the DB connection and filter counts before wiring agents together.

### Logic & Approach

The agent is a deliberately thin wrapper: it connects, fetches, joins, and filters — no business logic, no classification, no LLM calls. This matters architecturally because it makes the data layer independently testable and replaceable. If the DB schema changes or we switch to a different source (e.g., CSV snapshots for offline work), only this agent changes.

The two public surfaces — `get_knowledge_base()` and `get_target_rows()` — represent the two sides of the classification problem: what we know (labeled, filtered training examples) and what we need to label (the 24 unclassified failures). Downstream agents only interact with these two views, never raw tables.

### What Was Done

- Built `agents/ingestion_agent.py` with `IngestionAgent` class
- Implemented `fetch_tc_master()`, `fetch_his_exec_report()`, `build_reference_table()` (left join), `get_knowledge_base()` (with all three filters), and `get_target_rows()`
- `agents/__init__.py` created (empty, for package import)
- Standalone `__main__` runner for connection testing and output sampling

### Tradeoffs

The OP-prefix filter (J_COMPONENT starts with "OP") is a heuristic, not a precise platform identifier. There is a small risk that legitimate non-1P components might coincidentally start with "OP". This was judged acceptable given the manager's explicit guidance, but the exact filter criteria should be confirmed once a definitive 1P component list is available.

The 1P EXEC_ID filter is not yet implemented — we're using the OP-component proxy because the precise EXEC_ID pattern for 1P hasn't been confirmed. This is a known gap.

### Relationships

- Directly implements decisions from [this session's earlier section](#decisions-made) and [multi-agent architecture decision](../decisions/multi-agent-over-ml.md)
- `get_knowledge_base()` output feeds the Retriever agent (next to build)
- `get_target_rows()` output feeds the classification pipeline
- Notebook (`analysis.ipynb`) still uses raw CSV/direct DB queries for interactive exploration — the agent is for production pipeline use, not the notebook

### Open Questions

1. The OP-component filter should be validated against the actual 1P component list once obtained — confirm no legitimate Prisma/Ignitia components start with "OP".
2. If we get a definitive EXEC_ID pattern for 1P, add a fourth filter to `get_knowledge_base()` keyed on EXEC_ID rather than component name.
3. Should `build_reference_table()` cache its result to avoid double-fetching when both `get_knowledge_base()` and `get_target_rows()` are called in the same pipeline run?
