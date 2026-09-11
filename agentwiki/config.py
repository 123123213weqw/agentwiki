"""Paths and safety policy for AgentWiki L0 ingestion.

Design rule: we only ever *read* the sources, and only from an explicit
allowlist of files.  Nothing under ~/.codex is ever globbed wholesale,
because auth.json / .sandbox-secrets live right next to the data we want.
"""
from __future__ import annotations

import os
from pathlib import Path

HOME = Path(os.path.expanduser("~"))

AGENTWIKI_HOME = Path(os.environ.get("AGENTWIKI_HOME", HOME / ".agentwiki"))
DB_PATH = AGENTWIKI_HOME / "wiki.db"
REPORT_DIR = AGENTWIKI_HOME / "reports"
SNAPSHOT_DIR = AGENTWIKI_HOME / "snapshots"
# Where `export` drops wiki.json, i.e. the data the UI fetches.
WEB_DIR = AGENTWIKI_HOME / "web"
# Static assets shipped in the repo (index.html / app.js / styles.css).
WEB_SRC = Path(__file__).resolve().parent.parent / "web"

# ---------------------------------------------------------------- codex
CODEX_DIR = HOME / ".codex"
CODEX_STATE = CODEX_DIR / "state_5.sqlite"          # threads metadata (the spine)
CODEX_HISTORY = CODEX_DIR / "thread_history_1.sqlite"  # turns + items
CODEX_ROLLOUTS = CODEX_DIR / "sessions"             # raw jsonl archive
CODEX_ARCHIVED = CODEX_DIR / "archived_sessions"

# ---------------------------------------------------------------- claude code
CLAUDE_PROJECTS = HOME / ".claude" / "projects"

# ---------------------------------------------------------------- xharness (this harness)
_xh = os.environ.get("APPDATA")
XHARNESS_SESSIONS = (Path(_xh) / "com.xlang.xharness" / "state" / "sessions") if _xh else None

# ---------------------------------------------------------------- secrets
# Never opened, never copied.  Listed so the guard is explicit and auditable.
FORBIDDEN_NAMES = {
    "auth.json",
    "cap_sid",
    "chrome-native-hosts-v2.json",
    ".sandbox-secrets",
    ".sandbox",
    "installation_id",
    "mcp-oauth-locks",
    "vendor_imports",
}

# Files larger than this are skipped by the jsonl ingesters.
MAX_JSONL_LINE = 2_000_000
# Per-item text stored in the wiki db (full original stays in raw_json).
MAX_ITEM_TEXT = 100_000


def ensure_dirs() -> None:
    for d in (AGENTWIKI_HOME, REPORT_DIR, SNAPSHOT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def is_forbidden(path: Path) -> bool:
    """True if any path component is on the never-touch list."""
    return any(part in FORBIDDEN_NAMES for part in path.parts)
