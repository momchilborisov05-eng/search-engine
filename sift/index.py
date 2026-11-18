"""Inverted index (persisted in SQLite, served from memory) and BM25 ranking, implemented from scratch.

BM25 (Robertson/Sparck Jones, with the Lucene-style non-negative IDF):

    idf(t)        = ln(1 + (N - df + 0.5) / (df + 0.5))
    score(d, q)   = sum over t in q of  idf(t) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * |d| / avgdl))

On top of that a small title boost (title_boost * idf(t) for each query term in the title) and a phrase bonus.
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .storage import Store
from .text import analyze, normalize_term, tokenize


def idf(df: int, n_docs: int) -> float:
    return math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))


def bm25_term(tf: int, df: int, n_docs: int, dl: int, avgdl: float, k1: float = 1.2, b: float = 0.75) -> float:
    if tf <= 0 or n_docs <= 0:
        return 0.0
    norm = k1 * (1.0 - b + b * dl / avgdl) if avgdl else k1
    return idf(df, n_docs) * tf * (k1 + 1.0) / (tf + norm)


# -- query parsing ----------------------------------------------------------------------------------------------
_PHRASE_RE = re.compile(r'"([^"]*)"')


@dataclass
class ParsedQuery:
    terms: list[str] = field(default_factory=list)          # all positive terms (phrase terms included), unique
    phrases: list[list[str]] = field(default_factory=list)  # each a list of >= 2 consecutive terms
    excluded: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.terms


def parse_query(q: str) -> ParsedQuery:
    """`"exact phrase"` -> phrase (terms must be adjacent), `-word` -> excluded, everything else -> OR terms."""
    pq = ParsedQuery()
    seen: set[str] = set()

    def add(term: str) -> None:
        if term not in seen:
            seen.add(term)
            pq.terms.append(term)

    for raw in _PHRASE_RE.findall(q):
        terms = analyze(raw)
        if len(terms) >= 2:
            pq.phrases.append(terms)
        for t in terms:
            add(t)
    rest = _PHRASE_RE.sub(" ", q).replace('"', " ")
    for word in rest.split():
        if word.startswith("-") and len(word) > 1:
            for t in analyze(word[1:]):
                if t not in pq.excluded:
                    pq.excluded.append(t)
            continue
        for t in analyze(word):
            add(t)
    pq.terms = [t for t in pq.terms if t not in pq.excluded]
    return pq


# -- index ------------------------------------------------------------------------------------------------------
@dataclass
class BuildReport:
    docs: int
    terms: int
    postings: int
    seconds: float

    @property
    def docs_per_sec(self) -> float:
        return self.docs / self.seconds if self.seconds else 0.0


class InvertedIndex:
    """term -> {doc_id: (tf, positions-string)}; positions are parsed only when a phrase query needs them."""

    def __init__(self) -> None:
        self.postings: dict[str, dict[int, tuple[int, str]]] = {}
        self.doc_len: dict[int, int] = {}
        self.title_terms: dict[int, frozenset[str]] = {}
        self.n_docs = 0
        self.avgdl = 0.0

    # build ---------------------------------------------------------------------------------------------------
    @classmethod
    def build(cls, store: Store) -> tuple[InvertedIndex, BuildReport]:
        """Analyse every page, write terms/postings/doc_stats to SQLite in one transaction, return the index."""
        t0 = time.perf_counter()
        idx = cls()
        for row in store.iter_pages():
            title_terms = analyze(row["title"])
            terms = title_terms + analyze(row["body"])
            idx._add_doc(row["id"], terms, title_terms)
        idx._finish()
        posting_rows = [
            (term, doc_id, tf, pos) for term, docs in idx.postings.items() for doc_id, (tf, pos) in docs.items()
        ]
        with store.transaction() as c:
            c.execute("DELETE FROM postings")
            c.execute("DELETE FROM terms")
            c.execute("DELETE FROM doc_stats")
            c.executemany("INSERT INTO postings(term, doc_id, tf, positions) VALUES (?,?,?,?)", posting_rows)
            c.executemany("INSERT INTO terms(term, df) VALUES (?,?)", [(t, len(d)) for t, d in idx.postings.items()])
            c.executemany(
                "INSERT INTO doc_stats(doc_id, length, title_terms) VALUES (?,?,?)",
                [(d, n, " ".join(sorted(idx.title_terms[d]))) for d, n in idx.doc_len.items()],
            )
            for k, v in (("n_docs", idx.n_docs), ("avgdl", idx.avgdl), ("built_at", time.time())):
                c.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (k, str(v)))
        seconds = time.perf_counter() - t0
        return idx, BuildReport(idx.n_docs, len(idx.postings), len(posting_rows), seconds)

    def _add_doc(self, doc_id: int, terms: list[str], title_terms: list[str]) -> None:
        positions: dict[str, list[int]] = defaultdict(list)
        for i, t in enumerate(terms):
            positions[t].append(i)
        for t, pos in positions.items():
            self.postings.setdefault(t, {})[doc_id] = (len(pos), ",".join(map(str, pos)))
        self.doc_len[doc_id] = len(terms)
        self.title_terms[doc_id] = frozenset(title_terms)

    def _finish(self) -> None:
        self.n_docs = len(self.doc_len)
        self.avgdl = sum(self.doc_len.values()) / self.n_docs if self.n_docs else 0.0

    @classmethod
    def from_documents(cls, docs: dict[int, tuple[str, str]]) -> InvertedIndex:
        """In-memory build from {doc_id: (title, body)}; used by tests and the benchmark."""
        idx = cls()
        for doc_id, (title, body) in docs.items():
            tt = analyze(title)
            idx._add_doc(doc_id, tt + analyze(body), tt)
        idx._finish()
        return idx

    # load ----------------------------------------------------------------------------------------------------
    @classmethod
    def load(cls, store: Store) -> InvertedIndex:
        idx = cls()
        for row in store.query("SELECT doc_id, length, title_terms FROM doc_stats"):
            idx.doc_len[row[0]] = row[1]
            idx.title_terms[row[0]] = frozenset(row[2].split())
        for term, doc_id, tf, pos in store.query("SELECT term, doc_id, tf, positions FROM postings"):
            idx.postings.setdefault(term, {})[doc_id] = (tf, pos)
        idx._finish()
        return idx

    # query ---------------------------------------------------------------------------------------------------
    def df(self, term: str) -> int:
        return len(self.postings.get(term, ()))

    def positions(self, term: str, doc_id: int) -> list[int]:
        entry = self.postings.get(term, {}).get(doc_id)
        return [int(p) for p in entry[1].split(",")] if entry else []

    def has_phrase(self, doc_id: int, phrase: list[str]) -> bool:
        first = self.positions(phrase[0], doc_id)
        if not first:
            return False
        rest = [set(self.positions(t, doc_id)) for t in phrase[1:]]
        return any(all(p + i + 1 in s for i, s in enumerate(rest)) for p in first)

    def score(self, pq: ParsedQuery, k1: float = 1.2, b: float = 0.75, title_boost: float = 0.0,
              phrase_bonus: float = 1.0) -> list[tuple[int, float]]:
        """Rank documents for a parsed query. OR semantics over terms; phrases and exclusions are hard filters."""
        if pq.empty or not self.n_docs:
            return []
        scores: Counter[int] = Counter()
        for term in pq.terms:
            docs = self.postings.get(term)
            if not docs:
                continue
            df = len(docs)
            term_idf = idf(df, self.n_docs)
            for doc_id, (tf, _) in docs.items():
                s = bm25_term(tf, df, self.n_docs, self.doc_len[doc_id], self.avgdl, k1, b)
                if title_boost and term in self.title_terms[doc_id]:
                    s += title_boost * term_idf
                scores[doc_id] += s
        if pq.excluded:
            banned = set().union(*(self.postings.get(t, {}).keys() for t in pq.excluded))
            for d in banned:
                scores.pop(d, None)
        for phrase in pq.phrases:
            bonus = phrase_bonus * sum(idf(self.df(t), self.n_docs) for t in phrase)
            for d in list(scores):
                if self.has_phrase(d, phrase):
                    scores[d] += bonus
                else:
                    del scores[d]
        return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


# -- snippets ---------------------------------------------------------------------------------------------------
_WORD_SPAN = re.compile(r"[A-Za-z0-9]+")


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def make_snippet(body: str, query_terms: set[str], window: int = 30, max_chars: int = 260) -> str:
    """Best window of `window` words by (distinct query terms, total hits). Returns HTML-escaped text where only
    the matched words are wrapped in <mark>, so the frontend can insert it as HTML safely."""
    spans = [(m.start(), m.end(), normalize_term(m.group().lower())) for m in _WORD_SPAN.finditer(body)]
    if not spans:
        return ""
    hits = [1 if s[2] in query_terms else 0 for s in spans]
    best_start, best_key = 0, (-1, -1)
    if query_terms:
        for start in range(0, max(1, len(spans) - window + 1)):
            seg = spans[start : start + window]
            distinct = len({s[2] for s, h in zip(seg, hits[start : start + window], strict=False) if h})
            key = (distinct, sum(hits[start : start + window]))
            if key > best_key:
                best_start, best_key = start, key
        # start a couple of words before the first hit so the match has some context
        first_hit = next((i for i in range(best_start, min(len(spans), best_start + window)) if hits[i]), best_start)
        best_start = max(0, min(best_start, first_hit - 3))
    seg = spans[best_start : best_start + window]
    lo, hi = seg[0][0], seg[-1][1]
    if hi - lo > max_chars:
        fitting = [e for _, e, _ in seg if e - lo <= max_chars]
        hi = fitting[-1] if fitting else lo + max_chars
    out, cursor = [], lo
    for s, e, term in seg:
        if s >= hi:
            break
        out.append(_escape(body[cursor:s]))
        word = _escape(body[s:e])
        out.append(f"<mark>{word}</mark>" if term in query_terms else word)
        cursor = e
    out.append(_escape(body[cursor:hi]))
    text = " ".join("".join(out).split())
    return ("… " if lo > 0 else "") + text + (" …" if hi < len(body.rstrip()) else "")


def highlight_terms(text: str, query_terms: set[str]) -> str:
    """Escape `text` and <mark> the words whose analysed form is a query term (used for titles)."""
    out, cursor = [], 0
    for m in _WORD_SPAN.finditer(text):
        out.append(_escape(text[cursor : m.start()]))
        word = _escape(m.group())
        out.append(f"<mark>{word}</mark>" if normalize_term(m.group().lower()) in query_terms else word)
        cursor = m.end()
    out.append(_escape(text[cursor:]))
    return "".join(out)


__all__ = ["InvertedIndex", "ParsedQuery", "BuildReport", "parse_query", "bm25_term", "idf", "make_snippet",
           "highlight_terms", "tokenize"]
