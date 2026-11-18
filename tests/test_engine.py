import os
import tempfile
import unittest

from sift.config import Settings
from sift.engine import SearchEngine
from sift.storage import Store


class EngineTests(unittest.TestCase):
    def test_index_persists_and_reloads_identically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.db")
            settings = Settings(db_path=path, demo_traffic=False)
            first = SearchEngine(settings).startup()
            ranked = first.rank("socket timeout")
            first.close()
            second = SearchEngine(settings).startup()       # loads postings from SQLite instead of rebuilding
            self.assertIsNone(second.last_build)
            self.assertEqual(second.rank("socket timeout"), ranked)
            second.close()

    def test_reindex_picks_up_new_pages_and_clears_cache(self):
        eng = SearchEngine(Settings(db_path=":memory:", demo_traffic=False), Store(":memory:")).startup()
        self.assertEqual(eng.search("zyzzyva", log_query=False)["total"], 0)
        eng.store.upsert_page("https://ex.com/z", "Zyzzyva notes", "The zyzzyva is a weevil.", source="crawl")
        eng.reindex()
        res = eng.search("zyzzyva", log_query=False)
        self.assertFalse(res["cache_hit"])
        self.assertEqual(res["results"][0]["url"], "https://ex.com/z")

    def test_demo_traffic_is_tagged_and_runs_once(self):
        eng = SearchEngine(Settings(db_path=":memory:"), Store(":memory:")).startup()
        s = eng.analytics.summary()
        self.assertEqual(s["demo_searches"], s["searches"])
        self.assertGreater(s["ctr"], 0)
        self.assertTrue(s["zero_result_queries"])
        before = eng.analytics.query_count()
        eng.startup()
        self.assertEqual(eng.analytics.query_count(), before)

    def test_typo_queries_do_not_enter_autocomplete(self):
        eng = SearchEngine(Settings(db_path=":memory:", demo_traffic=False), Store(":memory:")).startup()
        res = eng.search("jsno dumps")                     # still finds json.dumps via "dumps"
        self.assertGreater(res["total"], 0)
        eng.search("json dumps")
        texts = [s["text"] for s in eng.suggest("js")]
        self.assertIn("json dumps", texts)
        self.assertNotIn("jsno dumps", texts)


if __name__ == "__main__":
    unittest.main()
