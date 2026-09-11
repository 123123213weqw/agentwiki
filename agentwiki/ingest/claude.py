"""Ingest Claude Code session transcripts (`~/.claude/projects/**/*.jsonl`).

Format: one JSON object per line, discriminated by `type`.  Only `user` and
`assistant` lines carry conversation; the rest (`mode`, `last-prompt`,
`file-history-snapshot`, `queue-operation`, ...) is UI bookkeeping and is
counted but not stored.

These lines carry `cwd` and `gitBranch`, which makes repo attribution easy.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .. import config, db, extract, redact

SOURCE = "claude"

KEEP_TYPES = {"user", "assistant"}


def _epoch_ms(ts) -> int | None:
    if not isinstance(ts, str):
        return None
    try:
        s = ts.replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except (ValueError, OSError):
        return None


def _text_of_message(message) -> str:
    """Claude stores content either as a plain string or as a block list."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for blk in content:
        if not isinstance(blk, dict):
            continue
        bt = blk.get("type")
        if bt == "text":
            t = blk.get("text")
            if t:
                out.append(t)
        elif bt == "tool_use":
            # keep the tool name: it is signal about *how* work was done
            out.append(f"[tool_use {blk.get('name')}]")
        elif bt == "thinking":
            continue  # noisy, low value at L0
        elif bt == "tool_result":
            continue  # bulky machine output
    return "\n".join(out)


def _iter_lines(path: Path) -> Iterator[tuple[int, dict]]:
    skipped = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if len(line) > config.MAX_JSONL_LINE:
                skipped += 1
                continue
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                skipped += 1
                continue
            if isinstance(d, dict):
                yield i, d
    if skipped:
        print(f"      ({path.name}: skipped {skipped} oversized/unparsable lines)")


def ingest(conn, verbose: bool = True, max_files: int | None = None) -> dict:
    root = config.CLAUDE_PROJECTS
    if not root or not root.exists():
        if verbose:
            print("  claude: no ~/.claude/projects, skipping")
        return {"sessions": 0, "items": 0, "files": 0}

    files = sorted(root.rglob("*.jsonl"))
    if max_files:
        files = files[:max_files]

    sessions: set[str] = set()
    n_items = 0
    n_new = 0

    for path in files:
        if config.is_forbidden(path):
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        db.catalog_file(conn, str(path), SOURCE, st.st_size, int(st.st_mtime), parsed=1)

        project = path.parent.name  # e.g. "D--agentuniverse"
        for idx, d in _iter_lines(path):
            if d.get("type") not in KEEP_TYPES:
                continue
            sid = d.get("sessionId") or path.stem
            text = _text_of_message(d.get("message"))
            if not text.strip():
                continue

            msg = d.get("message") or {}
            role = msg.get("role") or d.get("type")
            created = _epoch_ms(d.get("timestamp"))
            item_id = str(d.get("uuid") or f"{path.stem}:{idx}")

            if sid not in sessions:
                sessions.add(sid)
                db.upsert_session(conn, {
                    "session_id": sid,
                    "source": SOURCE,
                    "title": project,
                    "cwd": d.get("cwd"),
                    "repo": extract.repo_from_cwd(d.get("cwd")) or project,
                    "branch": d.get("gitBranch"),
                    "model": d.get("version"),
                    "tokens_used": None,
                    "created_at": created,
                    "updated_at": created,
                })

            row = _row(SOURCE, item_id, sid, None, idx, created,
                       d.get("type"), role, None, text,
                       json.dumps(d, ensure_ascii=False)[:config.MAX_ITEM_TEXT])
            n_items += 1
            if db.insert_item(conn, row):
                n_new += 1
                _collect_entities(conn, SOURCE, item_id, created, text)
            if n_items % 20000 == 0:
                conn.commit()

    conn.commit()
    if verbose:
        print(f"  claude: {len(files)} files, {len(sessions)} sessions, "
              f"{n_items} messages ({n_new} new)")
    return {"sessions": len(sessions), "items": n_items, "files": len(files)}


def _row(source, item_id, session_id, turn_id, ord_, created_at,
         item_type, role, phase, text, raw):
    text = text or ""
    truncated = 0
    if len(text) > config.MAX_ITEM_TEXT:
        text = text[: config.MAX_ITEM_TEXT]
        truncated = 1
    text = redact.scrub(text)
    return {
        "source": source, "item_id": item_id, "session_id": session_id,
        "turn_id": turn_id, "ord": ord_, "created_at": created_at,
        "item_type": item_type, "role": role, "phase": phase,
        "text": text, "text_len": len(text), "truncated": truncated,
        "content_hash": db.sha(text), "raw_json": raw,
    }


def _collect_entities(conn, source, item_id, ts, text) -> None:
    for eid, kind, canonical, n in extract.iter_entities(text):
        db.upsert_entity(conn, eid, kind, canonical, ts)
        db.add_mention(conn, eid, source, item_id, n)
