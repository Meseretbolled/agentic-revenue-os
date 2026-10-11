"""LLM spend ledger with a hard cap that fails closed (see migration 0003).

A call reserves its worst-case cost under one lock before it runs, so two concurrent calls cannot
both squeeze under the cap. If the ledger cannot be read or written the call is not made.
"""

import psycopg

from revos.domain.errors import SpendCapExceeded

_SPEND_LOCK = 727_002


class SpendLedger:
    def __init__(self, conn: psycopg.Connection, max_spend_usd: float,
                 reserve_per_call_usd: float) -> None:
        self._conn = conn
        self.max_spend_usd = max_spend_usd
        self.reserve_per_call_usd = reserve_per_call_usd

    def spent_usd(self) -> float:
        row = self._conn.execute("SELECT coalesce(sum(coalesce(cost_usd, reserved_usd)), 0) "
                                 "FROM llm_spend WHERE status <> 'failed'").fetchone()
        return float(row[0])

    def reserve(self, task: str, model: str) -> int:
        reserve = self.reserve_per_call_usd
        try:
            with self._conn.transaction():
                self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SPEND_LOCK,))
                if self.spent_usd() + reserve > self.max_spend_usd:
                    raise SpendCapExceeded(
                        f"spend cap of ${self.max_spend_usd:.2f} reached; call not made")
                return self._conn.execute(
                    "INSERT INTO llm_spend (task, model, reserved_usd) VALUES (%s, %s, %s) "
                    "RETURNING id", (task, model, reserve)).fetchone()[0]
        except SpendCapExceeded:
            raise
        except psycopg.Error as exc:       # cannot prove we are under the cap, so do not call
            raise SpendCapExceeded(f"spend ledger unavailable ({type(exc).__name__}); "
                                   "call not made") from exc

    def budget_for_call(self) -> float:
        """What this call may spend: the cap minus everything else (its own reserve excluded)."""
        others = self.spent_usd() - self.reserve_per_call_usd
        return max(0.0, self.max_spend_usd - others)

    def settle(self, entry: int, cost_usd: float | None, input_tokens: int | None = None,
               output_tokens: int | None = None) -> float:
        cost = cost_usd if cost_usd is not None else self.reserve_per_call_usd
        self._conn.execute("UPDATE llm_spend SET status = 'settled', cost_usd = %s, input_tokens = %s, "
                           "output_tokens = %s WHERE id = %s", (cost, input_tokens, output_tokens, entry))
        return cost

    def summary(self) -> dict[str, float | int]:
        """Settled calls so far: how many, tokens in and out, dollars. For the doctor and the reports."""
        row = self._conn.execute(
            "SELECT count(*), coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0), "
            "coalesce(sum(cost_usd), 0) FROM llm_spend WHERE status = 'settled'").fetchone()
        return {"calls": int(row[0]), "tokens_in": int(row[1]), "tokens_out": int(row[2]),
                "usd": float(row[3])}

    def fail(self, entry: int) -> None:
        self._conn.execute("UPDATE llm_spend SET status = 'failed', cost_usd = 0 WHERE id = %s",
                           (entry,))
