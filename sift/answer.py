""""AI answer" mode: retrieval-augmented answers over the top BM25 results, with numbered citations.

- With ANTHROPIC_API_KEY: Claude (default claude-sonnet-5-5) answers from the top-k pages only and cites them as [n].
- Without a key, or when the API call fails or declines: an extractive answer built from the highest-scoring
  sentences of those same pages, also cited. The app is fully usable without a key.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .index import idf
from .text import analyze

log = logging.getLogger(__name__)

SOURCE_CHARS = 1800
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'`])|\n+")
CITATION_RE = re.compile(r"\[(\d+)\]")

SYSTEM_PROMPT = """You answer questions for a documentation search engine using only the numbered sources provided \
in the user message.

Rules:
- Base every statement on the sources. Do not add facts that are not in them.
- Cite the source number in square brackets right after each sentence that uses it, for example [1] or [2][3].
- Keep the answer short: two to five sentences, plain text, no headings. Include a short code example only if a source contains one.
- If the sources do not contain the answer, reply exactly: "The indexed pages don't answer this question." and nothing else."""


@dataclass
class Source:
    n: int
    doc_id: int
    title: str
    url: str
    text: str


class ExtractiveAnswerer:
    """Picks the sentences that cover the most (IDF-weighted) query terms, at most one per source first."""

    mode = "extractive"

    def answer(self, question: str, sources: list[Source], n_docs: int, df: Any) -> str:
        q_terms = set(analyze(question))
        if not q_terms or not sources:
            return ""
        weights = {t: idf(df(t), max(n_docs, 1)) for t in q_terms}
        scored: list[tuple[float, int, int, str]] = []
        for src in sources:
            for i, sent in enumerate(SENTENCE_RE.split(src.text)):
                sent = " ".join(sent.split())
                if not 25 <= len(sent) <= 400 or sent.startswith(">>>"):
                    continue
                terms = set(analyze(sent))
                covered = q_terms & terms
                if not covered:
                    continue
                score = sum(weights[t] for t in covered) - 0.02 * i - 0.05 * src.n  # earlier sentences/sources win ties
                scored.append((score, src.n, i, sent))
        if not scored:
            return ""
        scored.sort(key=lambda x: -x[0])
        picked: list[tuple[float, int, int, str]] = []
        used_sources: set[int] = set()
        for item in scored:  # first pass: best sentence per source
            if item[1] not in used_sources and len(picked) < 3:
                picked.append(item)
                used_sources.add(item[1])
        picked.sort(key=lambda x: (x[1], x[2]))
        return " ".join(f"{s.rstrip()} [{n}]" for _, n, _, s in picked)


class ClaudeAnswerer:
    mode = "claude"

    def __init__(self, settings: Settings, client: Any = None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=2, timeout=60.0)
        self.client = client
        self.model = settings.claude_model
        self.effort = settings.claude_effort
        self.last_usage: dict = {}

    def request_kwargs(self, question: str, sources: list[Source]) -> dict:
        blocks = "\n\n".join(f"[{s.n}] {s.title}\nURL: {s.url}\n{s.text}" for s in sources)
        return {
            "model": self.model,
            "max_tokens": 8000,
            "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": f"Sources:\n\n{blocks}\n\nQuestion: {question}"}],
            "output_config": {"effort": self.effort},
            # server-side fallback: if the model declines, the API retries on a fallback model automatically
            "betas": ["server-side-fallback-2026-07-01"],
            "fallbacks": "default",
        }

    def answer(self, question: str, sources: list[Source]) -> str:
        resp = self.client.beta.messages.create(**self.request_kwargs(question, sources))
        if getattr(resp, "stop_reason", None) == "refusal":
            raise RuntimeError("the model declined to answer (stop_reason=refusal)")
        u = getattr(resp, "usage", None)
        self.last_usage = {"input_tokens": getattr(u, "input_tokens", 0), "output_tokens": getattr(u, "output_tokens", 0)}
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()
        if not text:
            raise RuntimeError("empty answer from the model")
        return text


class AnswerService:
    def __init__(self, engine, settings: Settings, client: Any = None):
        self.engine = engine
        self.settings = settings
        self.extractive = ExtractiveAnswerer()
        self.claude = ClaudeAnswerer(settings, client) if (settings.ai_live or client is not None) else None

    def sources_for(self, question: str, k: int) -> list[Source]:
        ranked = self.engine.rank(question)[:k]
        pages = self.engine.store.get_pages([d for d, _ in ranked])
        out = []
        for n, (doc_id, _) in enumerate(ranked, start=1):
            p = pages.get(doc_id)
            if p:
                out.append(Source(n, doc_id, p["title"], p["url"], p["body"][:SOURCE_CHARS]))
        return out

    def answer(self, question: str, k: int | None = None) -> dict:
        k = k or self.settings.answer_top_k
        sources = self.sources_for(question, k)
        result = {"question": question, "mode": "extractive", "model": None, "answer": "", "note": None,
                  "citations": [{"n": s.n, "doc_id": s.doc_id, "title": s.title, "url": s.url} for s in sources]}
        if not sources:
            result["answer"] = "No indexed page matches this question."
            return result
        if self.claude is not None:
            try:
                text = self.claude.answer(question, sources)
                result.update(mode="claude", model=self.claude.model, answer=text, usage=self.claude.last_usage)
                return self._keep_cited(result)
            except Exception as e:  # noqa: BLE001 - any API problem degrades to the extractive answer
                log.warning("Claude answer failed, using the extractive fallback: %s", e)
                result["note"] = f"AI answer unavailable ({type(e).__name__}); showing an extractive answer."
        idx = self.engine.index
        text = self.extractive.answer(question, sources, idx.n_docs, idx.df)
        result["answer"] = text or "The top results don't contain a sentence that answers this directly."
        return self._keep_cited(result)

    @staticmethod
    def _keep_cited(result: dict) -> dict:
        """Drop citation numbers that point at no source, and mark which sources the answer actually used."""
        valid = {c["n"] for c in result["citations"]}
        text = CITATION_RE.sub(lambda m: m.group(0) if int(m.group(1)) in valid else "", result["answer"])
        used = {int(n) for n in CITATION_RE.findall(text)}
        result["answer"] = text
        for c in result["citations"]:
            c["used"] = c["n"] in used
        return result
