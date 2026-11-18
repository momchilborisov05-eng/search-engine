"""SearchEngine: the one object the web layer, the CLI, the benchmark and the tests talk to."""

from __future__ import annotations

import gzip
import json
import logging
import random
import threading
import time
from collections import Counter
from pathlib import Path

from .analytics import Analytics
from .cache import LRUCache
from .config import Settings
from .index import BuildReport, InvertedIndex, highlight_terms, make_snippet, parse_query
from .storage import Store
from .text import STOPWORDS, normalize_query, strip_html, tokenize
from .trie import Trie

log = logging.getLogger(__name__)

SEED_PATH = Path(__file__).parent / "data" / "seed_corpus.jsonl.gz"
MAX_QUERY_LEN = 200
MAX_PAGE = 100
TITLE_SUGGEST_WEIGHT = 0.5      # a title is worth half a real search in autocomplete ranking
TERM_SUGGEST_WEIGHT = 0.05      # per document the term appears in, capped

# Realistic traffic for the demo dashboard: (query, relative popularity). A few are deliberately unanswerable.
DEMO_QUERIES = [
    ("json dumps", 30), ("read csv file", 22), ("sqlite3 connect", 20), ("regular expression match", 18),
    ("datetime timezone", 16), ("thread pool", 14), ("subprocess run", 14), ("pathlib path", 13),
    ("defaultdict", 12), ("sort list", 11), ("random choice", 10), ("http request", 10), ("parse url", 9),
    ("dataclass", 9), ("logging handler", 8), ("temporary file", 8), ("zip archive", 7), ("hash password", 7),
    ("asyncio event loop", 7), ("heap queue", 6), ("unit test assert", 6), ("decimal precision", 5),
    ("\"event loop\"", 5), ("copy deep", 5), ("uuid4", 4), ("enum", 4), ("string template", 4), ("socket server", 4),
    ("base64 encode", 4), ("shlex split", 3), ("xml parse", 3), ("struct pack", 3), ("pickle load", 3),
    ("timeit", 3), ("weak reference", 2), ("argparse subcommand", 2), ("textwrap dedent", 2),
    ("numpy array", 4), ("pandas dataframe", 3), ("kubernetes deploy", 2), ("react hooks", 2), ("jsno dumps", 2),
]
CLICK_PROBS = [0.42, 0.18, 0.09, 0.05, 0.03, 0.02, 0.01, 0.01, 0.01, 0.01]


def load_seed_pages(path: Path = SEED_PATH) -> list[tuple[str, str, str, int, str]]:
    rows = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            item = json.loads(line)
            page = strip_html(item["html"])
            rows.append((item["url"], page.title, page.text, len(item["html"].encode()), "seed"))
    return rows


