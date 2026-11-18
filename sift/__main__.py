"""Command line: serve the app, crawl a site, rebuild the index, search from the terminal."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import Settings


def _engine(settings: Settings, demo_traffic: bool | None = None):
    from dataclasses import replace

    from .engine import SearchEngine

    if demo_traffic is not None:
        settings = replace(settings, demo_traffic=demo_traffic)
    return SearchEngine(settings).startup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sift", description="A search engine built from scratch.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the web app (Flask dev server)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)

    c = sub.add_parser("crawl", help="crawl a site politely, store pages, rebuild the index")
    c.add_argument("urls", nargs="+")
    c.add_argument("--max-pages", type=int, default=30)
    c.add_argument("--delay", type=float, default=1.0, help="minimum seconds between requests to one host")

    sub.add_parser("reindex", help="rebuild the inverted index from the stored pages")

    q = sub.add_parser("search", help="search from the terminal")
    q.add_argument("query")
    q.add_argument("--page", type=int, default=1)
    q.add_argument("--size", type=int, default=10)

    a = sub.add_parser("answer", help="AI answer (Claude with a key, extractive without)")
    a.add_argument("question")

    sub.add_parser("stats", help="index and cache statistics as JSON")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()

    if args.cmd == "serve":
        from .web import create_app

        create_app(settings).run(host=args.host, port=args.port, threaded=True)
        return 0

    if args.cmd == "crawl":
        from .crawler import Crawler

        engine = _engine(settings, demo_traffic=False)
        report = Crawler(engine.store, delay=args.delay, max_pages=args.max_pages).crawl(args.urls)
        build = engine.reindex()
        print(json.dumps({"fetched": report.fetched, "stored": report.stored,
                          "blocked_by_robots": len(report.blocked_by_robots), "skipped_offsite": report.skipped_offsite,
                          "skipped_non_html": report.skipped_non_html, "errors": report.errors[:10],
                          "crawl_seconds": round(report.seconds, 2), "index_docs": build.docs}, indent=2))
        return 0

    engine = _engine(settings, demo_traffic=False)
    if args.cmd == "reindex":
        b = engine.reindex()
        print(f"indexed {b.docs} documents, {b.terms} terms, {b.postings} postings in {b.seconds:.3f}s")
    elif args.cmd == "search":
        res = engine.search(args.query, page=args.page, size=args.size, source="cli")
        print(f"{res['total']} results in {res['took_ms']:.2f} ms (cache {'hit' if res['cache_hit'] else 'miss'})")
        for r in res["results"]:
            print(f"{r['rank']:>3}. {r['title']}  [{r['score']:.2f}]\n     {r['url']}")
    elif args.cmd == "answer":
        from .answer import AnswerService

        out = AnswerService(engine, settings).answer(args.question)
        print(f"[{out['mode']}] {out['answer']}\n")
        for cit in out["citations"]:
            print(f"  [{cit['n']}] {cit['title']} - {cit['url']}")
    elif args.cmd == "stats":
        print(json.dumps(engine.stats(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
