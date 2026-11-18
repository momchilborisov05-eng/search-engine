"""SQLite persistence: crawled pages, the inverted index, and the query/click logs.

One connection guarded by an RLock: SQLite serialises writers anyway, and the hot read path (search) runs against
the in-memory index, not this database.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    id          INTEGER PRIMARY KEY,
    url         TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    html_bytes  INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'seed',
    fetched_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS terms (
    term TEXT PRIMARY KEY,
    df   INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS postings (
    term      TEXT NOT NULL,
    doc_id    INTEGER NOT NULL,
    tf        INTEGER NOT NULL,
    positions TEXT NOT NULL,          -- comma-separated term positions, for phrase queries
    PRIMARY KEY (term, doc_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS doc_stats (
    doc_id      INTEGER PRIMARY KEY,
    length      INTEGER NOT NULL,     -- number of index terms (title + body)
    title_terms TEXT NOT NULL         -- space-separated distinct title terms, for the title boost
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queries (
    id         INTEGER PRIMARY KEY,
    q          TEXT NOT NULL,
    norm       TEXT NOT NULL,
    results    INTEGER NOT NULL,
    latency_ms REAL NOT NULL,
    cache_hit  INTEGER NOT NULL,
    page       INTEGER NOT NULL DEFAULT 1,
    source     TEXT NOT NULL DEFAULT 'user',
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queries_norm ON queries(norm);
CREATE INDEX IF NOT EXISTS idx_queries_ts ON queries(ts);
CREATE TABLE IF NOT EXISTS clicks (
    id       INTEGER PRIMARY KEY,
    query_id INTEGER NOT NULL REFERENCES queries(id),
    doc_id   INTEGER NOT NULL,
    rank     INTEGER NOT NULL,
    ts       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clicks_query ON clicks(query_id);
"""


class Store:
    def __init__(self, path: str = "sift.db"):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.lock = threading.RLock()
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def scalar(self, sql: str, params: Iterable = ()):
        rows = self.query(sql, params)
        return rows[0][0] if rows else None

    # -- pages ---------------------------------------------------------------------------------------------------
    def upsert_page(self, url: str, title: str, body: str, html_bytes: int = 0, source: str = "crawl",
                    fetched_at: float | None = None) -> int:
        with self.lock:
            self.conn.execute(
                """INSERT INTO pages(url, title, body, html_bytes, source, fetched_at) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(url) DO UPDATE SET title=excluded.title, body=excluded.body,
                   html_bytes=excluded.html_bytes, source=excluded.source, fetched_at=excluded.fetched_at""",
                (url, title, body, html_bytes, source, fetched_at or time.time()),
            )
            return int(self.conn.execute("SELECT id FROM pages WHERE url=?", (url,)).fetchone()[0])

    def insert_pages(self, rows: Iterable[tuple[str, str, str, int, str]]) -> int:
        now = time.time()
        with self.transaction() as c:
            cur = c.executemany(
                "INSERT OR IGNORE INTO pages(url, title, body, html_bytes, source, fetched_at) VALUES (?,?,?,?,?,?)",
                [(*r, now) for r in rows],
            )
            return cur.rowcount

    def page_count(self) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM pages") or 0)

    def get_page(self, doc_id: int) -> sqlite3.Row | None:
        rows = self.query("SELECT id, url, title, body, source, fetched_at FROM pages WHERE id=?", (doc_id,))
        return rows[0] if rows else None

    def get_pages(self, ids: list[int]) -> dict[int, sqlite3.Row]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        return {r["id"]: r for r in self.query(f"SELECT id, url, title, body FROM pages WHERE id IN ({marks})", ids)}

    def iter_pages(self) -> list[sqlite3.Row]:
        return self.query("SELECT id, url, title, body FROM pages ORDER BY id")

    # -- meta ----------------------------------------------------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        v = self.scalar("SELECT value FROM meta WHERE key=?", (key,))
        return default if v is None else v

    def set_meta(self, key: str, value) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (key, str(value)))
