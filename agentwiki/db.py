"""Storage layer for AgentWiki.

Two kinds of databases:
  * SOURCE dbs   -- owned by other apps (codex). We only ever read a SNAPSHOT copy.
  * OUR store    -- ~/.agentwiki/wiki.db. We own it, we write it.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path
from typing import Iterable

from . import config, redact

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- ============================== L0 ==============================

CREATE TABLE IF NOT EXISTS sessions (
  session_id   TEXT PRIMARY KEY,
  source       TEXT NOT NULL,
  title        TEXT,
  cwd          TEXT,
  repo         TEXT,
  branch       TEXT,
  model        TEXT,
  tokens_used  INTEGER,
  created_at   INTEGER,
  updated_at   INTEGER,
  item_count   INTEGER DEFAULT 0,
  turn_count   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_sessions_source ON sessions(source, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_sessions_repo   ON sessions(repo);

CREATE TABLE IF NOT EXISTS turns (
  session_id   TEXT NOT NULL,
  turn_id      TEXT NOT NULL,
  status       TEXT,
  started_at   INTEGER,
  completed_at INTEGER,
  duration_ms  INTEGER,
  ordinal      INTEGER,
  PRIMARY KEY (session_id, turn_id)
);
CREATE INDEX IF NOT EXISTS ix_turns_dur ON turns(status, duration_ms);

CREATE TABLE IF NOT EXISTS items (
  source       TEXT NOT NULL,
  item_id      TEXT NOT NULL,
  session_id   TEXT NOT NULL,
  turn_id      TEXT,
  ord          INTEGER,
  created_at   INTEGER,
  item_type    TEXT NOT NULL,
  role         TEXT,
  phase        TEXT,
  text         TEXT,
  text_len     INTEGER DEFAULT 0,
  truncated    INTEGER DEFAULT 0,
  content_hash TEXT NOT NULL,
  raw_json     TEXT,
  PRIMARY KEY (source, item_id)
);
CREATE INDEX IF NOT EXISTS ix_items_session ON items(session_id, ord);
CREATE INDEX IF NOT EXISTS ix_items_type    ON items(item_type);
CREATE INDEX IF NOT EXISTS ix_items_hash    ON items(content_hash);

CREATE TABLE IF NOT EXISTS entities (
  entity_id     TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,
  canonical     TEXT NOT NULL,
  first_seen    INTEGER,
  last_seen     INTEGER,
  mention_count INTEGER DEFAULT 0,
  session_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_entities_kind ON entities(kind, mention_count DESC);

CREATE TABLE IF NOT EXISTS entity_aliases (
  alias     TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  source    TEXT
);

CREATE TABLE IF NOT EXISTS mentions (
  entity_id TEXT NOT NULL,
  item_id   TEXT NOT NULL,
  source    TEXT NOT NULL,
  n         INTEGER DEFAULT 1,
  PRIMARY KEY (entity_id, source, item_id)
);
CREATE INDEX IF NOT EXISTS ix_mentions_entity ON mentions(entity_id);

-- catalog of raw files we know about but may not fully parse yet
CREATE TABLE IF NOT EXISTS source_files (
  path      TEXT PRIMARY KEY,
  source    TEXT NOT NULL,
  size      INTEGER,
  mtime     INTEGER,
  parsed    INTEGER DEFAULT 0,
  note      TEXT
);

CREATE TABLE IF NOT EXISTS ingest_cursor (
  source     TEXT PRIMARY KEY,
  watermark  TEXT,
  updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS ingest_runs (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  source     TEXT,
  started_at INTEGER,
  ended_at   INTEGER,
  n_sessions INTEGER,
  n_items    INTEGER,
  note       TEXT
);

-- ====================== L1 / L2 (reserved) ======================
-- Declared now so W1/W2 need no migration.

CREATE TABLE IF NOT EXISTS claims (
  claim_id      TEXT PRIMARY KEY,
  entity_id     TEXT,
  kind          TEXT NOT NULL,
  statement     TEXT NOT NULL,
  evidence      TEXT NOT NULL,
  source_item   TEXT NOT NULL,
  session_id    TEXT,
  stated_at     INTEGER,
  confidence    REAL DEFAULT 0.7,
  valid_from    INTEGER,
  valid_to      INTEGER,
  superseded_by TEXT,
  dedup_key     TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_claims_entity ON claims(entity_id, stated_at);
CREATE INDEX IF NOT EXISTS ix_claims_kind   ON claims(kind);

CREATE TABLE IF NOT EXISTS pages (
  entity_id TEXT PRIMARY KEY,
  path      TEXT,
  revision  INTEGER DEFAULT 0,
  body_md   TEXT,
  compiled_at INTEGER,
  compiled_from_watermark TEXT
);

CREATE TABLE IF NOT EXISTS links (
  from_entity TEXT NOT NULL,
  to_entity   TEXT NOT NULL,
  kind        TEXT NOT NULL,
  weight      INTEGER DEFAULT 1,
  PRIMARY KEY (from_entity, to_entity, kind)
);
"""


