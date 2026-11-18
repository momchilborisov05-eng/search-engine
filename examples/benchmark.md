Measured 2026-11-18 on Python 3.12.3, Windows-11.

| Metric | Result |
|---|---|
| Corpus | 531 docs, 0.33 MB text, 3,238 terms, 22,188 postings |
| Index build (best of 5) | 75 ms (7,069 docs/s) |
| Cold startup (seed + build + autocomplete) | 187 ms |
| Search, uncached p50 / p95 | 2.39 / 4.868 ms |
| BM25 ranking only p50 / p95 | 0.069 / 0.432 ms |
| Search, cached p50 / p95 | 0.007 / 0.01 ms |
| LRU cache hit rate (Zipf 1.1, 2000 queries, capacity 256) | 76.4% (420 distinct, 666 q/s) |
| Autocomplete p50 / p95 | 11.6 / 15.2 µs over 1,828 entries |
