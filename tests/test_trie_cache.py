import unittest

from sift.cache import LRUCache
from sift.trie import Trie


class TrieTests(unittest.TestCase):
    def test_prefix_suggestions_ranked_by_weight(self):
        t = Trie()
        t.add("json loads", 5)
        t.add("json dumps", 9)
        t.add("jsonschema", 1)
        t.add("pickle", 50)
        self.assertEqual([s["text"] for s in t.suggest("json")], ["json dumps", "json loads", "jsonschema"])
        self.assertEqual(t.suggest("xyz"), [])

    def test_repeated_adds_accumulate_popularity(self):
        t = Trie()
        t.add("asyncio", 1)
        t.add("argparse", 2)
        self.assertEqual(t.suggest("a")[0]["text"], "argparse")
        t.add("asyncio", 1)
        t.add("asyncio", 1)
        self.assertEqual(t.weight("asyncio"), 3)
        self.assertEqual(t.suggest("a")[0]["text"], "asyncio")

    def test_case_insensitive_and_limit(self):
        t = Trie(top_k=3)
        for i in range(10):
            t.add(f"Term{i}", i)
        self.assertIn("term9", t)
        self.assertEqual(len(t), 10)
        self.assertEqual([s["text"] for s in t.suggest("TERM", k=2)], ["term9", "term8"])
        self.assertEqual(len(t.suggest("term", k=6)), 6)   # beyond the per-node cache -> full walk

    def test_ties_break_alphabetically_and_kind_is_kept(self):
        t = Trie()
        t.add("betb", 1, kind="title")
        t.add("beta", 1)
        self.assertEqual([s["text"] for s in t.suggest("bet")], ["beta", "betb"])
        self.assertEqual(t.suggest("betb")[0]["kind"], "title")
        self.assertEqual(t.suggest(""), [])      # empty prefix suggests nothing

    def test_rejects_bad_input(self):
        t = Trie()
        with self.assertRaises(ValueError):
            t.add("   ")
        with self.assertRaises(ValueError):
            t.add("x", -1)


class CacheTests(unittest.TestCase):
    def test_lru_eviction_order(self):
        c = LRUCache(2)
        c.put("a", 1)
        c.put("b", 2)
        self.assertEqual(c.get("a"), 1)    # a is now most recently used
        c.put("c", 3)                      # evicts b
        self.assertNotIn("b", c)
        self.assertIn("a", c)
        self.assertEqual(c.evictions, 1)

    def test_hit_rate(self):
        c = LRUCache(4)
        c.put("k", "v")
        c.get("k")
        c.get("k")
        c.get("missing")
        self.assertEqual((c.hits, c.misses), (2, 1))
        self.assertAlmostEqual(c.hit_rate, 2 / 3)
        self.assertEqual(c.stats()["size"], 1)

    def test_ttl_expiry_with_fake_clock(self):
        now = [100.0]
        c = LRUCache(4, ttl=10, clock=lambda: now[0])
        c.put("k", "v")
        now[0] = 109.0
        self.assertEqual(c.get("k"), "v")
        now[0] = 111.0
        self.assertIsNone(c.get("k"))
        self.assertEqual(len(c), 0)

    def test_clear_and_validation(self):
        c = LRUCache(2)
        c.put("a", 1)
        c.clear()
        self.assertEqual(len(c), 0)
        with self.assertRaises(ValueError):
            LRUCache(0)


if __name__ == "__main__":
    unittest.main()
