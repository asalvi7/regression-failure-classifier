"""
Retriever Agent (Agent 1)
-------------------------
Given any unlabeled failure row, finds the top-K most similar past
labeled failures from the knowledge base using semantic similarity.

How it works:
  1. Converts each KB row into a searchable text string
     (FAILURE_REMARKS if available, else STATUS + structured fields)
  2. Encodes all KB rows into vectors using sentence-transformers
  3. Builds a FAISS index for fast cosine similarity search
  4. For each query row → returns top-K similar labeled rows as evidence

Caching:
  - data/cache/embeddings_index.faiss  — FAISS index (skip rebuild)
  - data/cache/embeddings_meta.parquet — KB metadata in index order
  - Use RetrieverAgent(fresh=True) or --fresh flag to force rebuild

Usage (import):
    from agents.ingestion_agent import IngestionAgent
    from agents.retriever_agent import RetrieverAgent

    ingestion = IngestionAgent()
    kb        = ingestion.get_knowledge_base()
    retriever = RetrieverAgent(kb)

    targets   = ingestion.get_target_rows()
    row       = targets.iloc[0]
    neighbors = retriever.query(row, top_k=5)

Usage (standalone):
    python agents/retriever_agent.py          # build index + sample query
    python agents/retriever_agent.py --fresh  # force re-embed
"""

import sys
import time
import numpy as np
import pandas as pd
import faiss
from pathlib import Path
from fastembed import TextEmbedding

CACHE_DIR        = Path(__file__).parent.parent / "data" / "cache"
FAISS_INDEX_PATH = CACHE_DIR / "embeddings_index.faiss"
META_PATH        = CACHE_DIR / "embeddings_meta.parquet"

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
BATCH_SIZE = 256

RETURN_COLS = [
    "TC_ID", "EXEC_ID", "JIRA_ID",
    "INTRIM_STATUS", "AUTO_FAILURE_REASON",
    "FAILURE_REMARKS", "USER_REMARKS",
    "MODULE", "J_COMPONENT", "FUNC_AREA",
]


def _get_text(row: pd.Series) -> str:
    """Build the text string to embed for a single row."""
    remarks = str(row.get("FAILURE_REMARKS", "") or "").strip()
    user    = str(row.get("USER_REMARKS",    "") or "").strip()
    status  = str(row.get("INTRIM_STATUS",   "") or "").strip()

    base = remarks if remarks else f"STATUS:{status}"
    return f"{base} | {user}" if user else base


