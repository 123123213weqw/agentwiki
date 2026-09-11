"""Render every route in a real browser and assert it actually painted.

Why this exists
---------------
`node --check` validates syntax and nothing else.  Three separate bugs in this UI
were invisible to it and to every other check, because they only appeared once
the page ran:

  * `viewGraph()` referenced `KIND_COLOR`, which was declared *inside*
    `startGraph()` -> ReferenceError -> the page hung on "正在加载 wiki.json…".
  * `app.js` and `styles.css` drifted into two different naming conventions
    (37 elements unstyled) while still "working".
  * `styles.css` referenced `--radius`, a variable that was never declared, so
    the declaration was silently dropped.

The first is a runtime error, the second and third are visual.  A screenshot
catches all three, so this script takes one -- but asserts against the rendered
DOM rather than an image, which makes it cheap and pass/fail.

Usage
-----
    python tools/render_check.py [--port 8787]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Chromium-family browsers are the only ones that reliably exist here.  Edge
# ships with Windows, so it is the last-resort fallback.
BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

# The literal string the shell shows before boot() replaces it.  If it survives
# into the rendered DOM, a script threw before rendering.
LOADING_MARKER = "正在加载 wiki.json"
ERROR_MARKER = "无法加载 wiki.json"

# route -> a string that must be present once that route has rendered.
ROUTES = {
    "#/": "知识地形",
    "#/entities": "点开任意一个看它的出处与关联",
    "#/sessions": "按最近活动排序",
    "#/graph": "同一个会话",
    "#/entity/file:preprocessor.py": "出处",
}


def find_browser() -> str | None:
    for path in BROWSERS:
        if Path(path).exists():
            return path
    return shutil.which("chrome") or shutil.which("msedge")


def dump_dom(browser: str, url: str, timeout: int = 60) -> str:
    """Render `url` and return the serialised DOM after scripts have run."""
    proc = subprocess.run(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--virtual-time-budget=8000",
            "--dump-dom",
            url,
        ],
        capture_output=True,
        timeout=timeout,
    )
    # Chromium writes noise to stderr and the DOM to stdout; be lenient about
    # the encoding because the page is full of CJK.
    return proc.stdout.decode("utf-8", errors="replace")


def server_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5):
            return True
    except (urllib.error.URLError, OSError):
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()

    browser = find_browser()
    if browser is None:
        print("SKIP: no Chromium-family browser found; cannot render-check.")
        return 0

    if not server_alive(args.port):
        print(f"SKIP: nothing serving on 127.0.0.1:{args.port}. "
              f"Start it with: python -m agentwiki serve")
        return 0

    print(f"browser: {browser}")
    print(f"server:  http://127.0.0.1:{args.port}\n")

    failures = []
    for route, expect in ROUTES.items():
        url = f"http://127.0.0.1:{args.port}/{route}"
        try:
            dom = dump_dom(browser, url)
        except subprocess.TimeoutExpired:
            failures.append(f"{route}: browser timed out")
            print(f"  FAIL {route}: timed out")
            continue

        problems = []
        if LOADING_MARKER in dom:
            problems.append("still stuck on the loading state (a script threw)")
        if ERROR_MARKER in dom:
            problems.append("rendered the error state (wiki.json failed to load)")
        if expect not in dom:
            problems.append(f"expected text not found: {expect!r}")

        if problems:
            failures.append(f"{route}: " + "; ".join(problems))
            print(f"  FAIL {route}")
            for p in problems:
                print(f"       - {p}")
        else:
            print(f"  ok   {route}")

    print()
    if failures:
        print(f"RENDER FAIL: {len(failures)} route(s) did not paint correctly.")
        return 1
    print(f"RENDER OK: all {len(ROUTES)} routes painted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
