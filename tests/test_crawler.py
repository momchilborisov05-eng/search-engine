import unittest

from sift.crawler import Crawler, FetchResult, normalize_url
from sift.storage import Store

HTML = "text/html; charset=utf-8"


def page(title, body, *links):
    a = "".join(f'<a href="{href}">link</a>' for href in links)
    return f"<html><head><title>{title}</title></head><body><p>{body}</p>{a}</body></html>"


class FakeSite:
    """A mocked web: url -> FetchResult. Records every request."""

    def __init__(self, pages, robots=None, robots_status=200):
        self.pages = pages
        self.robots = robots
        self.robots_status = robots_status
        self.requests = []

    def __call__(self, url):
        self.requests.append(url)
        if url.endswith("/robots.txt"):
            if self.robots is None:
                return FetchResult(url, 404)
            return FetchResult(url, self.robots_status, "text/plain", self.robots)
        if url in self.pages:
            ctype, text = self.pages[url]
            return FetchResult(url, 200, ctype, text)
        return FetchResult(url, 404)


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def make_crawler(site, **kw):
    clk = FakeClock()
    store = Store(":memory:")
    kw.setdefault("delay", 0)
    return Crawler(store, fetch=site, clock=clk.clock, sleep=clk.sleep, **kw), store, clk


SITE = {
    "https://ex.com/": (HTML, page("Home", "welcome home", "/a", "/private/x", "https://other.org/z", "/img.png")),
    "https://ex.com/a": (HTML, page("A", "page a text", "/b", "/#frag", "/a#top")),
    "https://ex.com/b": (HTML, page("B", "page b text", "/data.txt")),
    "https://ex.com/data.txt": ("text/plain", "not html"),
    "https://ex.com/private/x": (HTML, page("Secret", "hidden")),
}


class CrawlerTests(unittest.TestCase):
    def test_respects_robots_disallow(self):
        site = FakeSite(SITE, robots="User-agent: *\nDisallow: /private/\n")
        crawler, store, _ = make_crawler(site)
        report = crawler.crawl(["https://ex.com/"])
        self.assertIn("https://ex.com/private/x", report.blocked_by_robots)
        self.assertNotIn("https://ex.com/private/x", site.requests)
        self.assertEqual(store.scalar("SELECT COUNT(*) FROM pages WHERE url LIKE '%private%'"), 0)

    def test_stays_on_domain_and_skips_assets_and_non_html(self):
        site = FakeSite(SITE)
        crawler, store, _ = make_crawler(site)
        report = crawler.crawl(["https://ex.com/"])
        self.assertFalse(any("other.org" in u for u in site.requests))
        self.assertFalse(any(u.endswith(".png") for u in site.requests))
        self.assertEqual(report.skipped_offsite, 1)
        self.assertEqual(report.skipped_non_html, 1)
        self.assertEqual(report.stored, store.page_count())
        self.assertEqual(store.page_count(), 4)   # home, a, b, private (no robots.txt -> allowed)

    def test_fragments_are_deduplicated(self):
        site = FakeSite(SITE)
        crawler, _, _ = make_crawler(site)
        crawler.crawl(["https://ex.com/"])
        pages = [u for u in site.requests if not u.endswith("robots.txt")]
        self.assertEqual(len(pages), len(set(pages)))

    def test_max_pages_limit(self):
        site = FakeSite(SITE)
        crawler, store, _ = make_crawler(site, max_pages=2)
        report = crawler.crawl(["https://ex.com/"])
        self.assertEqual(report.fetched, 2)
        self.assertEqual(store.page_count(), 2)

    def test_rate_limit_and_crawl_delay(self):
        site = FakeSite(SITE, robots="User-agent: *\nCrawl-delay: 2\n")
        crawler, _, clk = make_crawler(site, delay=0.5, max_pages=3)
        crawler.crawl(["https://ex.com/"])
        # robots + 3 pages = 4 requests -> 3 waits of max(delay, crawl-delay) = 2s each
        self.assertEqual(clk.sleeps, [2.0, 2.0, 2.0])

    def test_robots_server_error_means_disallow_all(self):
        site = FakeSite(SITE, robots="", robots_status=503)
        crawler, store, _ = make_crawler(site)
        report = crawler.crawl(["https://ex.com/"])
        self.assertEqual(store.page_count(), 0)
        self.assertEqual(report.blocked_by_robots, ["https://ex.com/"])

    def test_invalid_seeds_rejected(self):
        crawler, _, _ = make_crawler(FakeSite(SITE))
        with self.assertRaises(ValueError):
            crawler.crawl(["ftp://ex.com/", "not a url"])

    def test_normalize_url(self):
        self.assertEqual(normalize_url("/a#x", base="https://EX.com/b/"), "https://ex.com/a")
        self.assertEqual(normalize_url("https://ex.com"), "https://ex.com/")
        self.assertIsNone(normalize_url("mailto:me@ex.com"))
        self.assertIsNone(normalize_url("https://ex.com/file.pdf"))


if __name__ == "__main__":
    unittest.main()