class RetrieverAgent:

    def __init__(self, knowledge_base: pd.DataFrame, fresh: bool = False):
        print(f"[RetrieverAgent] Loading model: {MODEL_NAME} ...")
        t = time.time()
        self._model = TextEmbedding(MODEL_NAME)
        print(f"[RetrieverAgent] Model loaded ({time.time()-t:.1f}s)")

        self._index = None
        self._meta  = None

        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        if not fresh and FAISS_INDEX_PATH.exists() and META_PATH.exists():
            self._load_index()
        else:
            self._build_index(knowledge_base)

    # ------------------------------------------------------------------
    # Index build / load
    # ------------------------------------------------------------------

    def _build_index(self, kb: pd.DataFrame) -> None:
        total = len(kb)
        print(f"\n[RetrieverAgent] Building index from {total:,} KB rows ...")
        t = time.time()

        # Step 1 — build text per row
        print("[RetrieverAgent] Step 1/3 — Building text representations ...")
        texts         = [_get_text(row) for _, row in kb.iterrows()]
        has_remarks   = sum(1 for r in texts if not r.startswith("STATUS:"))
        print(f"[RetrieverAgent]   Remarks-based: {has_remarks:,}  |  Fallback: {total - has_remarks:,}")

        # Step 2 — encode with progress bar
        t1 = time.time()
        print(f"[RetrieverAgent] Step 2/3 — Encoding {total:,} texts ...")
        embeddings = np.array(list(self._model.embed(texts, batch_size=BATCH_SIZE)), dtype="float32")
        print(f"[RetrieverAgent]   Encoded in {time.time()-t1:.1f}s  | Shape: {embeddings.shape}")

        # Step 3 — build + save FAISS index
        t2 = time.time()
        print("[RetrieverAgent] Step 3/3 — Building FAISS index ...")
        dim        = embeddings.shape[1]
        self._index = faiss.IndexFlatIP(dim)
        self._index.add(embeddings.astype(np.float32))

        faiss.write_index(self._index, str(FAISS_INDEX_PATH))

        keep       = [c for c in RETURN_COLS if c in kb.columns]
        self._meta = kb[keep].reset_index(drop=True)
        self._meta.to_parquet(META_PATH, index=False)

        total_time = time.time() - t
        print(f"[RetrieverAgent]   FAISS index: {self._index.ntotal:,} vectors  ({time.time()-t2:.1f}s)")
        print(f"[RetrieverAgent] Index saved → {FAISS_INDEX_PATH.name}, {META_PATH.name}")
        print(f"[RetrieverAgent] Total build time: {total_time:.1f}s  ({total_time/60:.1f} min)\n")

    def _load_index(self) -> None:
        print("[RetrieverAgent] Loading index from cache ...")
        t = time.time()
        self._index = faiss.read_index(str(FAISS_INDEX_PATH))
        self._meta  = pd.read_parquet(META_PATH)
        print(f"[RetrieverAgent] Index loaded — {self._index.ntotal:,} vectors  ({time.time()-t:.1f}s)")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def query(self, row: pd.Series, top_k: int = 5) -> pd.DataFrame:
        """Returns top_k most similar KB rows for a single target row."""
        text = _get_text(row)
        vec  = np.array(list(self._model.embed([text])), dtype="float32")

        scores, indices = self._index.search(vec, top_k)
        result = self._meta.iloc[indices[0]].copy().reset_index(drop=True)
        result.insert(0, "similarity_score", scores[0].round(4))
        return result

    def query_batch(self, targets: pd.DataFrame, top_k: int = 5) -> list:
        """Bulk search — one pass for all target rows. Returns list of DataFrames."""
        texts = [_get_text(row) for _, row in targets.iterrows()]
        vecs  = np.array(list(self._model.embed(texts, batch_size=BATCH_SIZE)), dtype="float32")

        scores_all, indices_all = self._index.search(vecs, top_k)
        results = []
        for scores, indices in zip(scores_all, indices_all):
            df = self._meta.iloc[indices].copy().reset_index(drop=True)
            df.insert(0, "similarity_score", scores.round(4))
            results.append(df)
        return results


# ------------------------------------------------------------------
# Standalone run
# ------------------------------------------------------------------

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from agents.ingestion_agent import IngestionAgent

    fresh = "--fresh" in sys.argv

    print("="*60)
    print("  RETRIEVER AGENT — BUILD & TEST")
    print("="*60)
    t_total = time.time()

    ingestion = IngestionAgent(fresh=fresh)
    kb        = ingestion.get_knowledge_base()
    targets   = ingestion.get_target_rows()
    ingestion.close()

    retriever = RetrieverAgent(kb, fresh=fresh)

    print("\n" + "="*60)
    print("  SAMPLE SEARCH — 3 random target rows")
    print("="*60)
    sample = targets.sample(3, random_state=42)
    for i, (_, row) in enumerate(sample.iterrows(), 1):
        print(f"\n--- Target {i} ---")
        print(f"  TC_ID          : {row.get('TC_ID')}")
        print(f"  INTRIM_STATUS  : {row.get('INTRIM_STATUS')}")
        print(f"  FAILURE_REMARKS: {str(row.get('FAILURE_REMARKS', ''))[:120]}")
        print(f"  Top-5 matches:")
        for _, n in retriever.query(row, top_k=5).iterrows():
            print(f"    [{n['similarity_score']:.3f}]  {n['AUTO_FAILURE_REASON']:<15}  "
                  f"{str(n.get('FAILURE_REMARKS', ''))[:70]}")

    print(f"\n{'='*60}")
    print(f"  Total wall-clock: {(time.time()-t_total)/60:.1f} min")
    print(f"{'='*60}")
