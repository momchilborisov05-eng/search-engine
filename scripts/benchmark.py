"""Measure the engine on the bundled corpus and write examples/benchmark.{json,md}.

    python scripts/benchmark.py            # default: 5 index builds, 2,000-query Zipf workload

Everything runs in-process against an in-memory SQLite database, so the numbers are the engine's own cost
(no HTTP, no disk). Run it on your machine; the README quotes one run.
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sift.analytics import percentile  # noqa: E402
from sift.config import Settings  # noqa: E402
from sift.engine import DEMO_QUERIES, SearchEngine, load_seed_pages  # noqa: E402
from sift.index import InvertedIndex  # noqa: E402
from sift.storage import Store  # noqa: E402
from sift.text import tokenize  # noqa: E402

QUERIES = [q for q, _ in DEMO_QUERIES]


def fresh_engine(cache_size: int = 256) -> SearchEngine:
    settings = Settings(db_path=":memory:", demo_traffic=False, cache_size=cache_size, cache_ttl=0)
    return SearchEngine(settings, Store(":memory:")).startup()


def bench_build(pages, runs: int) -> dict:
    """Full build: analyse every page + write terms/postings/doc_stats to SQLite."""
    times, report = [], None
    for _ in range(runs):
        store = Store(":memory:")
        store.insert_pages(pages)
        _, report = InvertedIndex.build(store)
        times.append(report.seconds)
        store.close()
    best = min(times)
    raw_bytes = sum(len(p[2].encode()) for p in pages)
    return {"docs": report.docs, "terms": report.terms, "postings": report.postings,
            "corpus_mb": round(raw_bytes / 1e6, 2),
            "build_seconds_best": round(best, 4), "build_seconds_median": round(statistics.median(times), 4),
            "docs_per_sec": round(report.docs / best), "runs": runs}


def bench_queries(engine: SearchEngine, rounds: int) -> dict:
    """Uncached latency: the full search path (parse, BM25 over postings, sort, fetch page rows, snippets)."""
    engine.cache = type(engine.cache)(1)          # capacity-1 cache + distinct keys => every call misses
    uncached = []
    for r in range(rounds):
        for i, q in enumerate(QUERIES):
            t0 = time.perf_counter()
            engine.search(q, page=1, size=10 - (r + i) % 2, log_query=False)
            uncached.append((time.perf_counter() - t0) * 1000)
    ranking = []
    for q in QUERIES * rounds:
        t0 = time.perf_counter()
        engine.rank(q)
        ranking.append((time.perf_counter() - t0) * 1000)
    eng = fresh_engine()
    for q in QUERIES:
        eng.search(q, log_query=False)
    cached = []
    for q in QUERIES * rounds:
        t0 = time.perf_counter()
        eng.search(q, log_query=False)
        cached.append((time.perf_counter() - t0) * 1000)

    def summ(xs):
        return {"p50_ms": round(percentile(xs, 50), 3), "p95_ms": round(percentile(xs, 95), 3),
                "mean_ms": round(statistics.fmean(xs), 3), "n": len(xs)}

    return {"uncached_search": summ(uncached), "bm25_rank_only": summ(ranking), "cached_search": summ(cached)}


def bench_cache(n_queries: int, capacity: int, seed: int = 1) -> dict:
    """Zipf(s=1.1) workload over a 1,000-query vocabulary: realistic head-heavy traffic."""
    rng = random.Random(seed)
    words = sorted({t for p in load_seed_pages()[:300] for t in tokenize(p[1]) if len(t) > 3})
    vocab = [" ".join(rng.sample(words, 2)) for _ in range(1000)]
    weights = [1 / (r ** 1.1) for r in range(1, len(vocab) + 1)]
    workload = rng.choices(vocab, weights=weights, k=n_queries)
    eng = fresh_engine(cache_size=capacity)
    t0 = time.perf_counter()
    for q in workload:
        eng.search(q, log_query=False)
    total = time.perf_counter() - t0
    st = eng.cache.stats()
    return {"queries": n_queries, "distinct": len(set(workload)), "capacity": capacity,
            "hit_rate": round(st["hit_rate"], 4), "evictions": st["evictions"],
            "throughput_qps": round(n_queries / total)}


def bench_suggest(engine: SearchEngine, rounds: int) -> dict:
    prefixes = sorted({q[:n] for q in QUERIES for n in (1, 2, 3, 5) if len(q) >= n})
    xs = []
    for _ in range(rounds):
        for p in prefixes:
            t0 = time.perf_counter()
            engine.suggest(p, 8)
            xs.append((time.perf_counter() - t0) * 1e6)
    return {"entries": len(engine.trie), "prefixes": len(prefixes),
            "p50_us": round(percentile(xs, 50), 1), "p95_us": round(percentile(xs, 95), 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--builds", type=int, default=5)
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--cache-queries", type=int, default=2000)
    ap.add_argument("--cache-capacity", type=int, default=256)
    ap.add_argument("--out", default=str(ROOT / "examples"))
    args = ap.parse_args()

    pages = load_seed_pages()
    t0 = time.perf_counter()
    engine = fresh_engine()
    startup = time.perf_counter() - t0
    result = {
        "machine": {"python": platform.python_version(), "platform": platform.platform(terse=True),
                    "processor": platform.processor() or platform.machine()},
        "cold_startup_seconds": round(startup, 3),
        "index_build": bench_build(pages, args.builds),
        "queries": bench_queries(engine, args.rounds),
        "cache": bench_cache(args.cache_queries, args.cache_capacity),
        "autocomplete": bench_suggest(engine, args.rounds),
        "measured_at": time.strftime("%Y-%m-%d"),
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "benchmark.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (out / "benchmark.md").write_text(to_markdown(result), encoding="utf-8")
    print(to_markdown(result))
    return 0


def to_markdown(r: dict) -> str:
    b, q, c, a = r["index_build"], r["queries"], r["cache"], r["autocomplete"]
    m = r["machine"]
    rows = [
        ("Corpus", f"{b['docs']} docs, {b['corpus_mb']} MB text, {b['terms']:,} terms, {b['postings']:,} postings"),
        (f"Index build (best of {b['runs']})", f"{b['build_seconds_best'] * 1000:.0f} ms ({b['docs_per_sec']:,} docs/s)"),
        ("Cold startup (seed + build + autocomplete)", f"{r['cold_startup_seconds'] * 1000:.0f} ms"),
        ("Search, uncached p50 / p95", f"{q['uncached_search']['p50_ms']} / {q['uncached_search']['p95_ms']} ms"),
        ("BM25 ranking only p50 / p95", f"{q['bm25_rank_only']['p50_ms']} / {q['bm25_rank_only']['p95_ms']} ms"),
        ("Search, cached p50 / p95", f"{q['cached_search']['p50_ms']} / {q['cached_search']['p95_ms']} ms"),
        (f"LRU cache hit rate (Zipf 1.1, {c['queries']} queries, capacity {c['capacity']})",
         f"{c['hit_rate'] * 100:.1f}% ({c['distinct']} distinct, {c['throughput_qps']:,} q/s)"),
        ("Autocomplete p50 / p95", f"{a['p50_us']} / {a['p95_us']} µs over {a['entries']:,} entries"),
    ]
    lines = [f"Measured {r['measured_at']} on Python {m['python']}, {m['platform']}.", "",
             "| Metric | Result |", "|---|---|"] + [f"| {k} | {v} |" for k, v in rows]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
