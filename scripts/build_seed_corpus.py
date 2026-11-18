"""Build the bundled offline seed corpus from Python standard-library docstrings.

Each public module, class and function with a meaningful docstring becomes one small HTML page whose URL points at
the matching docs.python.org anchor. The pages go through the same HTML-stripping pipeline as crawled pages.

The output (sift/data/seed_corpus.jsonl.gz) is committed, so the demo and the tests never depend on the Python
version that runs them. Re-run only to regenerate it:

    python scripts/build_seed_corpus.py
"""

from __future__ import annotations

import gzip
import html
import importlib
import inspect
import json
import pathlib
import sys

OUT = pathlib.Path(__file__).resolve().parents[1] / "sift" / "data" / "seed_corpus.jsonl.gz"

MODULES = [
    "abc", "argparse", "array", "base64", "bisect", "calendar", "collections", "configparser", "contextlib", "copy",
    "csv", "dataclasses", "datetime", "decimal", "difflib", "enum", "fractions", "functools", "getpass", "glob",
    "gzip", "hashlib", "heapq", "html", "html.parser", "http.client", "inspect", "io", "itertools", "json",
    "logging", "math", "operator", "os", "os.path", "pathlib", "platform", "pprint", "queue", "random", "re",
    "secrets", "shlex", "shutil", "socket", "sqlite3", "statistics", "string", "struct", "subprocess", "tempfile",
    "textwrap", "threading", "time", "traceback", "typing", "unittest", "urllib.parse", "urllib.request", "uuid",
    "warnings", "weakref", "xml.etree.ElementTree", "zipfile", "zoneinfo", "asyncio", "email.message", "hmac",
    "ipaddress", "mimetypes", "numbers", "pickle", "sched", "selectors", "signal", "ssl", "tarfile", "timeit",
    "tokenize", "types", "unicodedata", "venv", "wave", "zlib", "codecs", "locale", "gettext", "filecmp",
]
PER_MODULE = 9
MIN_DOC = 90
MAX_DOC = 3500


def page_for(module: str, qualname: str, kind: str, signature: str, doc: str) -> dict:
    anchor = qualname if qualname != module else f"module-{module}"
    url = f"https://docs.python.org/3/library/{module}.html#{anchor}"
    paras = [p.strip() for p in doc.split("\n\n") if p.strip()]
    body = "\n".join(
        f"<pre>{html.escape(p)}</pre>" if p.startswith((">>>", "    ")) else f"<p>{html.escape(' '.join(p.split()))}</p>"
        for p in paras
    )
    sig = f'<pre class="sig">{html.escape(signature)}</pre>' if signature else ""
    title = f"{qualname} ({kind})" if qualname != module else f"{module} module"
    page = (
        f"<!doctype html><html><head><title>{html.escape(title)} - Python standard library</title>"
        f"<style>body{{font-family:sans-serif}}</style><script>window.analytics=1;</script></head><body>"
        f'<nav><a href="https://docs.python.org/3/library/index.html">Library</a> &raquo; '
        f'<a href="https://docs.python.org/3/library/{module}.html">{module}</a></nav>'
        f"<main><h1>{html.escape(qualname)}</h1>{sig}{body}</main>"
        f"<footer>Text from the Python {sys.version_info.major}.{sys.version_info.minor} docstrings (PSF License).</footer>"
        f"</body></html>"
    )
    return {"url": url, "html": page}


def clean(doc: str | None) -> str:
    doc = inspect.cleandoc(doc if isinstance(doc, str) else "")
    return doc[:MAX_DOC]


def main() -> None:
    pages: list[dict] = []
    seen: set[str] = set()
    for name in MODULES:
        try:
            mod = importlib.import_module(name)
        except Exception:  # noqa: BLE001 - a module missing on this platform is simply skipped
            continue
        mdoc = clean(mod.__doc__)
        if len(mdoc) >= MIN_DOC:
            pages.append(page_for(name, name, "module", "", mdoc))
        count = 0
        for attr in sorted(getattr(mod, "__all__", None) or dir(mod)):
            if attr.startswith("_") or count >= PER_MODULE:
                continue
            obj = getattr(mod, attr, None)
            if not (inspect.isfunction(obj) or inspect.isclass(obj) or inspect.isbuiltin(obj)):
                continue
            doc = clean(obj.__doc__)
            if len(doc) < MIN_DOC:
                continue
            key = f"{name}.{attr}"
            if key in seen or doc in seen:
                continue
            seen.update((key, doc))
            try:
                sig = f"{key}{inspect.signature(obj)}"
            except (TypeError, ValueError):
                sig = ""
            kind = "class" if inspect.isclass(obj) else "function"
            pages.append(page_for(name, key, kind, sig, doc))
            count += 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as fh:
        for p in pages:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    print(f"wrote {len(pages)} pages to {OUT} ({OUT.stat().st_size / 1024:.0f} KiB)")


if __name__ == "__main__":
    main()
