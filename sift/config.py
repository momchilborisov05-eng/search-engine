"""Settings from environment variables (and an optional .env file). Everything has a working default."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv is optional
        return
    load_dotenv(override=False)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, default))))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    db_path: str = "sift.db"
    seed: bool = True                    # load the bundled corpus when the database has no pages
    demo_traffic: bool = True            # replay simulated searches/clicks once so the dashboard is not empty
    cache_size: int = 256
    cache_ttl: float = 300.0             # seconds; 0 disables expiry
    page_size: int = 10
    k1: float = 1.2
    b: float = 0.75
    title_boost: float = 0.6
    anthropic_api_key: str = field(default="", repr=False)
    claude_model: str = "claude-sonnet-5-5"
    claude_effort: str = "low"
    answer_top_k: int = 5

    @property
    def ai_live(self) -> bool:
        return bool(self.anthropic_api_key)

    @classmethod
    def from_env(cls) -> Settings:
        _load_dotenv()
        return cls(
            db_path=os.environ.get("SIFT_DB", "sift.db"),
            seed=_bool("SIFT_SEED", True),
            demo_traffic=_bool("SIFT_DEMO_TRAFFIC", True),
            cache_size=_int("SIFT_CACHE_SIZE", 256, 1, 100_000),
            cache_ttl=_float("SIFT_CACHE_TTL", 300.0),
            page_size=_int("SIFT_PAGE_SIZE", 10, 1, 50),
            k1=_float("SIFT_BM25_K1", 1.2),
            b=_float("SIFT_BM25_B", 0.75),
            title_boost=_float("SIFT_TITLE_BOOST", 0.6),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
            claude_model=os.environ.get("SIFT_CLAUDE_MODEL", "claude-sonnet-5-5"),
            claude_effort=os.environ.get("SIFT_CLAUDE_EFFORT", "low"),
            answer_top_k=_int("SIFT_ANSWER_TOP_K", 5, 1, 10),
        )