class SearchEngine:
    def __init__(self, settings: Settings | None = None, store: Store | None = None):
        self.settings = settings or Settings()
        self.store = store or Store(self.settings.db_path)
        self.analytics = Analytics(self.store)
        self.cache = LRUCache(self.settings.cache_size, ttl=self.settings.cache_ttl)
        self.trie = Trie()
        self.index = InvertedIndex()
        self.last_build: BuildReport | None = None
        self._reindex_lock = threading.Lock()

    # lifecycle -----------------------------------------------------------------------------------------------
    def startup(self) -> SearchEngine:
        """Seed if empty, load (or build) the index, rebuild autocomplete, replay demo traffic once if enabled."""
        if self.store.page_count() == 0 and self.settings.seed:
            n = self.store.insert_pages(load_seed_pages())
            log.info("loaded %d seed pages", n)
        built_for = self.store.get_meta("index_pages")
        if built_for != str(self.store.page_count()) or not self.store.scalar("SELECT COUNT(*) FROM doc_stats"):
            self.reindex()
        else:
            self.index = InvertedIndex.load(self.store)
            self.rebuild_suggestions()
        if self.settings.demo_traffic and self.store.get_meta("demo_traffic") is None and self.index.n_docs:
            self.simulate_traffic()
        return self

    def close(self) -> None:
        self.store.close()

    def reindex(self) -> BuildReport:
        with self._reindex_lock:
            index, report = InvertedIndex.build(self.store)
            self.index = index                 # atomic swap: readers see the old or the new index, never half
            self.store.set_meta("index_pages", self.store.page_count())
            self.cache.clear()                 # cached results describe the old index
            self.last_build = report
            self.rebuild_suggestions()
            log.info("indexed %d docs, %d terms in %.3fs", report.docs, report.terms, report.seconds)
            return report

    def rebuild_suggestions(self) -> None:
        """Autocomplete = page titles + frequent surface words (cold start) + every successful past query, which
        outweighs both as soon as people search."""
        trie = Trie()
        word_df: Counter[str] = Counter()
        for row in self.store.query("SELECT title, body FROM pages"):
            title = row["title"].split(" - ")[0].split(" (")[0]
            if 2 < len(title) <= 60:
                trie.add(title, TITLE_SUGGEST_WEIGHT, kind="title")
            word_df.update({w for w in tokenize(row["body"]) if len(w) > 3 and not w.isdigit() and w not in STOPWORDS})
        for word, df in word_df.items():
            if df >= 3:
                trie.add(word, min(df, 20) * TERM_SUGGEST_WEIGHT, kind="term")
        for norm, count in self.analytics.popular_queries():
            if self._suggestable(parse_query(norm).terms):
                trie.add(norm, float(count), kind="query")
        self.trie = trie

    def _suggestable(self, terms: list[str]) -> bool:
        return bool(terms) and all(self.index.df(t) for t in terms)

    # search --------------------------------------------------------------------------------------------------
    def rank(self, q: str) -> list[tuple[int, float]]:
        s = self.settings
        return self.index.score(parse_query(q), k1=s.k1, b=s.b, title_boost=s.title_boost)

    def search(self, q: str, page: int = 1, size: int | None = None, *, log_query: bool = True,
               source: str = "user", ts: float | None = None) -> dict:
        q = (q or "").strip()
        if not q:
            raise ValueError("query must not be empty")
        if len(q) > MAX_QUERY_LEN:
            raise ValueError(f"query must be at most {MAX_QUERY_LEN} characters")
        size = size or self.settings.page_size
        if not 1 <= size <= 50:
            raise ValueError("size must be between 1 and 50")
        if not 1 <= page <= MAX_PAGE:
            raise ValueError(f"page must be between 1 and {MAX_PAGE}")

        t0 = time.perf_counter()
        norm = normalize_query(q)
        key = (norm, page, size)
        cached = self.cache.get(key)
        if cached is not None:
            payload, cache_hit = cached, True
        else:
            payload, cache_hit = self._search_uncached(q, page, size), False
            self.cache.put(key, payload)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        query_id = None
        if log_query:
            query_id = self.analytics.log_query(q, norm, payload["total"], latency_ms, cache_hit, page, source, ts)
            # only queries whose every term exists in the index feed autocomplete, so typos never get suggested
            terms = payload["parsed"]["terms"]
            if payload["total"] > 0 and page == 1 and self._suggestable(terms):
                self.trie.add(norm, 1.0, kind="query")
        return {**payload, "query": q, "page": page, "size": size, "took_ms": round(latency_ms, 3),
                "cache_hit": cache_hit, "query_id": query_id}

    def _search_uncached(self, q: str, page: int, size: int) -> dict:
        pq = parse_query(q)
        s = self.settings
        ranked = self.index.score(pq, k1=s.k1, b=s.b, title_boost=s.title_boost)
        total = len(ranked)
        start = (page - 1) * size
        window = ranked[start : start + size]
        pages = self.store.get_pages([d for d, _ in window])
        terms = set(pq.terms)
        results = []
        for i, (doc_id, score) in enumerate(window):
            p = pages.get(doc_id)
            if p is None:
                continue
            results.append({
                "rank": start + i + 1,
                "doc_id": doc_id,
                "url": p["url"],
                "title": p["title"],
                "title_html": highlight_terms(p["title"], terms),
                "snippet": make_snippet(p["body"], terms),
                "score": round(score, 4),
            })
        return {
            "total": total,
            "pages": (total + size - 1) // size,
            "results": results,
            "parsed": {"terms": pq.terms, "phrases": [" ".join(p) for p in pq.phrases], "excluded": pq.excluded},
        }

    def suggest(self, prefix: str, k: int = 8) -> list[dict]:
        prefix = (prefix or "").strip()[:MAX_QUERY_LEN]
        return self.trie.suggest(prefix, k) if prefix else []

    def click(self, query_id: int, doc_id: int, rank: int) -> int:
        if self.store.get_page(doc_id) is None:
            raise LookupError(f"unknown doc_id {doc_id}")
        return self.analytics.log_click(query_id, doc_id, rank)

    def stats(self) -> dict:
        idx = self.index
        b = self.last_build
        return {
            "documents": idx.n_docs,
            "terms": len(idx.postings),
            "postings": sum(len(d) for d in idx.postings.values()),
            "avg_doc_length": round(idx.avgdl, 1),
            "pages_by_source": {r[0]: r[1] for r in self.store.query("SELECT source, COUNT(*) FROM pages GROUP BY source")},
            "last_build": None if b is None else {"docs": b.docs, "terms": b.terms, "postings": b.postings,
                                                  "seconds": round(b.seconds, 4), "docs_per_sec": round(b.docs_per_sec)},
            "cache": self.cache.stats(),
            "suggestions": len(self.trie),
            "bm25": {"k1": self.settings.k1, "b": self.settings.b, "title_boost": self.settings.title_boost},
        }

    # demo ----------------------------------------------------------------------------------------------------
    def simulate_traffic(self, n: int = 400, days: int = 7, seed: int = 7) -> dict:
        """Replay `n` searches through the real search path (real latency, real cache) spread over the last `days`
        days, with clicks drawn from a position-biased model. Rows are tagged source='demo' and the dashboard
        says so; real searches are tagged 'user'."""
        rng = random.Random(seed)
        queries, weights = zip(*DEMO_QUERIES, strict=True)
        now = time.time()
        clicks = 0
        for i in range(n):
            q = rng.choices(queries, weights=weights)[0]
            ts = now - days * 86400 * (1 - i / n) + rng.uniform(0, 3600)
            res = self.search(q, source="demo", ts=min(ts, now))
            for r in res["results"]:
                if rng.random() < CLICK_PROBS[min(r["rank"], len(CLICK_PROBS)) - 1]:
                    self.analytics.log_click(res["query_id"], r["doc_id"], r["rank"], ts=min(ts, now) + 5)
                    clicks += 1
                    break
        self.store.set_meta("demo_traffic", n)
        return {"searches": n, "clicks": clicks}