def store() -> sqlite3.Connection:
    """Open (creating if needed) our own wiki database."""
    config.AGENTWIKI_HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# --------------------------------------------------------------------------
# Reading foreign sqlite databases safely
# --------------------------------------------------------------------------

def snapshot_sqlite(src: Path) -> Path:
    """Copy a live sqlite db (+ its WAL/SHM) so we never touch the original.

    Opening another app's WAL database read-only is unreliable: sqlite may need
    to write the -shm file, and a concurrent writer can hand us a torn view.
    Copying first is the only approach that is both safe and consistent.
    """
    config.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    dst = config.SNAPSHOT_DIR / (src.name + ".snap")
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        side = Path(str(src) + suffix)
        if side.exists():
            try:
                shutil.copy2(side, Path(str(dst) + suffix))
            except OSError:
                pass
    return dst


def open_snapshot(src: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(snapshot_sqlite(src)))
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def upsert_entity(conn, entity_id: str, kind: str, canonical: str, ts: int | None) -> None:
    conn.execute(
        """
        INSERT INTO entities (entity_id, kind, canonical, first_seen, last_seen, mention_count)
        VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT(entity_id) DO UPDATE SET
            mention_count = mention_count + 1,
            first_seen = CASE WHEN excluded.first_seen IS NULL THEN entities.first_seen
                              WHEN entities.first_seen IS NULL THEN excluded.first_seen
                              ELSE MIN(entities.first_seen, excluded.first_seen) END,
            last_seen  = CASE WHEN excluded.last_seen IS NULL THEN entities.last_seen
                              WHEN entities.last_seen IS NULL THEN excluded.last_seen
                              ELSE MAX(entities.last_seen, excluded.last_seen) END
        """,
        (entity_id, kind, canonical[:300], ts, ts),
    )


def insert_item(conn, row: dict) -> bool:
    """Insert one item. Returns True if newly inserted, False if unchanged.

    This is the single choke point for item writes, so the secret scrubber runs
    here *and* in the ingesters.  `raw_json` keeps the full original record for
    traceability, but it must never hold a live credential: the wiki is a
    searchable index that an LLM will read later.
    """
    row = dict(row)
    row["text"] = redact.scrub(row.get("text") or "")
    row["raw_json"] = redact.scrub(row.get("raw_json") or "")
    row["text_len"] = len(row["text"])
    cur = conn.execute(
        """
        INSERT INTO items (source, item_id, session_id, turn_id, ord, created_at,
                           item_type, role, phase, text, text_len, truncated,
                           content_hash, raw_json)
        VALUES (:source, :item_id, :session_id, :turn_id, :ord, :created_at,
                :item_type, :role, :phase, :text, :text_len, :truncated,
                :content_hash, :raw_json)
        ON CONFLICT(source, item_id) DO NOTHING
        """,
        row,
    )
    return cur.rowcount > 0


