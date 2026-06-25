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

---

## 2026-06-24 — Caching, Dirty Labels, and Retriever Agent

### Context

The Ingestion Agent built earlier in the session had three latent problems: it fetched both tables twice per pipeline run (once for knowledge base, once for targets), had no disk cache so the 4-minute DB pull happened every run, and passed dirty labels (typos, free-text entries, noise) directly into the knowledge base without cleaning them. This session fixed all three and then built the Retriever Agent — the first agent that actually performs semantic search.

### Decisions Made

- **Three-level caching hierarchy for IngestionAgent** — **Why:** The DB pull takes ~4 minutes for 785K rows. In a development loop, re-fetching on every run kills iteration speed. The hierarchy: in-memory (instant, within a session) → reference table parquet (seconds, across sessions) → raw table parquet (backup if only one table changes). A `fresh=True` flag bypasses all three for intentional re-fetches.

- **Dirty label normalization at the ingestion boundary, not downstream** — **Why:** The knowledge base is the foundation of all retrieval. If it contains `APP-CHNAGE`, `DatA-ISSUE`, `Sync Issue`, those become training examples with broken labels that bias every similarity search. Normalizing at the ingestion boundary (via a `LABEL_MAP` dict) means every downstream agent sees only canonical labels. Noise labels (`DAY-1`, `IMPROVEMENT`, free-text entries) are dropped entirely — they add no classification signal and dilute the index.

- **fastembed (ONNX runtime) over sentence-transformers (PyTorch)** — **Why:** First attempt used sentence-transformers + PyTorch 2.2.2, which segfaulted after ~36 batches on Mac due to a NumPy 2.x vs NumPy 1.x binary incompatibility in the C extension layer. Downgrading NumPy to 1.x fixed the import but the segfault persisted under load. fastembed uses ONNX runtime (no PyTorch dependency), handles the same model (`all-MiniLM-L6-v2`), and is stable on Mac. The output vectors are identical; the runtime is different.

- **`all-MiniLM-L6-v2` as the embedding model** — **Why:** It's the standard choice for semantic similarity at this scale — small enough to run locally on CPU, fast enough to embed 62K rows in reasonable time, and well-tuned for sentence-level similarity (which is what FAILURE_REMARKS is). The 384-dimension output fits comfortably in a FAISS flat index.

- **FAISS IndexFlatIP (inner product) on L2-normalized vectors** — **Why:** Inner product on L2-normalized vectors is mathematically equivalent to cosine similarity. FAISS FlatIP does exact search (no approximation), which matters when the KB is only 62K rows — the accuracy loss from approximate indexing is not worth the complexity at this scale.

- **Fallback text for rows without FAILURE_REMARKS** — **Why:** ~29K of the 62K KB rows have no FAILURE_REMARKS. Rather than exclude them from the index (losing half the KB), we construct a synthetic text string from INTRIM_STATUS + structured fields. This is weaker than real error text, but it preserves those rows as retrieval candidates for target rows that also lack FAILURE_REMARKS — making the fallback consistent on both sides.

### Logic & Approach

The embedding index is built once and cached to disk. Every subsequent pipeline run loads it in seconds. The `fresh` flag on both agents provides an escape hatch when the KB grows (new labeled rows added) and the index needs to be regenerated.

The dirty label problem deserves more attention than it gets in most pipelines. The knowledge base is only as good as the labels in it. Twenty-plus known dirty values — typos from manual labeling, free-text entries, deprecated category names — were mapped to canonical labels or dropped. Any value not in `VALID_LABELS` after normalization is discarded. This is aggressive but correct: a mislabeled example in the retrieval results is worse than a missing one, because it generates plausible-sounding but wrong evidence for the Analyst.

### What Was Done

- **IngestionAgent** rewritten with 3-level caching, `LABEL_MAP` (20+ dirty → canonical mappings), `_normalize_labels()` method, and `summary()` for quick stats
- **RetrieverAgent** built: text extraction logic, fastembed-based encoding over 62K KB rows, FAISS FlatIP index, `query()` (single row) and `query_batch()` (bulk) methods, disk cache for index + metadata
- pyarrow installed for parquet support; NumPy downgraded to 1.x for PyTorch compatibility; transformers pinned to 4.44.0

