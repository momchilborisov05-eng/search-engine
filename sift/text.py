"""Text pipeline: HTML stripping, tokenization, stopwords and a small suffix-stripping stemmer.

The same `analyze()` runs over documents at index time and over queries at search time, so a term only ever has
one spelling inside the engine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from html.parser import HTMLParser

TOKEN_RE = re.compile(r"[a-z0-9]+")
WORD_RE = re.compile(r"[A-Za-z0-9]+")

STOPWORDS = frozenset(
    """
    a about above after again against all am an and any are aren as at be because been before being below between
    both but by can cannot could did didn do does doesn doing don down during each few for from further had hasn
    has have haven having he her here hers herself him himself his how i if in into is isn it its itself just let
    me more most mustn my myself no nor not now of off on once only or other ought our ours ourselves out over own
    same shan she should shouldn so some such than that the their theirs them themselves then there these they
    this those through to too under until up very was wasn we were weren what when where which while who whom why
    will with won would wouldn you your yours yourself yourselves s t ll re ve d m o y also may e g eg ie etc
    """.split()
)

# Ordered longest-first inside each group; the first matching rule wins.
_SUFFIX_RULES: tuple[tuple[str, str], ...] = (
    ("ational", "ate"), ("ization", "ize"), ("fulness", "ful"), ("ousness", "ous"), ("iveness", "ive"),
    ("tional", "tion"), ("ically", "ic"), ("ations", "ate"), ("ation", "ate"), ("ingly", ""), ("edly", ""),
    ("ments", "ment"), ("ies", "y"), ("sses", "ss"), ("ing", ""), ("ed", ""), ("ly", ""), ("es", "e"), ("s", ""),
)
_NO_PLURAL = ("ss", "us", "is", "ous")
_VOWELS = set("aeiouy")


@lru_cache(maxsize=65536)
def stem(word: str) -> str:
    """Deliberately small stemmer (a subset of Porter step 1/2) that maps common inflections onto one form:
    parse/parses/parsed/parsing -> pars, running -> run, dictionaries -> dictionary."""
    if len(word) <= 3 or word.isdigit():
        return word
    for suffix, repl in _SUFFIX_RULES:
        if not word.endswith(suffix):
            continue
        if suffix == "s" and word.endswith(_NO_PLURAL):
            break
        base = word[: len(word) - len(suffix)] + repl
        # keep the stem pronounceable: at least 3 letters and one vowel
        if len(base) >= 3 and any(c in _VOWELS for c in base):
            word = base
        break
    if len(word) > 4 and word.endswith("e"):
        word = word[:-1]
    if len(word) > 3 and word[-1] == word[-2] and word[-1] not in "lsz" and word[-1] not in _VOWELS:
        word = word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric runs. Underscores, dots and punctuation split tokens (json.dumps -> json, dumps)."""
    return TOKEN_RE.findall(text.lower())


def normalize_term(token: str) -> str | None:
    """Stopword filter + stem for one lowercase token; None when the token carries no meaning."""
    if token in STOPWORDS or (len(token) < 2 and not token.isdigit()):
        return None
    return stem(token)


def analyze(text: str) -> list[str]:
    """Text -> index terms. Positions in the returned list are what phrase queries compare."""
    out = []
    for tok in tokenize(text):
        term = normalize_term(tok)
        if term:
            out.append(term)
    return out


def normalize_query(q: str) -> str:
    """Canonical form used for analytics grouping, autocomplete and the cache key."""
    return " ".join(q.lower().split())


# -- HTML ------------------------------------------------------------------------------------------------------
_BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "tr", "table", "section",
          "article", "main", "header", "footer", "nav", "blockquote", "dt", "dd"}
_SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "head"}


@dataclass
class ParsedPage:
    title: str = ""
    text: str = ""
    links: list[str] = field(default_factory=list)


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.links: list[str] = []
        self.skip_depth = 0
        self.in_title = False
        self.in_nav = 0

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        elif tag in _SKIP:
            self.skip_depth += 1
        if tag == "nav":
            self.in_nav += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag in _SKIP and self.skip_depth:
            self.skip_depth -= 1
        if tag == "nav" and self.in_nav:
            self.in_nav -= 1
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        elif not self.skip_depth and not self.in_nav:  # navigation chrome is boilerplate, not content
            self.parts.append(data)


def strip_html(html: str) -> ParsedPage:
    """Visible text, <title> and hrefs of an HTML page. Scripts, styles and <nav> are dropped."""
    p = _Extractor()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 - malformed markup still yields whatever was parsed so far
        pass
    lines = (" ".join(line.split()) for line in "".join(p.parts).split("\n"))
    text = "\n".join(line for line in lines if line)
    return ParsedPage(title=" ".join("".join(p.title_parts).split()), text=text, links=p.links)
