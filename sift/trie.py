"""Prefix trie for autocomplete, ranked by weight (query popularity).

Every node keeps a small cached list of its best completions, refreshed along the insert path, so `suggest()` is
O(len(prefix) + k) instead of walking the whole subtree. A full subtree walk is still used as the fallback when a
node's cache is smaller than the requested k.
"""

from __future__ import annotations

import heapq
import threading


class _Node:
    __slots__ = ("children", "weight", "top")

    def __init__(self) -> None:
        self.children: dict[str, _Node] = {}
        self.weight: float | None = None          # set when a phrase ends here
        self.top: list[tuple[float, str]] = []    # cached best completions below this node, best first


def _rank_key(item: tuple[float, str]) -> tuple[float, str]:
    weight, text = item
    return (-weight, text)                        # heavier first, then alphabetical


class Trie:
    def __init__(self, top_k: int = 10):
        self.root = _Node()
        self.top_k = top_k
        self._size = 0
        self._lock = threading.RLock()
        self._kinds: dict[str, str] = {}

    @staticmethod
    def _norm(text: str) -> str:
        return " ".join(text.lower().split())

    def add(self, text: str, weight: float = 1.0, kind: str = "query") -> float:
        """Increase the weight of `text` by `weight` (inserting it if new). Returns the new weight."""
        text = self._norm(text)
        if not text:
            raise ValueError("cannot insert an empty string")
        if weight < 0:
            raise ValueError("weights only grow (the cached top lists rely on it)")
        with self._lock:
            path = [self.root]
            node = self.root
            for ch in text:
                node = node.children.setdefault(ch, _Node())
                path.append(node)
            if node.weight is None:
                self._size += 1
                node.weight = 0.0
            node.weight += weight
            if kind == "query" or text not in self._kinds:
                self._kinds[text] = kind
            new_weight = node.weight
            for n in path:                         # refresh the cached top lists on the way down
                entries = [e for e in n.top if e[1] != text]
                entries.append((new_weight, text))
                entries.sort(key=_rank_key)
                n.top = entries[: self.top_k]
            return new_weight

    def weight(self, text: str) -> float | None:
        node = self._find(self._norm(text))
        return node.weight if node else None

    def _find(self, prefix: str) -> _Node | None:
        node = self.root
        for ch in prefix:
            node = node.children.get(ch)
            if node is None:
                return None
        return node

    def suggest(self, prefix: str, k: int = 8) -> list[dict]:
        prefix = self._norm(prefix)
        if not prefix or k <= 0:
            return []
        with self._lock:
            node = self._find(prefix)
            if node is None:
                return []
            if k <= len(node.top) or len(node.top) < self.top_k:
                best = node.top[:k]
            else:
                best = self._walk(node, prefix, k)
            return [{"text": t, "weight": round(w, 3), "kind": self._kinds.get(t, "query")} for w, t in best]

    def _walk(self, node: _Node, prefix: str, k: int) -> list[tuple[float, str]]:
        found: list[tuple[float, str]] = []
        stack = [(node, prefix)]
        while stack:
            n, text = stack.pop()
            if n.weight is not None:
                found.append((n.weight, text))
            for ch, child in n.children.items():
                stack.append((child, text + ch))
        return heapq.nsmallest(k, found, key=_rank_key)

    def __contains__(self, text: str) -> bool:
        return self.weight(text) is not None

    def __len__(self) -> int:
        return self._size
