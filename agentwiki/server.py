"""A tiny local web server for the wiki UI.

Two reasons this exists instead of just opening `index.html`:

1. Browsers refuse `fetch()` over `file://`, so a purely static page cannot load
   `wiki.json`.  A server is required, so it may as well be trivial.
2. It keeps the core dependency-free: this is `http.server` from the standard
   library, no Flask, no npm, no build step.

Security posture: binds to 127.0.0.1 only, serves exactly two roots (the repo's
`web/` assets and the generated `wiki.json`), and rejects path traversal.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import config

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".woff2": "font/woff2",
}


def _safe_join(root: Path, rel: str) -> Path | None:
    """Resolve `rel` under `root`, or None if it escapes."""
    rel = rel.lstrip("/")
    if not rel:
        return None
    cand = (root / rel).resolve()
    try:
        cand.relative_to(root.resolve())
    except ValueError:
        return None
    return cand


def make_handler(json_path: Path, asset_root: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentWiki/1.0"

        def log_message(self, fmt, *args):  # keep the console readable
            pass

        def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # Local data changes whenever you re-ingest; never let it be cached.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _text(self, msg: str, code: int) -> None:
            self._send(msg.encode("utf-8"), "text/plain; charset=utf-8", code)

        def do_HEAD(self):  # noqa: N802
            self.do_GET()

        def do_GET(self):  # noqa: N802
            path = unquote(urlparse(self.path).path)

            if path in ("/", "/index.html"):
                path = "/index.html"

            # The exported dataset lives outside the repo (it is personal data),
            # so it is mapped explicitly rather than served from the asset root.
            if path == "/wiki.json":
                if not json_path.exists():
                    self._text("wiki.json not found. Run: python -m agentwiki export", 404)
                    return
                try:
                    body = json_path.read_bytes()
                except OSError as e:
                    self._text(f"cannot read wiki.json: {e}", 500)
                    return
                self._send(body, CONTENT_TYPES[".json"])
                return

            target = _safe_join(asset_root, path)
            if target is None or not target.is_file():
                self._text("not found", 404)
                return
            try:
                body = target.read_bytes()
            except OSError as e:
                self._text(f"cannot read asset: {e}", 500)
                return
            ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
            self._send(body, ctype)

    return Handler


def _free_port(preferred: int) -> int:
    """Use `preferred` if it is free, otherwise let the OS pick one."""
    for candidate in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return s.getsockname()[1]
    return preferred


def serve(*, port: int = 8765, open_browser: bool = True, quiet: bool = False) -> int:
    json_path = config.WEB_DIR / "wiki.json"
    asset_root = config.WEB_SRC

    if not asset_root.is_dir():
        raise FileNotFoundError(f"web assets not found at {asset_root}")
    if not json_path.exists():
        raise FileNotFoundError(
            f"{json_path} not found -- run `python -m agentwiki export` first"
        )

    port = _free_port(port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(json_path, asset_root))
    url = f"http://127.0.0.1:{port}/"

    meta = {}
    try:
        meta = json.loads(json_path.read_text(encoding="utf-8")).get("meta", {})
    except (OSError, ValueError):
        pass

    if not quiet:
        print(f"AgentWiki UI   {url}")
        if meta:
            print(f"  generated    {meta.get('generated_at')}")
            caps = meta.get("caps") or {}
            print(f"  export caps  entities<={caps.get('max_entities')} "
                  f"snippets<={caps.get('snippets_per_entity')}")
        print(f"  data         {json_path}  ({json_path.stat().st_size/1e6:.1f} MB)")
        print("  Ctrl+C to stop")

    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        if not quiet:
            print("\nstopped.")
    finally:
        httpd.server_close()
    return 0
