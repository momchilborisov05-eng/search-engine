import unittest

from sift.config import Settings
from sift.engine import SearchEngine
from sift.storage import Store
from sift.web import create_app


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = Settings(db_path=":memory:", demo_traffic=False)
        cls.engine = SearchEngine(settings, Store(":memory:")).startup()
        cls.app = create_app(settings, engine=cls.engine)
        cls.client = cls.app.test_client()

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["status"], "ok")
        self.assertGreater(r.json["documents"], 500)
        self.assertEqual(r.json["ai_mode"], "extractive")

    def test_search_returns_ranked_highlighted_results(self):
        r = self.client.get("/api/search?q=parse+json")
        self.assertEqual(r.status_code, 200)
        d = r.json
        self.assertGreater(d["total"], 0)
        self.assertTrue(any("json" in x["url"] for x in d["results"][:3]))
        scores = [x["score"] for x in d["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertIn("<mark>", d["results"][0]["snippet"])
        self.assertIsInstance(d["query_id"], int)

    def test_pagination(self):
        p1 = self.client.get("/api/search?q=file&size=5").json
        p2 = self.client.get("/api/search?q=file&size=5&page=2").json
        self.assertEqual(len(p1["results"]), 5)
        self.assertEqual(p2["results"][0]["rank"], 6)
        self.assertFalse({x["doc_id"] for x in p1["results"]} & {x["doc_id"] for x in p2["results"]})
        self.assertEqual(p1["pages"], -(-p1["total"] // 5))

    def test_repeat_query_hits_cache(self):
        self.client.get("/api/search?q=thread+lock")
        r = self.client.get("/api/search?q=Thread%20%20Lock").json
        self.assertTrue(r["cache_hit"])

    def test_validation_errors_are_json_400(self):
        for url in ("/api/search", "/api/search?q=", "/api/search?q=x&page=0", "/api/search?q=x&page=abc",
                    "/api/search?q=x&size=500", "/api/search?q=" + "a" * 201):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 400, url)
            self.assertIn("error", r.json)

    def test_zero_result_query(self):
        d = self.client.get("/api/search?q=kubernetes+helm").json
        self.assertEqual(d["total"], 0)
        self.assertEqual(d["results"], [])

    def test_suggest_includes_popular_queries(self):
        for _ in range(3):
            self.client.get("/api/search?q=regular+expression+syntax")
        s = self.client.get("/api/suggest?q=regular+exp").json["suggestions"]
        self.assertEqual(s[0]["text"], "regular expression syntax")
        self.assertEqual(s[0]["kind"], "query")
        self.assertEqual(self.client.get("/api/suggest?q=").json["suggestions"], [])

    def test_click_logging_feeds_ctr(self):
        d = self.client.get("/api/search?q=decimal+rounding").json
        top = d["results"][0]
        r = self.client.post("/api/click", json={"query_id": d["query_id"], "doc_id": top["doc_id"], "rank": 1})
        self.assertEqual(r.status_code, 201)
        a = self.client.get("/api/analytics").json
        self.assertGreater(a["ctr"], 0)
        self.assertGreater(a["searches"], 0)
        self.assertIn("p95", a["latency_ms"])
        self.assertIn("hit_rate", a["cache"])

    def test_click_validation(self):
        self.assertEqual(self.client.post("/api/click", json={"query_id": 1}).status_code, 400)
        self.assertEqual(self.client.post("/api/click", data="nope", content_type="text/plain").status_code, 400)
        r = self.client.post("/api/click", json={"query_id": 10 ** 9, "doc_id": 1, "rank": 1})
        self.assertEqual(r.status_code, 404)

    def test_answer_endpoint_extractive(self):
        r = self.client.post("/api/answer", json={"q": "temporary directory"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["mode"], "extractive")
        self.assertTrue(r.json["citations"])
        self.assertEqual(self.client.post("/api/answer", json={"q": "x", "k": 99}).status_code, 400)

    def test_doc_and_404s(self):
        self.assertEqual(self.client.get("/api/doc/1").status_code, 200)
        r = self.client.get("/api/doc/999999")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json["error"], "document not found")
        self.assertEqual(self.client.get("/nope").status_code, 404)
        self.assertIn("error", self.client.get("/nope").json)

    def test_pages_served_with_csp(self):
        with self.client.get("/") as r:
            self.assertEqual(r.status_code, 200)
            self.assertIn("script-src 'self' https://cdnjs.cloudflare.com", r.headers["Content-Security-Policy"])
        for url in ("/analytics", "/static/app.js", "/static/style.css"):
            with self.client.get(url) as r:
                self.assertEqual(r.status_code, 200, url)


if __name__ == "__main__":
    unittest.main()
