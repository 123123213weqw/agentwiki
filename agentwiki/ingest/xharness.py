"""Ingest xharness (this harness) session logs.

Format: one JSON object per line, `{"record": ...}`.

  {"record":"header", "format":"xharness.session.jsonl", "format_version":1,
   "header":{"version":1,"id":"session-...","created_at_ms":...,"cwd":"..."}}

  {"record":"batch", "previous_revision":N, "revision":M,
   "events":[{"seq":5,"revision":2,"timestamp_ms":...,"event":{"type":"...","data":{...}}}]}

Event types worth keeping (measured on this machine):
  user/message       78      -> data.message.{id,role,content,reasoning}
  assistant/message  1391    -> data.{turn,step,message:{content,reasoning,tool_calls},usage}
  tool/call          1603    -> tool invocation
  turn/start,end     68/64   -> turn boundaries (duration source)
  session/title      27      -> title
  compaction/summary 10      -> context was compacted here (mark it!)

Deliberately skipped: assistant/chunk (25556 streaming deltas -- pure noise).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

from .. import config, db, extract, redact

SOURCE = "xharness"

KEEP_EVENTS = {
    "user/message",
    "assistant/message",
    "tool/call",
    "tool/result",
    "session/title",
    "turn/start",
    "turn/end",
    "compaction/summary",
    "question/requested",
    "question/resolved",
}


def _iter_session_files() -> Iterator[Path]:
    root = config.XHARNESS_SESSIONS
    if not root or not root.is_dir():
        return
    for p in sorted(root.glob("*.jsonl")):
        if config.is_forbidden(p):
            continue
        yield p


def _tool_calls_text(msg: dict) -> str:
    out = []
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        name = tc.get("name") or "?"
        args = tc.get("arguments_json") or ""
        try:
            a = json.loads(args) if args else {}
        except Exception:
            a = {}
        hint = ""
        for key in ("command", "file_path", "path", "pattern", "url", "query", "description"):
            if isinstance(a, dict) and a.get(key):
                hint = str(a[key]).replace("\n", " ")[:300]
                break
        if not hint and isinstance(a, dict) and a.get("message"):
            hint = str(a["message"]).replace("\n", " ")[:300]
        out.append(f"[tool {name}] {hint}".rstrip())
    return "\n".join(out)


def _event_text(etype: str, data: dict) -> str:
    if etype in ("user/message", "assistant/message"):
        msg = data.get("message") or {}
        parts = []
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            parts.append(content)
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    parts.append(blk.get("text") or "")
                elif isinstance(blk, str):
                    parts.append(blk)
        tc = _tool_calls_text(msg)
        if tc:
            parts.append(tc)
        return "\n".join(x for x in parts if x)

    if etype == "tool/call":
        name = data.get("name") or (data.get("call") or {}).get("name") or "?"
        args = data.get("arguments") or data.get("arguments_json") or ""
        return f"[tool {name}] {str(args)[:400]}"

    if etype == "tool/result":
        name = data.get("name") or "?"
        status = data.get("status") or ""
        return f"[tool result {name} {status}]"

    if etype == "session/title":
        return f"title: {data.get('title') or ''}"

    if etype == "turn/start":
        return f"turn {data.get('turn')} start"

    if etype == "turn/end":
        reason = data.get("reason") or {}
        return f"turn {data.get('turn')} end ({reason.get('kind') or '?'})"

    if etype == "compaction/summary":
        return "[context compacted here]"

    if etype.startswith("question/"):
        return f"{etype}: {str(data)[:300]}"

    return ""


def ingest(conn, verbose: bool = True) -> dict:
    files = list(_iter_session_files())
    if not files:
        return {"source": SOURCE, "sessions": 0, "items": 0, "note": "no xharness session files"}

    run_id = db.start_run(conn, SOURCE)
    n_sessions = 0
    n_items = 0
    n_new = 0

    for path in files:
        try:
            st = path.stat()
        except OSError:
            continue
        db.catalog_file(conn, str(path), SOURCE, st.st_size, int(st.st_mtime), parsed=1)

        header = None
        title = None
        session_id = path.stem
        # turn durations: turn number -> start ts
        turn_start: dict[int, int] = {}
        events_buf: list[dict] = []
        ord_ = 0

        try:
            fh = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue

        with fh:
            for raw_line in fh:
                if not raw_line or len(raw_line) > config.MAX_JSONL_LINE:
                    continue
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue

                if rec.get("record") == "header":
                    header = rec.get("header") or {}
                    session_id = header.get("id") or session_id
                    continue

                for ev in rec.get("events") or []:
                    if not isinstance(ev, dict):
                        continue
                    e = ev.get("event") or {}
                    etype = e.get("type") or ""
                    if etype not in KEEP_EVENTS:
                        continue
                    data = e.get("data") or {}
                    if not isinstance(data, dict):
                        continue
                    ts = ev.get("timestamp_ms")
                    seq = ev.get("seq")
                    iid = f"{session_id}:{seq}"

                    if etype == "session/title":
                        title = data.get("title") or title
                        continue

                    if etype == "turn/start":
                        t = data.get("turn")
                        if isinstance(t, int) and ts:
                            turn_start[t] = ts
                        continue

                    if etype == "turn/end":
                        t = data.get("turn")
                        if isinstance(t, int) and ts and t in turn_start:
                            db.insert_turn(
                                conn,
                                dict(
                                    session_id=session_id,
                                    turn_id=str(t),
                                    status=(data.get("reason") or {}).get("kind"),
                                    started_at=turn_start[t],
                                    completed_at=ts,
                                    duration_ms=ts - turn_start[t],
                                    ordinal=t,
                                ),
                            )
                        continue

                    text = _event_text(etype, data)
                    if not text.strip():
                        continue
                    role = (data.get("message") or {}).get("role")
                    ord_ += 1
                    ok = db.insert_item(
                        conn,
                        _mkrow(
                            item_id=iid,
                            session_id=session_id,
                            turn_id=str(data.get("turn")) if data.get("turn") is not None else None,
                            ord_=ord_,
                            created_at=ts if ts else None,
                            item_type=etype,
                            role=role,
                            phase=("final_answer" if etype == "assistant/message" else None),
                            text=text,
                            raw=json.dumps(data, ensure_ascii=False),
                        ),
                    )
                    if ok:
                        n_new += 1
                        _mentions(conn, iid, text, ts if ts else None)
                    n_items += 1
                    if len(events_buf) >= 500:
                        conn.commit()
                        events_buf.clear()

        created = (header or {}).get("created_at_ms")
        cwd = (header or {}).get("cwd")
        db.upsert_session(
            conn,
            dict(
                session_id=session_id,
                source=SOURCE,
                title=title,
                cwd=cwd,
                repo=extract.repo_from_cwd(cwd),
                branch=None,
                model=None,
                tokens_used=None,
                created_at=created if created else None,
                updated_at=int(path.stat().st_mtime * 1000),
            ),
        )
        n_sessions += 1
        conn.commit()

    db.recompute_session_counts(conn)
    db.set_cursor(conn, SOURCE, str(max((p.stat().st_mtime for p in files), default=0)))
    conn.commit()
    db.end_run(conn, run_id, n_sessions, n_new, f"items_seen={n_items}")
    conn.commit()
    return {"source": SOURCE, "sessions": n_sessions, "items": n_items, "new": n_new}


def _mkrow(item_id, session_id, turn_id, ord_, created_at, item_type, role, phase, text, raw):
    text = redact.scrub(text or "")
    truncated = 0
    if len(text) > config.MAX_ITEM_TEXT:
        text = text[: config.MAX_ITEM_TEXT]
        truncated = 1
    raw = (raw or "")[: config.MAX_ITEM_TEXT]
    return dict(
        source=SOURCE,
        item_id=item_id,
        session_id=session_id,
        turn_id=turn_id,
        ord=ord_,
        created_at=created_at,
        item_type=item_type,
        role=role,
        phase=phase,
        text=text,
        text_len=len(text),
        truncated=truncated,
        content_hash=db.sha(text),
        raw_json=raw,
    )


def _mentions(conn, item_id: str, text: str, ts) -> None:
    for eid, kind, canonical, n in extract.iter_entities(text):
        db.upsert_entity(conn, eid, kind, canonical, ts)
        db.add_mention(conn, eid, SOURCE, item_id, n)
