"""A polite, single-threaded breadth-first crawler.

Politeness rules: obeys robots.txt (including Crawl-delay), waits at least `delay` seconds between requests to
the same host, never leaves the seed domains, only follows http(s) links to HTML, caps pages and response size,
and identifies itself with a descriptive User-Agent. The fetch function, the clock and the sleep are injectable,
so tests run the whole crawler against an in-memory fake web.
"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request
import urllib.robotparser
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlsplit

from .storage import Store
from .text import strip_html

log = logging.getLogger(__name__)

USER_AGENT = "SiftBot/1.0 (portfolio search-engine crawler; respects robots.txt)"
SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar", ".mp4",
            ".mp3", ".css", ".js", ".json", ".xml", ".woff", ".woff2", ".exe", ".dmg")


@dataclass
class FetchResult:
    url: str
    status: int
    content_type: str = ""
    text: str = ""


Fetcher = Callable[[str], FetchResult]


def http_fetch(url: str, timeout: float = 10.0, max_bytes: int = 2_000_000) -> FetchResult:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - scheme checked by the crawler
            raw = resp.read(max_bytes + 1)[:max_bytes]
            charset = resp.headers.get_content_charset() or "utf-8"
            return FetchResult(resp.geturl(), resp.status, resp.headers.get("Content-Type", ""),
                               raw.decode(charset, errors="replace"))
    except urllib.error.HTTPError as e:
        return FetchResult(url, e.code)


@dataclass
class CrawlReport:
    fetched: int = 0
    stored: int = 0
    blocked_by_robots: list[str] = field(default_factory=list)
    skipped_offsite: int = 0
    skipped_non_html: int = 0
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0


def normalize_url(url: str, base: str | None = None) -> str | None:
    url = urljoin(base, url) if base else url
    url, _ = urldefrag(url)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    if parts.path.lower().endswith(SKIP_EXT):
        return None
    path = parts.path or "/"
    return f"{parts.scheme}://{parts.netloc.lower()}{path}" + (f"?{parts.query}" if parts.query else "")


class Crawler:
    def __init__(self, store: Store, fetch: Fetcher | None = None, *, delay: float = 1.0, max_pages: int = 50,
                 user_agent: str = USER_AGENT, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        self.store = store
        self.fetch = fetch or http_fetch
        self.delay = delay
        self.max_pages = max_pages
        self.user_agent = user_agent
        self.clock = clock
        self.sleep = sleep
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}

    # politeness ----------------------------------------------------------------------------------------------
    def _wait_turn(self, host: str) -> None:
        delay = self.delay
        rp = self._robots.get(host)
        if rp is not None:
            crawl_delay = rp.crawl_delay(self.user_agent)
            if crawl_delay:
                delay = max(delay, float(crawl_delay))
        last = self._last_hit.get(host)
        if last is not None:
            wait = delay - (self.clock() - last)
            if wait > 0:
                self.sleep(wait)
        self._last_hit[host] = self.clock()

    def _robots_for(self, scheme: str, host: str) -> urllib.robotparser.RobotFileParser | None:
        if host not in self._robots:
            robots_url = f"{scheme}://{host}/robots.txt"
            self._wait_turn(host)
            try:
                res = self.fetch(robots_url)
            except Exception as e:  # noqa: BLE001
                log.warning("robots.txt fetch failed for %s: %s", host, e)
                res = FetchResult(robots_url, 599)
            rp = urllib.robotparser.RobotFileParser(robots_url)
            if 200 <= res.status < 300:
                rp.parse(res.text.splitlines())
            elif 400 <= res.status < 500:
                rp.allow_all = True        # no robots.txt: everything allowed (RFC 9309)
            else:
                rp.disallow_all = True     # server error: assume complete disallow (RFC 9309)
            self._robots[host] = rp
        return self._robots[host]

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        rp = self._robots_for(parts.scheme, parts.netloc)
        return rp.can_fetch(self.user_agent, url) if rp else True

    # crawl ---------------------------------------------------------------------------------------------------
    def crawl(self, seeds: list[str]) -> CrawlReport:
        t0 = time.perf_counter()
        report = CrawlReport()
        start = [u for u in (normalize_url(s) for s in seeds) if u]
        if not start:
            raise ValueError("no valid http(s) seed URL")
        domains = {urlsplit(u).netloc for u in start}
        queue: deque[str] = deque(start)
        seen: set[str] = set(start)
        while queue and report.fetched < self.max_pages:
            url = queue.popleft()
            if not self.allowed(url):
                report.blocked_by_robots.append(url)
                continue
            self._wait_turn(urlsplit(url).netloc)
            try:
                res = self.fetch(url)
            except Exception as e:  # noqa: BLE001 - one bad page must not stop the crawl
                report.errors.append(f"{url}: {e}")
                continue
            report.fetched += 1
            if res.status != 200:
                report.errors.append(f"{url}: HTTP {res.status}")
                continue
            if "html" not in (res.content_type or "").lower():
                report.skipped_non_html += 1
                continue
            page = strip_html(res.text)
            if page.text.strip():
                self.store.upsert_page(url, page.title or url, page.text, len(res.text.encode()), source="crawl")
                report.stored += 1
            for href in page.links:
                link = normalize_url(href, base=res.url or url)
                if not link or link in seen:
                    continue
                if urlsplit(link).netloc not in domains:
                    report.skipped_offsite += 1
                    continue
                seen.add(link)
                queue.append(link)
        report.seconds = time.perf_counter() - t0
        return report
