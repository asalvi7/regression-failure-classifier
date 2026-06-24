"""
Retriever Agent
---------------
Agent 1 in the RAG pipeline.

For every target row, finds the top-K most similar past failures from
the labeled knowledge base using semantic (vector) similarity.

Two-phase design:
  1. Index phase  — embed all KB rows once, store as FAISS index on disk
  2. Query phase  — embed a target row, return top-K labeled neighbors

Text strategy:
  - Primary : FAILURE_REMARKS  (machine-generated error text)
  - Fallback : INTRIM_STATUS   (when FAILURE_REMARKS is empty)
  If USER_REMARKS is also present, it is appended to provide extra context.

Embedding model: all-MiniLM-L6-v2 (384 dims, fast on CPU, good on short text)
Vector index:    FAISS FlatIP   (inner product on L2-normalised vectors = cosine)

Cache layout (data/cache/):
  embeddings_index.faiss   — FAISS index
  embeddings_meta.parquet  — KB rows in index order (for label lookup)

Usage (import):
    from agents.ingestion_agent import IngestionAgent
    from agents.retriever_agent import RetrieverAgent

    ingestion = IngestionAgent()
    kb        = ingestion.get_knowledge_base()
    retriever = RetrieverAgent(kb)                # builds / loads index

    targets   = ingestion.get_target_rows()
    row       = targets.iloc[0]
    neighbors = retriever.query(row, top_k=5)
    # returns DataFrame with label, similarity_score, and key fields

Usage (standalone):
    python agents/retriever_agent.py          # build index + sample query
    python agents/retriever_agent.py --fresh  # force re-embed
"""

import os
import sys
import time
import numpy as np
import pandas as pd
import faiss
from pathlib import Path
from sentence_transformers import SentenceTransformer

# Prevents HuggingFace tokenizer from spawning worker processes, which
# causes a segfault on macOS when encoding large corpora with torch 2.x.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

CACHE_DIR        = Path(__file__).parent.parent / "data" / "cache"
FAISS_INDEX_PATH = CACHE_DIR / "embeddings_index.faiss"
META_PATH        = CACHE_DIR / "embeddings_meta.parquet"

MODEL_NAME = "all-MiniLM-L6-v2"

# Columns returned to downstream agents for each retrieved neighbor
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

    if remarks:
        base = remarks
    else:
        base = f"STATUS:{status}"

    if user:
        return f"{base} | {user}"
    return base


class RetrieverAgent:

    def __init__(self, knowledge_base: pd.DataFrame, fresh: bool = False):
        self._model = SentenceTransformer(MODEL_NAME)
        self._index: faiss.IndexFlatIP | None = None
        self._meta:  pd.DataFrame | None      = None

        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        if not fresh and FAISS_INDEX_PATH.exists() and META_PATH.exists():
            self._load_index()
        else:
            self._build_index(knowledge_base)

    # ------------------------------------------------------------------
    # Index build / load
    # ------------------------------------------------------------------

    def _build_index(self, kb: pd.DataFrame) -> None:
        print(f"[RetrieverAgent] Building embeddings index over {len(kb):,} KB rows ...")
        t = time.time()

        texts = [_get_text(row) for _, row in kb.iterrows()]

        embeddings = self._model.encode(
            texts,
            batch_size=64,
            show_progress_bar=True,
            convert_to_numpy=True,
            normalize_embeddings=True,   # L2 normalise → dot product = cosine
        )

        dim   = embeddings.shape[1]
        index = faiss.IndexFlatIP(dim)
        index.add(embeddings.astype("float32"))

        faiss.write_index(index, str(FAISS_INDEX_PATH))

        # Store only the columns we need for lookup
        keep = [c for c in RETURN_COLS if c in kb.columns]
        kb[keep].to_parquet(META_PATH, index=False)

        self._index = index
        self._meta  = kb[keep].reset_index(drop=True)

        print(f"[RetrieverAgent] Index built  — {len(kb):,} vectors, dim={dim}  ({time.time()-t:.1f}s)")
        print(f"[RetrieverAgent] Saved → {FAISS_INDEX_PATH.name}, {META_PATH.name}")

    def _load_index(self) -> None:
        print("[RetrieverAgent] Loading index from cache ...")
        self._index = faiss.read_index(str(FAISS_INDEX_PATH))
        self._meta  = pd.read_parquet(META_PATH)
        print(f"[RetrieverAgent] Index loaded  — {self._index.ntotal:,} vectors")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def query(self, row: pd.Series, top_k: int = 5) -> pd.DataFrame:
        """
        Returns the top_k most similar KB rows for the given target row.

        Result DataFrame columns:
            similarity_score  float  (0-1, higher = more similar)
            + all RETURN_COLS present in the index metadata
        """
        text = _get_text(row)
        vec  = self._model.encode(
            [text],
            convert_to_numpy=True,
            normalize_embeddings=True,
        ).astype("float32")

        scores, indices = self._index.search(vec, top_k)

        result = self._meta.iloc[indices[0]].copy().reset_index(drop=True)
        result.insert(0, "similarity_score", scores[0].round(4))
        return result

    def query_batch(self, targets: pd.DataFrame, top_k: int = 5) -> list[pd.DataFrame]:
        """
        Bulk version of query — encodes all rows at once (faster for large batches).
        Returns a list of DataFrames, one per target row.
        """
        texts = [_get_text(row) for _, row in targets.iterrows()]
        vecs  = self._model.encode(
            texts,
            batch_size=64,
            show_progress_bar=True,
            convert_to_numpy=True,
            normalize_embeddings=True,
        ).astype("float32")

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
    from agents.ingestion_agent import IngestionAgent

    fresh = "--fresh" in sys.argv

    ingestion = IngestionAgent()
    kb        = ingestion.get_knowledge_base()
    targets   = ingestion.get_target_rows()
    ingestion.close()

    retriever = RetrieverAgent(kb, fresh=fresh)

    print("\n" + "="*60)
    print("  RETRIEVER AGENT — SAMPLE QUERIES")
    print("="*60)

    sample = targets.sample(3, random_state=42)
    for i, (_, row) in enumerate(sample.iterrows(), 1):
        print(f"\n--- Target {i} ---")
        print(f"  TC_ID          : {row.get('TC_ID')}")
        print(f"  INTRIM_STATUS  : {row.get('INTRIM_STATUS')}")
        print(f"  FAILURE_REMARKS: {str(row.get('FAILURE_REMARKS', ''))[:120]}")
        print(f"\n  Top-5 neighbors:")
        neighbors = retriever.query(row, top_k=5)
        for _, n in neighbors.iterrows():
            print(f"    [{n['similarity_score']:.3f}] {n['AUTO_FAILURE_REASON']:<15} | "
                  f"{str(n.get('FAILURE_REMARKS', ''))[:80]}")

    print("\n" + "="*60)
