"""
Ingestion Agent
---------------
Connects to the live Oracle DB and exposes TC_MASTER and HIS_EXEC_REPORT
as clean, filtered DataFrames. Acts as the single data source for all
other agents in the RAG pipeline.

Features:
  - Fetches once and caches in memory (no double DB hits)
  - Saves to local parquet cache so subsequent runs skip the 4-min pull
  - Normalises dirty AUTO_FAILURE_REASON labels before returning knowledge base
  - Applies all platform/component filters in one place

Usage (import):
    from agents.ingestion_agent import IngestionAgent

    agent = IngestionAgent()           # uses local cache if available
    agent = IngestionAgent(fresh=True) # forces re-fetch from DB

    kb      = agent.get_knowledge_base()   # 62K labeled rows (clean)
    targets = agent.get_target_rows()      # 226K unlabeled rows
    agent.close()

Usage (standalone):
    python agents/ingestion_agent.py          # uses cache
    python agents/ingestion_agent.py --fresh  # re-fetches from DB
"""

import os
import sys
import time
import oracledb
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ------------------------------------------------------------------
# Paths
# ------------------------------------------------------------------
CACHE_DIR       = Path(__file__).parent.parent / "data" / "cache"
TC_CACHE        = CACHE_DIR / "tc_master.parquet"
HIS_CACHE       = CACHE_DIR / "his_exec_report.parquet"
REFERENCE_CACHE = CACHE_DIR / "reference_table.parquet"

# ------------------------------------------------------------------
# Dirty label normalization map
# Maps any non-standard AUTO_FAILURE_REASON value → canonical label
# ------------------------------------------------------------------
LABEL_MAP = {
    # Typos / case variants
    "DATA-ISSUEDATA-ISSUE" : "DATA-ISSUE",
    "DatA-ISSUE"           : "DATA-ISSUE",
    "APP-CHNAGE"           : "APP-CHANGE",
    "Script Issue"         : "SCRIPT-ISSUE",
    "Sync Issue"           : "SYNC-ISSUE",
    "Sync issue"           : "SYNC-ISSUE",
    "SCRIPT-MAINTAIN"      : "SCRIPT-ISSUE",
    "SCRIPT-MAINTAINED"    : "SCRIPT-ISSUE",
    "DATA-MAINTAINED"      : "DATA-ISSUE",
    # Free-text remarks accidentally saved as labels
    "Passed on rerun"      : "PERF-ISSUE",
    "Passed locally"       : "PERF-ISSUE",
    "passed manually"      : "PERF-ISSUE",
    "Locally passed"       : "PERF-ISSUE",
    "MAINTAINED"           : "SYNC-ISSUE",
    "MAINTAIN"             : "SCRIPT-ISSUE",
    "BLOCKED"              : "PERF-ISSUE",
    "OOS"                  : "APP-CHANGE",
    # Noise labels — exclude from knowledge base
    "NOT-RUN"              : None,
    "DAY-1"                : None,
    "IMPROVEMENT"          : None,
    "Baseline updated for Print CI" : None,
    "BASELINE Updated"     : None,
    "Table locator issue, updated same" : None,
    "Moved Test case to maintaince"     : None,
}

# Valid canonical labels — rows with anything else are dropped from KB
VALID_LABELS = {
    "APP-ISSUE",
    "APP-CHANGE",
    "SCRIPT-ISSUE",
    "DATA-ISSUE",
    "PERF-ISSUE",
    "SYNC-ISSUE",
}

# Category map — groups root causes into top-level categories
CATEGORY_MAP = {
    "APP-ISSUE"     : "Product Bug",
    "APP-CHANGE"    : "Product Bug",
    "DATA-ISSUE"    : "Auto Bug",
    "SCRIPT-ISSUE"  : "Auto Bug",
    "PERF-ISSUE"    : "System Issue",
    "SYNC-ISSUE"    : "System Issue",
    "YET-TO-ANALYZE": "To Investigate",
}

# Keywords in USER_REMARKS that signal a Performance Issue
PERF_KEYWORDS = {
    "passed on rerun", "passed locally", "locally passed",
    "passed manually", "slowness", "slow", "intermittent",
    "flaky", "timing", "timeout", "passed in local",
}

# Keywords in USER_REMARKS that signal a Sync Issue
SYNC_KEYWORDS = {
    "sync", "synchronization", "synchronisation",
}


