"""Query and click logging plus the dashboard aggregates (top queries, zero-result queries, CTR, latency)."""

from __future__ import annotations

import time

from .storage import Store


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolated percentile (same definition as numpy's default), p in [0, 100]."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    rank = (len(s) - 1) * p / 100.0
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    return float(s[lo] + (s[hi] - s[lo]) * (rank - lo))


class Analytics:
    def __init__(self, store: Store):
        self.store = store

    def log_query(self, q: str, norm: str, results: int, latency_ms: float, cache_hit: bool, page: int = 1,
                  source: str = "user", ts: float | None = None) -> int:
        with self.store.lock:
            cur = self.store.conn.execute(
                "INSERT INTO queries(q, norm, results, latency_ms, cache_hit, page, source, ts) VALUES (?,?,?,?,?,?,?,?)",
                (q, norm, results, latency_ms, int(cache_hit), page, source, ts or time.time()),
            )
            return int(cur.lastrowid)

    def log_click(self, query_id: int, doc_id: int, rank: int, ts: float | None = None) -> int:
        with self.store.lock:
            if not self.store.conn.execute("SELECT 1 FROM queries WHERE id=?", (query_id,)).fetchone():
                raise LookupError(f"unknown query_id {query_id}")
            cur = self.store.conn.execute("INSERT INTO clicks(query_id, doc_id, rank, ts) VALUES (?,?,?,?)",
                                          (query_id, doc_id, rank, ts or time.time()))
            return int(cur.lastrowid)

    def query_count(self) -> int:
        return int(self.store.scalar("SELECT COUNT(*) FROM queries") or 0)

    def popular_queries(self, limit: int = 5000) -> list[tuple[str, int]]:
        """Queries that returned results, with how often they were searched (autocomplete weights)."""
        rows = self.store.query(
            "SELECT norm, COUNT(*) AS n FROM queries WHERE results > 0 AND page = 1 GROUP BY norm ORDER BY n DESC LIMIT ?",
            (limit,),
        )
        return [(r["norm"], r["n"]) for r in rows]

    def summary(self, days: int = 7, top: int = 10) -> dict:
        since = time.time() - days * 86400
        q = self.store.query
        # only first-page searches count as "a search" (page 2+ are the same search, paginated)
        totals = q(
            """SELECT COUNT(*) AS n, COUNT(DISTINCT norm) AS uniq,
                      SUM(results = 0) AS zero, SUM(cache_hit) AS hits,
                      SUM(source = 'demo') AS demo
               FROM queries WHERE ts >= ? AND page = 1""",
            (since,),
        )[0]
        n = totals["n"] or 0
        with_results = q("SELECT COUNT(*) FROM queries WHERE ts >= ? AND page = 1 AND results > 0", (since,))[0][0]
        clicked = q(
            """SELECT COUNT(DISTINCT c.query_id) FROM clicks c JOIN queries qu ON qu.id = c.query_id
               WHERE qu.ts >= ?""",
            (since,),
        )[0][0]
        clicks_total, mean_rank = q(
            "SELECT COUNT(*), AVG(c.rank) FROM clicks c JOIN queries qu ON qu.id = c.query_id WHERE qu.ts >= ?",
            (since,),
        )[0]
        latencies = [r[0] for r in q("SELECT latency_ms FROM queries WHERE ts >= ?", (since,))]
        uncached = [r[0] for r in q("SELECT latency_ms FROM queries WHERE ts >= ? AND cache_hit = 0", (since,))]

        top_rows = q(
            """SELECT qu.norm, COUNT(*) AS n, AVG(qu.results) AS avg_results,
                      SUM(EXISTS(SELECT 1 FROM clicks c WHERE c.query_id = qu.id)) AS clicked
               FROM queries qu
               WHERE qu.ts >= ? AND qu.page = 1 GROUP BY qu.norm ORDER BY n DESC, qu.norm LIMIT ?""",
            (since, top),
        )
        zero_rows = q(
            """SELECT norm, COUNT(*) AS n, MAX(ts) AS last_ts FROM queries
               WHERE ts >= ? AND page = 1 AND results = 0 GROUP BY norm ORDER BY n DESC, norm LIMIT ?""",
            (since, top),
        )
        daily = q(
            """SELECT date(ts, 'unixepoch') AS day, COUNT(*) AS n, SUM(results = 0) AS zero
               FROM queries WHERE ts >= ? AND page = 1 GROUP BY day ORDER BY day""",
            (since,),
        )
        buckets = [1, 2, 5, 10, 20, 50, 100]
        hist = [0] * (len(buckets) + 1)
        for v in latencies:
            hist[next((i for i, edge in enumerate(buckets) if v <= edge), len(buckets))] += 1
        labels = [f"≤{b} ms" for b in buckets] + [f">{buckets[-1]} ms"]

        return {
            "window_days": days,
            "searches": n,
            "unique_queries": totals["uniq"] or 0,
            "demo_searches": totals["demo"] or 0,
            "zero_result_rate": round((totals["zero"] or 0) / n, 4) if n else 0.0,
            "ctr": round(clicked / with_results, 4) if with_results else 0.0,
            "clicks": clicks_total or 0,
            "mean_click_rank": round(mean_rank, 2) if mean_rank else None,
            "cache_hit_rate_logged": round((totals["hits"] or 0) / n, 4) if n else 0.0,
            "latency_ms": {
                "p50": round(percentile(latencies, 50), 3),
                "p95": round(percentile(latencies, 95), 3),
                "p99": round(percentile(latencies, 99), 3),
                "p50_uncached": round(percentile(uncached, 50), 3),
                "p95_uncached": round(percentile(uncached, 95), 3),
            },
            "latency_histogram": {"labels": labels, "counts": hist},
            "top_queries": [
                {"query": r["norm"], "count": r["n"], "avg_results": round(r["avg_results"] or 0, 1),
                 "ctr": round(r["clicked"] / r["n"], 3) if r["n"] else 0.0}
                for r in top_rows
            ],
            "zero_result_queries": [{"query": r["norm"], "count": r["n"], "last_ts": r["last_ts"]} for r in zero_rows],
            "daily": [{"day": r["day"], "searches": r["n"], "zero": r["zero"]} for r in daily],
        }
