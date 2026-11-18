"""Flask JSON API + the static vanilla-JS UI."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from .. import __version__
from ..answer import AnswerService
from ..config import Settings
from ..engine import MAX_QUERY_LEN, SearchEngine

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"

CSP = (
    "default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
)


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _int_arg(name: str, default: int, lo: int, hi: int) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(f"'{name}' must be an integer") from None
    if not lo <= value <= hi:
        raise ApiError(f"'{name}' must be between {lo} and {hi}")
    return value


def _json_body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("request body must be a JSON object")
    return data


def _query_text(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApiError("'q' is required")
    if len(value) > MAX_QUERY_LEN:
        raise ApiError(f"'q' must be at most {MAX_QUERY_LEN} characters")
    return value.strip()


def create_app(settings: Settings | None = None, engine: SearchEngine | None = None, answer_client=None) -> Flask:
    settings = settings or Settings.from_env()
    if engine is None:
        engine = SearchEngine(settings).startup()
    answers = AnswerService(engine, settings, client=answer_client)
    started = time.time()

    app = Flask(__name__, static_folder=None)
    app.config["JSON_SORT_KEYS"] = False
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
    app.extensions["sift_engine"] = engine

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        if resp.mimetype == "text/html":
            resp.headers.setdefault("Content-Security-Policy", CSP)
        return resp

    @app.errorhandler(ApiError)
    def api_error(e: ApiError):
        return jsonify(error=e.message), e.status

    @app.errorhandler(HTTPException)
    def http_error(e: HTTPException):
        return jsonify(error=e.description or e.name), e.code

    @app.errorhandler(Exception)
    def unhandled(e: Exception):
        log.exception("unhandled error")
        return jsonify(error="internal server error"), 500

    # pages -----------------------------------------------------------------------------------------------------
    @app.get("/")
    def index_page():
        return send_from_directory(STATIC, "index.html")

    @app.get("/analytics")
    def analytics_page():
        return send_from_directory(STATIC, "analytics.html")

    @app.get("/static/<path:name>")
    def static_files(name: str):
        return send_from_directory(STATIC, name, max_age=3600)

    # api -------------------------------------------------------------------------------------------------------
    @app.get("/health")
    def health():
        return jsonify(status="ok", version=__version__, documents=engine.index.n_docs,
                       uptime_seconds=round(time.time() - started, 1),
                       ai_mode="claude" if answers.claude else "extractive")

    @app.get("/api/search")
    def api_search():
        q = _query_text(request.args.get("q"))
        page = _int_arg("page", 1, 1, 100)
        size = _int_arg("size", settings.page_size, 1, 50)
        try:
            return jsonify(engine.search(q, page=page, size=size))
        except ValueError as e:
            raise ApiError(str(e)) from None

    @app.get("/api/suggest")
    def api_suggest():
        prefix = request.args.get("q", "")
        if len(prefix) > MAX_QUERY_LEN:
            raise ApiError(f"'q' must be at most {MAX_QUERY_LEN} characters")
        k = _int_arg("k", 8, 1, 20)
        return jsonify(prefix=prefix, suggestions=engine.suggest(prefix, k))

    @app.post("/api/click")
    def api_click():
        data = _json_body()
        try:
            query_id, doc_id, rank = (int(data[k]) for k in ("query_id", "doc_id", "rank"))
        except (KeyError, TypeError, ValueError):
            raise ApiError("'query_id', 'doc_id' and 'rank' are required integers") from None
        if rank < 1:
            raise ApiError("'rank' must be >= 1")
        try:
            click_id = engine.click(query_id, doc_id, rank)
        except LookupError as e:
            raise ApiError(str(e), 404) from None
        return jsonify(ok=True, click_id=click_id), 201

    @app.post("/api/answer")
    def api_answer():
        data = _json_body()
        q = _query_text(data.get("q"))
        k = data.get("k", settings.answer_top_k)
        if not isinstance(k, int) or not 1 <= k <= 10:
            raise ApiError("'k' must be an integer between 1 and 10")
        return jsonify(answers.answer(q, k))

    @app.get("/api/doc/<int:doc_id>")
    def api_doc(doc_id: int):
        p = engine.store.get_page(doc_id)
        if p is None:
            raise ApiError("document not found", 404)
        return jsonify(id=p["id"], url=p["url"], title=p["title"], body=p["body"], source=p["source"])

    @app.get("/api/analytics")
    def api_analytics():
        days = _int_arg("days", 7, 1, 365)
        summary = engine.analytics.summary(days=days)
        summary["cache"] = engine.cache.stats()
        return jsonify(summary)

    @app.get("/api/stats")
    def api_stats():
        return jsonify({**engine.stats(), "ai_mode": "claude" if answers.claude else "extractive",
                        "ai_model": settings.claude_model if answers.claude else None})

    return app