### Tradeoffs

fastembed downloads and caches the ONNX model on first load (~80 MB). This is a one-time cost, but in air-gapped environments it would need to be pre-staged.

The fallback text for empty FAILURE_REMARKS rows produces weaker embeddings — `STATUS:FAILED | MODULE` is not semantically rich. For target rows that also lack FAILURE_REMARKS, the retrieval will be driven by INTRIM_STATUS similarity, which effectively means the top-5 results share the same status rather than the same failure pattern. The Analyst agent needs to be aware of this and weight INTRIM_STATUS-based retrievals differently than text-based ones.

### Open Questions

1. ~~How long does the embedding run take for 62K rows on fastembed/ONNX?~~ **Answered: 20.3 min** (1212s encoding + 0.4s FAISS build). Cached now — future runs load in seconds.
2. Should the retrieval fall back to INTRIM_STATUS-only rule-based matching for rows with no FAILURE_REMARKS, rather than embedding a weak text string? The Analyst may be better served by knowing "no text signal available" than by receiving low-confidence semantic neighbors.
3. The `query_batch()` method exists for bulk classification runs but isn't tested yet — should be validated against `query()` on a sample before the Analyst agent uses it.

---

## 2026-06-24 — Retriever Validated: Two Distinct Retrieval Modes

### Context

The Retriever index finished building (20.3 min, 62,143 vectors). The sample output revealed a clear split in retrieval quality that directly shapes how Agent 2 must be written.

### What the Results Showed

Three sample target rows produced two completely different retrieval patterns:

**Pattern A — High-signal (text match):** TC_ID 32394, `INTRIM_STATUS=OOS`, `FAILURE_REMARKS` contains "NUnit.Framework.AssertionException: Row 1 mismatch found between expected and actual files." → All 5 neighbors returned `similarity_score=1.000`, all labeled `DATA-ISSUE`, all with identical FAILURE_REMARKS text. The retrieval is functionally exact — the same test failure exists in the labeled KB with a known label. Agent 2 barely needs to reason here; it should trust the neighbors almost unconditionally.

**Pattern B — Low-signal (status fallback):** TC_IDs 294 and 1111, both with `FAILURE_REMARKS=None`. All 5 neighbors also had no remarks and shared the same INTRIM_STATUS. Similarity scores were 0.62–0.75 — not wrong, but not meaningful. The retrieval tells us nothing beyond "other rows with the same status exist in the KB." The neighbors are structurally similar, not semantically similar.

### Decision Made

- **Agent 2 must branch on retrieval confidence, not treat all neighbors equally** — **Why:** Treating a 1.000-score retrieval the same as a 0.62-score retrieval would either over-trust useless neighbors or under-trust perfect ones. The threshold that separates signal from noise is approximately 0.80 based on what we saw. Above that: neighbors are strong evidence. Below that: Agent 2 should route to INTRIM_STATUS domain rules and treat retrieval as weak supporting context only.

### Implication for Agent 2 Prompt Design

Two distinct reasoning paths are needed in the system prompt:

1. **High-confidence path** (similarity ≥ 0.80, FAILURE_REMARKS present): "Multiple labeled examples match closely. Weight the neighbor labels heavily. Confirm with FAILURE_REMARKS text and INTRIM_STATUS. If all neighbors agree, that is the label."
2. **Low-confidence path** (similarity < 0.80, no FAILURE_REMARKS): "No strong text match found. Rely on INTRIM_STATUS domain rules: FAILED→APP-ISSUE, OOS→APP-CHANGE, MAINTAIN→SCRIPT-ISSUE. Treat retrieved neighbors as weak supporting evidence only."

This is a critical design decision — without it, Agent 2 will hallucinate confident reasoning from weak retrieval.

### Open Questions

1. What is the right confidence threshold? 0.80 is a hypothesis from 3 samples. Should be validated on a larger sample (e.g., 50–100 rows with known labels) before being hardcoded in the Agent 2 prompt.
2. For Pattern A rows (perfect match, score=1.000), do we even need Agent 2/3? Could the pipeline short-circuit: if top-1 score ≥ 0.99 and all 5 neighbors agree on the label → assign directly with high confidence?
