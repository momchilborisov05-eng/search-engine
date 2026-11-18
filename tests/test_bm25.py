import math
import unittest

from sift.index import InvertedIndex, bm25_term, highlight_terms, idf, make_snippet, parse_query
from sift.text import analyze

# Three tiny documents, no titles, so document length == number of body terms.
DOCS = {1: ("", "apple banana"), 2: ("", "apple apple cherry"), 3: ("", "cherry plum")}


class Bm25MathTests(unittest.TestCase):
    def test_idf_matches_hand_calculation(self):
        # N=3, df=2: ln(1 + (3 - 2 + 0.5) / (2 + 0.5)) = ln(1.6)
        self.assertAlmostEqual(idf(2, 3), math.log(1.6), places=12)
        self.assertGreater(idf(1, 3), idf(2, 3))   # rarer term weighs more
        self.assertGreater(idf(3, 3), 0)           # Lucene-style IDF never goes negative

    def test_bm25_score_matches_hand_checked_example(self):
        idx = InvertedIndex.from_documents(DOCS)
        self.assertAlmostEqual(idx.avgdl, 7 / 3)
        scores = dict(idx.score(parse_query("apple")))
        # d1: tf=1, dl=2 -> 0.470004 * 2.2 / (1 + 1.2 * (0.25 + 0.75 * 2 / (7/3))) = 0.499176
        # d2: tf=2, dl=3 -> 0.470004 * 4.4 / (2 + 1.2 * (0.25 + 0.75 * 3 / (7/3))) = 0.598187
        self.assertAlmostEqual(scores[1], 0.499176, places=5)
        self.assertAlmostEqual(scores[2], 0.598187, places=5)
        self.assertNotIn(3, scores)
        self.assertAlmostEqual(bm25_term(1, 2, 3, 2, 7 / 3), scores[1], places=12)

    def test_multi_term_scores_add_and_rank(self):
        idx = InvertedIndex.from_documents(DOCS)
        ranked = idx.score(parse_query("apple cherry"))
        self.assertEqual(ranked[0][0], 2)          # only doc with both terms
        self.assertEqual({d for d, _ in ranked}, {1, 2, 3})

    def test_b_zero_disables_length_normalisation(self):
        self.assertAlmostEqual(bm25_term(1, 2, 3, 2, 7 / 3, b=0), bm25_term(1, 2, 3, 50, 7 / 3, b=0))


class QueryFeatureTests(unittest.TestCase):
    def setUp(self):
        self.idx = InvertedIndex.from_documents({
            1: ("Regular expressions", "the re module provides regular expression matching"),
            2: ("String methods", "an expression that is regular in shape but not about regex"),
            3: ("JSON", "encode and decode json documents with the json module"),
        })

    def test_parse_query_phrases_and_exclusions(self):
        pq = parse_query('"regular expression" -json module')
        self.assertEqual(pq.phrases, [analyze("regular expression")])
        self.assertEqual(pq.excluded, analyze("json"))
        self.assertIn(analyze("module")[0], pq.terms)

    def test_phrase_requires_adjacent_terms(self):
        ranked = self.idx.score(parse_query('"regular expression"'))
        self.assertEqual([d for d, _ in ranked], [1])

    def test_exclusion_removes_documents(self):
        ranked = self.idx.score(parse_query("module -json"))
        self.assertEqual([d for d, _ in ranked], [1])

    def test_title_boost_lifts_title_matches(self):
        plain = dict(self.idx.score(parse_query("regular")))
        boosted = dict(self.idx.score(parse_query("regular"), title_boost=1.0))
        self.assertGreater(boosted[1] - plain[1], 0)
        self.assertAlmostEqual(boosted[2], plain[2])

    def test_empty_and_stopword_only_queries_return_nothing(self):
        self.assertEqual(self.idx.score(parse_query("")), [])
        self.assertEqual(self.idx.score(parse_query("the and of")), [])


class SnippetTests(unittest.TestCase):
    def test_snippet_marks_terms_and_escapes_html(self):
        body = "Intro text. " * 30 + "Use <script>json.loads</script> to parse JSON strings safely. " + "Tail. " * 30
        html = make_snippet(body, set(analyze("parse json")))
        self.assertIn("<mark>json</mark>", html)
        self.assertIn("<mark>parse</mark>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertTrue(html.startswith("… "))

    def test_highlight_terms_in_title(self):
        self.assertEqual(highlight_terms("Parsing <JSON>", set(analyze("parse"))), "<mark>Parsing</mark> &lt;JSON&gt;")


if __name__ == "__main__":
    unittest.main()
