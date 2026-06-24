"""
Ingestion Agent
---------------
Connects to the live Oracle DB and exposes TC_MASTER and HIS_EXEC_REPORT
as DataFrames. Acts as the single data source for all other agents.

Usage (import):
    from agents.ingestion_agent import IngestionAgent
    agent = IngestionAgent()
    tc_master    = agent.fetch_tc_master()
    his_exec     = agent.fetch_his_exec_report()
    reference_df = agent.build_reference_table()
    agent.close()

Usage (standalone):
    python agents/ingestion_agent.py
"""

import os
import oracledb
import pandas as pd
from dotenv import load_dotenv

load_dotenv()


class IngestionAgent:

    def __init__(self):
        self.connection = None
        self._connect()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self):
        user     = os.getenv("DB_USER")
        password = os.getenv("DB_PASSWORD")
        host     = os.getenv("DB_HOST")
        port     = os.getenv("DB_PORT", "1521")
        service  = os.getenv("DB_SERVICE")

        if not all([user, password, host, service]):
            raise EnvironmentError(
                "Missing DB credentials. Ensure .env has DB_USER, DB_PASSWORD, "
                "DB_HOST, DB_PORT, DB_SERVICE."
            )

        dsn = f"{host}:{port}/{service}"
        self.connection = oracledb.connect(user=user, password=password, dsn=dsn)
        print(f"[IngestionAgent] Connected → {host}/{service} as {user}")

    def close(self):
        if self.connection:
            self.connection.close()
            print("[IngestionAgent] Connection closed.")

    # ------------------------------------------------------------------
    # Table fetchers
    # ------------------------------------------------------------------

    def _query_to_df(self, query: str) -> pd.DataFrame:
        """Execute a query and return results as a DataFrame using cursor (no SQLAlchemy needed)."""
        with self.connection.cursor() as cursor:
            cursor.execute(query)
            columns = [col[0] for col in cursor.description]
            rows    = cursor.fetchall()
        return pd.DataFrame(rows, columns=columns)

    def fetch_tc_master(self) -> pd.DataFrame:
        """Fetch all rows from TC_MASTER."""
        df = self._query_to_df("SELECT * FROM TC_MASTER")
        print(f"[IngestionAgent] TC_MASTER        → {len(df):,} rows, {len(df.columns)} columns")
        return df

    def fetch_his_exec_report(self) -> pd.DataFrame:
        """Fetch all rows from HIS_EXEC_REPORT."""
        df = self._query_to_df("SELECT * FROM HIS_EXEC_REPORT")
        print(f"[IngestionAgent] HIS_EXEC_REPORT  → {len(df):,} rows, {len(df.columns)} columns")
        return df

    # ------------------------------------------------------------------
    # Reference table
    # ------------------------------------------------------------------

    def build_reference_table(self) -> pd.DataFrame:
        """
        Join TC_MASTER + HIS_EXEC_REPORT on TC_ID into one flat table.
        This is the master reference used by all downstream agents.
        """
        tc  = self.fetch_tc_master()
        his = self.fetch_his_exec_report()

        reference = his.merge(
            tc[["TC_ID", "MODULE", "J_COMPONENT", "AUTOMATED_TC_ID",
                "AUTOMATED_BY_USERID", "FUNC_AREA", "CONTINENT"]],
            on="TC_ID",
            how="left",
            suffixes=("", "_master")
        )

        print(f"[IngestionAgent] Reference table   → {len(reference):,} rows, {len(reference.columns)} columns")
        return reference

    # ------------------------------------------------------------------
    # Filtered subsets (used by downstream agents)
    # ------------------------------------------------------------------

    def get_knowledge_base(self) -> pd.DataFrame:
        """
        Returns the clean, filtered labeled rows used as the classifier's
        knowledge base (training / retrieval reference).

        Filters applied:
          1. Must have AUTO_FAILURE_REASON (labeled by an engineer)
          2. Exclude J_COMPONENT starting with 'OP' (1P platform — unreliable labels)
          3. Exclude INTRIM_STATUS = 'PASSED' (no failure to learn from)
        """
        df = self.build_reference_table()

        # Filter 1 — must be labeled
        df = df[df["AUTO_FAILURE_REASON"].fillna("").str.strip() != ""]

        # Filter 2 — exclude OP components (1P platform)
        df = df[~df["J_COMPONENT"].fillna("").str.upper().str.startswith("OP")]

        # Filter 3 — exclude rows with PASSED status (no failure signal)
        df = df[df["INTRIM_STATUS"] != "PASSED"]

        print(f"[IngestionAgent] Knowledge base    → {len(df):,} rows after filtering")
        return df.reset_index(drop=True)

    def get_target_rows(self) -> pd.DataFrame:
        """
        Returns the unlabeled non-PASSED rows that the classifier must predict.

        Filters applied:
          1. No AUTO_FAILURE_REASON (unclassified)
          2. INTRIM_STATUS != PASSED (actual failures, not passing tests)
        """
        df = self.build_reference_table()

        df = df[df["AUTO_FAILURE_REASON"].fillna("").str.strip() == ""]
        df = df[df["INTRIM_STATUS"] != "PASSED"]

        print(f"[IngestionAgent] Target rows       → {len(df):,} rows to classify")
        return df.reset_index(drop=True)


# ------------------------------------------------------------------
# Standalone run — quick sanity check
# ------------------------------------------------------------------

if __name__ == "__main__":
    agent = IngestionAgent()

    print("\n--- Knowledge Base ---")
    kb = agent.get_knowledge_base()
    print(kb[["TC_ID", "JIRA_ID", "INTRIM_STATUS", "AUTO_FAILURE_REASON", "J_COMPONENT"]].head(5))

    print("\n--- Target Rows ---")
    targets = agent.get_target_rows()
    print(targets[["TC_ID", "JIRA_ID", "INTRIM_STATUS", "FAILURE_REMARKS", "USER_REMARKS"]].to_string())

    agent.close()