def insert_many_items(conn, rows: Iterable[dict]) -> int:
    n = 0
    for r in rows:
        if insert_item(conn, r):
            n += 1
    return n


def set_cursor(conn, source: str, watermark: str) -> None:
    conn.execute(
        """INSERT INTO ingest_cursor(source, watermark, updated_at) VALUES (?,?,strftime('%s','now'))
           ON CONFLICT(source) DO UPDATE SET watermark=excluded.watermark, updated_at=excluded.updated_at""",
        (source, watermark),
    )


def upsert_session(conn, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO sessions (session_id, source, title, cwd, repo, branch, model,
                              tokens_used, created_at, updated_at)
        VALUES (:session_id, :source, :title, :cwd, :repo, :branch, :model,
                :tokens_used, :created_at, :updated_at)
        ON CONFLICT(session_id) DO UPDATE SET
            title       = COALESCE(excluded.title, sessions.title),
            cwd         = COALESCE(excluded.cwd, sessions.cwd),
            repo        = COALESCE(excluded.repo, sessions.repo),
            branch      = COALESCE(excluded.branch, sessions.branch),
            model       = COALESCE(excluded.model, sessions.model),
            tokens_used = COALESCE(excluded.tokens_used, sessions.tokens_used),
            updated_at  = MAX(COALESCE(excluded.updated_at, 0), COALESCE(sessions.updated_at, 0))
        """,
        row,
    )


def insert_turn(conn, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO turns (session_id, turn_id, status, started_at, completed_at, duration_ms, ordinal)
        VALUES (:session_id, :turn_id, :status, :started_at, :completed_at, :duration_ms, :ordinal)
        ON CONFLICT(session_id, turn_id) DO UPDATE SET
            status       = COALESCE(excluded.status, turns.status),
            duration_ms  = COALESCE(excluded.duration_ms, turns.duration_ms),
            completed_at = COALESCE(excluded.completed_at, turns.completed_at)
        """,
        row,
    )


def add_mention(conn, entity_id: str, source: str, item_id: str, n: int) -> None:
    conn.execute(
        """INSERT INTO mentions (entity_id, source, item_id, n) VALUES (?,?,?,?)
           ON CONFLICT(entity_id, source, item_id) DO UPDATE SET n = n + excluded.n""",
        (entity_id, source, item_id, n),
    )


def catalog_file(conn, path: str, source: str, size: int, mtime: int,
                 parsed: int = 0, note: str | None = None) -> None:
    conn.execute(
        """INSERT INTO source_files (path, source, size, mtime, parsed, note) VALUES (?,?,?,?,?,?)
           ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime,
                                           parsed=excluded.parsed, note=excluded.note""",
        (path, source, size, mtime, parsed, note),
    )


def recompute_session_counts(conn) -> None:
    """Fill sessions.item_count / turn_count and entities.session_count."""
    conn.execute(
        """UPDATE sessions SET
             item_count = (SELECT COUNT(*) FROM items  WHERE items.session_id = sessions.session_id),
             turn_count = (SELECT COUNT(*) FROM turns  WHERE turns.session_id = sessions.session_id)"""
    )
    conn.execute(
        """UPDATE entities SET session_count = (
             SELECT COUNT(DISTINCT i.session_id) FROM mentions m
             JOIN items i ON i.source = m.source AND i.item_id = m.item_id
             WHERE m.entity_id = entities.entity_id)"""
    )


def start_run(conn, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO ingest_runs (source, started_at) VALUES (?, strftime('%s','now'))", (source,)
    )
    return int(cur.lastrowid)


def end_run(conn, run_id: int, n_sessions: int, n_items: int, note: str = "") -> None:
    conn.execute(
        """UPDATE ingest_runs SET ended_at=strftime('%s','now'), n_sessions=?, n_items=?, note=?
           WHERE id=?""",
        (n_sessions, n_items, note[:500], run_id),
    )
