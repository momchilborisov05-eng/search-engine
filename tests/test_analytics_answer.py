import unittest
from types import SimpleNamespace

from sift.analytics import Analytics, percentile
from sift.answer import AnswerService
from sift.config import Settings
from sift.engine import SearchEngine
from sift.storage import Store


class PercentileTests(unittest.TestCase):
    def test_linear_interpolation(self):
        data = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        self.assertEqual(percentile(data, 50), 5.5)
        self.assertAlmostEqual(percentile(data, 95), 9.55)
        self.assertEqual(percentile([7], 95), 7)
        self.assertEqual(percentile([], 50), 0.0)


class AnalyticsTests(unittest.TestCase):
    def test_ctr_zero_results_and_top_queries(self):
        a = Analytics(Store(":memory:"))
        q1 = a.log_query("JSON", "json", 12, 1.0, False)
        a.log_query("json", "json", 12, 0.1, True)
        a.log_query("nothing here", "nothing here", 0, 2.0, False)
        a.log_query("json", "json", 12, 1.5, False, page=2)   # pagination is not a new search
        a.log_click(q1, 3, 1)
        a.log_click(q1, 4, 2)                                    # 2 clicks, still 1 clicked search
        s = a.summary()
        self.assertEqual(s["searches"], 3)
        self.assertAlmostEqual(s["ctr"], 0.5)                   # 1 clicked / 2 searches with results
        self.assertAlmostEqual(s["zero_result_rate"], round(1 / 3, 4))
        self.assertEqual(s["top_queries"][0]["query"], "json")
        self.assertEqual(s["top_queries"][0]["count"], 2)
        self.assertEqual(s["zero_result_queries"][0]["query"], "nothing here")
        self.assertEqual(s["clicks"], 2)
        with self.assertRaises(LookupError):
            a.log_click(9999, 1, 1)


def _engine():
    return SearchEngine(Settings(db_path=":memory:", demo_traffic=False), Store(":memory:")).startup()


class FakeClaude:
    def __init__(self, text="Use json.loads to parse a string [1]. Bogus cite [9].", stop_reason="end_turn"):
        self.calls = []
        self.text, self.stop_reason = text, stop_reason
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=self.text)],
                               usage=SimpleNamespace(input_tokens=900, output_tokens=40))


class AnswerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = _engine()

    def test_extractive_answer_without_key_cites_sources(self):
        svc = AnswerService(self.engine, Settings(db_path=":memory:"))
        self.assertIsNone(svc.claude)
        out = svc.answer("parse json")
        self.assertEqual(out["mode"], "extractive")
        self.assertRegex(out["answer"], r"\[\d\]")
        self.assertTrue(any(c["used"] for c in out["citations"]))

    def test_claude_request_shape_and_citation_cleanup(self):
        fake = FakeClaude()
        out = AnswerService(self.engine, Settings(db_path=":memory:"), client=fake).answer("parse json", k=3)
        kw = fake.calls[0]
        self.assertEqual(kw["model"], "claude-sonnet-5-5")
        self.assertEqual(kw["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01", kw["betas"])
        self.assertEqual(kw["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(out["mode"], "claude")
        self.assertNotIn("[9]", out["answer"])                # citation to a non-existent source removed
        self.assertTrue(out["citations"][0]["used"])
        self.assertEqual(len(out["citations"]), 3)

    def test_refusal_falls_back_to_extractive(self):
        out = AnswerService(self.engine, Settings(db_path=":memory:"),
                            client=FakeClaude(stop_reason="refusal")).answer("parse json")
        self.assertEqual(out["mode"], "extractive")
        self.assertIn("unavailable", out["note"])


if __name__ == "__main__":
    unittest.main()
