import unittest

from sift.text import STOPWORDS, analyze, normalize_query, stem, strip_html, tokenize


class TokenizerTests(unittest.TestCase):
    def test_tokenize_lowercases_and_splits_on_non_alphanumerics(self):
        self.assertEqual(tokenize("Hello, World! json.loads() v2"), ["hello", "world", "json", "loads", "v2"])

    def test_analyze_drops_stopwords_and_single_letters(self):
        terms = analyze("The parser is a tool for the JSON format")
        self.assertNotIn("the", terms)
        self.assertNotIn("is", terms)
        self.assertNotIn("a", terms)
        self.assertIn("json", terms)
        self.assertTrue({"the", "is", "for"} <= STOPWORDS)

    def test_analyze_keeps_digits(self):
        self.assertIn("3", analyze("python 3"))

    def test_stem_conflates_inflections(self):
        self.assertEqual(len({stem(w) for w in ("parse", "parses", "parsed", "parsing")}), 1)
        self.assertEqual(stem("running"), stem("run"))
        self.assertEqual(stem("dictionaries"), stem("dictionary"))

    def test_query_and_document_analysis_agree(self):
        self.assertEqual(analyze("Parsing files"), analyze("parse file"))

    def test_normalize_query_collapses_whitespace_and_case(self):
        self.assertEqual(normalize_query("  Parse   JSON\tFile "), "parse json file")


class StripHtmlTests(unittest.TestCase):
    HTML = """<html><head><title>JSON &amp; you</title><style>.x{color:red}</style></head>
    <body><nav>Home | About</nav><script>var secret = 1;</script>
    <h1>Encoding</h1><p>Use <b>json.dumps</b> to serialise.</p><a href="/next">next</a><a href="b.html#top">b</a>
    </body></html>"""

    def test_extracts_title_text_and_links(self):
        page = strip_html(self.HTML)
        self.assertEqual(page.title, "JSON & you")
        self.assertIn("json.dumps", page.text)
        self.assertIn("Encoding", page.text)
        self.assertEqual(page.links, ["/next", "b.html#top"])

    def test_drops_script_style_and_nav(self):
        text = strip_html(self.HTML).text
        self.assertNotIn("secret", text)
        self.assertNotIn("color", text)
        self.assertNotIn("About", text)


if __name__ == "__main__":
    unittest.main()