class IngestionAgent:

    def __init__(self, fresh: bool = False):
        self.connection  = None
        self._reference  = None   # in-memory cache
        self._fresh      = fresh
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._connect()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self):
        user    = os.getenv("DB_USER")
        password= os.getenv("DB_PASSWORD")
        host    = os.getenv("DB_HOST")
        port    = os.getenv("DB_PORT", "1521")
        service = os.getenv("DB_SERVICE")

        if not all([user, password, host, service]):
            raise EnvironmentError(
                "Missing DB credentials. Ensure .env has DB_USER, DB_PASSWORD, "
                "DB_HOST, DB_PORT, DB_SERVICE."
            )

        dsn = f"{host}:{port}/{service}"
        self.connection = oracledb.connect(user=user, password=password, dsn=dsn)
        print(f"[IngestionAgent] Connected  → {host}/{service} as {user}")

    def close(self):
        if self.connection:
            self.connection.close()
            print("[IngestionAgent] Connection closed.")

    # ------------------------------------------------------------------
    # Internal: DB fetch
    # ------------------------------------------------------------------

    def _fetch_from_db(self, query: str, label: str) -> pd.DataFrame:
        t = time.time()
        print(f"[IngestionAgent] Fetching {label} from DB ...")
        with self.connection.cursor() as cur:
            cur.execute(query)
            cols = [c[0] for c in cur.description]
            rows = cur.fetchall()
        df = pd.DataFrame(rows, columns=cols)
        print(f"[IngestionAgent] {label:<22} → {len(df):,} rows  ({time.time()-t:.1f}s)")
        return df

    # ------------------------------------------------------------------
    # Table fetchers (with parquet cache)
    # ------------------------------------------------------------------

    def fetch_tc_master(self) -> pd.DataFrame:
        if not self._fresh and TC_CACHE.exists():
            print(f"[IngestionAgent] TC_MASTER             → loaded from cache")
            return pd.read_parquet(TC_CACHE)
        df = self._fetch_from_db("SELECT * FROM TC_MASTER", "TC_MASTER")
        df.to_parquet(TC_CACHE, index=False)
        return df

    def fetch_his_exec_report(self) -> pd.DataFrame:
        if not self._fresh and HIS_CACHE.exists():
            print(f"[IngestionAgent] HIS_EXEC_REPORT       → loaded from cache")
            return pd.read_parquet(HIS_CACHE)
        df = self._fetch_from_db("SELECT * FROM HIS_EXEC_REPORT", "HIS_EXEC_REPORT")
        df.to_parquet(HIS_CACHE, index=False)
        return df
    

    # ------------------------------------------------------------------
    # Reference table (fetched once, cached in memory + disk)
    # ------------------------------------------------------------------

    def build_reference_table(self) -> pd.DataFrame:
        """
        Join TC_MASTER + HIS_EXEC_REPORT on TC_ID.
        Result is cached in memory so subsequent calls are instant.
        """
        if self._reference is not None:
            return self._reference

        if not self._fresh and REFERENCE_CACHE.exists():
            print(f"[IngestionAgent] Reference table       → loaded from cache")
            self._reference = pd.read_parquet(REFERENCE_CACHE)
            return self._reference

        tc  = self.fetch_tc_master()
        his = self.fetch_his_exec_report()

        self._reference = his.merge(
            tc[["TC_ID", "MODULE", "J_COMPONENT", "AUTOMATED_TC_ID",
                "AUTOMATED_BY_USERID", "FUNC_AREA", "CONTINENT"]],
            on="TC_ID",
            how="left",
            suffixes=("", "_master")
        )

        self._reference.to_parquet(REFERENCE_CACHE, index=False)
        print(f"[IngestionAgent] Reference table       → {len(self._reference):,} rows, "
              f"{len(self._reference.columns)} columns")
        return self._reference

    # ------------------------------------------------------------------
    # Label normalization
    # ------------------------------------------------------------------

    def _normalize_labels(self, df: pd.DataFrame) -> pd.DataFrame:
        """Apply LABEL_MAP, split ENV-ISSUE → PERF-ISSUE/SYNC-ISSUE, drop noise."""
        df = df.copy()
        df["AUTO_FAILURE_REASON"] = (
            df["AUTO_FAILURE_REASON"]
            .str.strip()
            .replace(LABEL_MAP)
        )
        # Drop rows whose label maps to None (noise labels)
        df = df[df["AUTO_FAILURE_REASON"].notna()]

        # Split remaining ENV-ISSUE rows using INTRIM_STATUS + USER_REMARKS
        env_mask = df["AUTO_FAILURE_REASON"] == "ENV-ISSUE"
        if env_mask.any():
            df.loc[env_mask, "AUTO_FAILURE_REASON"] = df[env_mask].apply(
                self._classify_env_issue, axis=1
            )

        # Drop rows whose label is still not in the valid set
        df = df[df["AUTO_FAILURE_REASON"].isin(VALID_LABELS)]
        return df

    def _classify_env_issue(self, row: pd.Series) -> str:
        """Split ENV-ISSUE into PERF-ISSUE or SYNC-ISSUE using available signals."""
        status  = str(row.get("INTRIM_STATUS",  "") or "").strip().upper()
        remarks = str(row.get("USER_REMARKS",   "") or "").strip().lower()

        # MAINTAINED = fix was applied, passed after → Sync Issue
        if status == "MAINTAINED":
            return "SYNC-ISSUE"

        # USER_REMARKS contains sync keywords → Sync Issue
        if any(kw in remarks for kw in SYNC_KEYWORDS):
            return "SYNC-ISSUE"

        # USER_REMARKS contains performance keywords → Perf Issue
        if any(kw in remarks for kw in PERF_KEYWORDS):
            return "PERF-ISSUE"

        # Default ambiguous ENV-ISSUE → PERF-ISSUE (most common sub-type)
        return "PERF-ISSUE"

    # ------------------------------------------------------------------
    # Public API — filtered subsets for downstream agents
    # ------------------------------------------------------------------

    def get_knowledge_base(self) -> pd.DataFrame:
        """
        Clean, filtered, label-normalized labeled rows.
        Used by Agent 1 (Retriever) as the searchable knowledge base.

        Filters:
          1. Must have AUTO_FAILURE_REASON
          2. Label normalized + invalid/noise labels dropped
          3. J_COMPONENT must not start with 'OP' (1P platform — unreliable)
          4. INTRIM_STATUS must not be 'PASSED'
        """
        df = self.build_reference_table()

        # Filter 1 — must be labeled
        df = df[df["AUTO_FAILURE_REASON"].fillna("").str.strip() != ""].copy()

        # Filter 2 — normalize + drop noise labels
        df = self._normalize_labels(df)

        # Filter 3 — exclude OP components (1P platform)
        df = df[~df["J_COMPONENT"].fillna("").str.upper().str.startswith("OP")]

        # Filter 4 — no failure signal on PASSED rows
        df = df[df["INTRIM_STATUS"] != "PASSED"]

        print(f"[IngestionAgent] Knowledge base        → {len(df):,} rows (clean, labeled)")
        return df.reset_index(drop=True)

    def get_target_rows(self) -> pd.DataFrame:
        """
        Unlabeled rows with failure text that the classifier must predict.

        Filters:
          1. No AUTO_FAILURE_REASON
          2. INTRIM_STATUS not PASSED
          3. FAILURE_REMARKS must be present — empty remarks means the test
             passed directly, so there is nothing to classify
        """
        df = self.build_reference_table()

        df = df[df["AUTO_FAILURE_REASON"].fillna("").str.strip() == ""]
        df = df[df["INTRIM_STATUS"] != "PASSED"]
        df = df[df["FAILURE_REMARKS"].fillna("").str.strip() != ""]

        print(f"[IngestionAgent] Target rows           → {len(df):,} rows to classify")
        return df.reset_index(drop=True)

    def summary(self):
        """Print a quick summary of both datasets."""
        ref = self.build_reference_table()
        kb  = self.get_knowledge_base()
        tgt = self.get_target_rows()

        print("\n" + "="*55)
        print("  INGESTION AGENT — DATA SUMMARY")
        print("="*55)
        print(f"  Total rows (reference table) : {len(ref):>10,}")
        print(f"  Knowledge base (labeled)     : {len(kb):>10,}")
        print(f"  Target rows (to classify)    : {len(tgt):>10,}")
        print()
        print("  Knowledge base — label distribution:")
        for label, cnt in kb["AUTO_FAILURE_REASON"].value_counts().items():
            print(f"    {label:<20} : {cnt:,}")
        print()
        print("  Target rows — INTRIM_STATUS breakdown:")
        for status, cnt in tgt["INTRIM_STATUS"].value_counts().items():
            print(f"    {status:<20} : {cnt:,}")
        print("="*55)


# ------------------------------------------------------------------
# Standalone run
# ------------------------------------------------------------------

if __name__ == "__main__":
    fresh = "--fresh" in sys.argv
    agent = IngestionAgent(fresh=fresh)
    agent.summary()
    agent.close()
