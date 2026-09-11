"""Ingest Codex session data -- the primary and cleanest source.

Two sqlite databases are SNAPSHOTTED (copied) before being opened, so we never
touch a live db that codex is writing to:

    state_5.sqlite           -> threads                      (the spine)
    thread_history_1.sqlite  -> thread_turns / thread_items   (the content)

`threads` gives title / cwd / repo / branch / model, `thread_turns` gives
duration_ms (this is what makes the "how long did I wait" analysis possible),
and `thread_items` gives the actual content as JSON.
"""

from __future__ import annotations

import json
import re
from typing import Iterator

from .. import config, db, extract, redact

SOURCE = "codex"

_IDE_MARKERS = ("# My request for Codex:", "## My request for Codex:", "My request for Codex:")

# item types we keep. `assistant/chunk`-style streaming noise has no analogue
# here, but `reasoning` is mostly empty summaries and pure cost, so it is
# dropped unless it actually carries a summary.
KEEP_TYPES = {
    "userMessage",
    "agentMessage",
    "fileChange",
    "commandExecution",
    "webSearch",
    "mcpToolCall",
    "subAgentActivity",
    "contextCompaction",
}


def _strip_ide_boilerplate(t: str) -> str:
    """Codex prefixes user messages with a big IDE-context blob.

    The real request lives after the last '## My request for Codex:' marker.
    """
    for m in _IDE_MARKERS:
        i = t.find(m)
        if i >= 0:
            return t[i + len(m):].strip()
    return t.strip()


def _text_user(d: dict) -> str:
    parts = []
    for blk in d.get("content") or []:
        if isinstance(blk, dict) and blk.get("type") == "text":
            parts.append(blk.get("text") or "")
    return _strip_ide_boilerplate("\n".join(parts))


def _text_agent(d: dict) -> str:
    return (d.get("text") or "").strip()


def _text_filechange(d: dict) -> str:
    out = []
    for ch in d.get("changes") or []:
        if not isinstance(ch, dict):
            continue
        path = ch.get("path") or ""
        kind = (ch.get("kind") or {}).get("type") or "update"
        out.append(f"[{kind}] {path}")
        diff = ch.get("diff") or ""
        if diff:
            out.append(diff[:4000])
    return "\n".join(out)


def _text_command(d: dict) -> str:
    return (d.get("command") or "").strip()


def _text_websearch(d: dict) -> str:
    out = [f"query: {d.get('query') or ''}"]
    act = d.get("action") or {}
    if isinstance(act, dict) and act.get("url"):
        out.append(f"url: {act['url']}")
    for r in (d.get("results") or [])[:5]:
        if isinstance(r, dict) and r.get("title"):
            out.append(f"- {r.get('title')} {r.get('url') or ''}")
    return "\n".join(out)


def _text_mcp(d: dict) -> str:
    return f"{d.get('server')}/{d.get('tool')} status={d.get('status')}"


def _text_subagent(d: dict) -> str:
    return f"subagent kind={d.get('kind')} path={d.get('agentPath')}"


def _text_reasoning(d: dict) -> str:
    s = d.get("summary") or []
    if isinstance(s, list):
        return "\n".join(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in s)
    return str(s)


_EXTRACTORS = {
    "userMessage": _text_user,
    "agentMessage": _text_agent,
    "fileChange": _text_filechange,
    "commandExecution": _text_command,
    "webSearch": _text_websearch,
    "mcpToolCall": _text_mcp,
    "subAgentActivity": _text_subagent,
    "reasoning": _text_reasoning,
    "contextCompaction": lambda d: "[context compaction -- history was truncated here]",
}


def _norm_repo(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"[/:]([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?/?$", url)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return url


def _row(
    item_id: str,
    session_id: str,
    turn_id: str | None,
    ord_: int,
    created_at: int | None,
    item_type: str,
    role: str | None,
    phase: str | None,
    text: str,
    raw: str | None,
) -> dict:
    text = text or ""
    truncated = 0
    if len(text) > config.MAX_ITEM_TEXT:
        text = text[: config.MAX_ITEM_TEXT]
        truncated = 1
    text = redact.scrub(text)
    if raw and len(raw) > config.MAX_ITEM_TEXT:
        raw = raw[: config.MAX_ITEM_TEXT]
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


def _iter_item_rows(hist, thread_id: str) -> Iterator[dict]:
    q = """SELECT item_id, turn_id, rollout_ordinal, created_at_ms, item_type, item_json
           FROM thread_items WHERE thread_id = ? ORDER BY rollout_ordinal"""
    for r in hist.execute(q, (thread_id,)):
        itype = r["item_type"]
        raw = r["item_json"]
        if itype not in _EXTRACTORS:
            if itype not in KEEP_TYPES:
                continue
        try:
            d = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            continue
        fn = _EXTRACTORS.get(itype)
        text = fn(d) if fn else ""
        role = "user" if itype == "userMessage" else ("assistant" if itype == "agentMessage" else None)
        yield _row(
            item_id=r["item_id"],
            session_id=thread_id,
            turn_id=r["turn_id"],
            ord_=r["rollout_ordinal"] or 0,
            created_at=r["created_at_ms"],
            item_type=itype,
            role=role,
            phase=d.get("phase"),
            text=text,
            raw=raw,
        )


def ingest(conn, limit_threads: int | None = None, verbose: bool = True) -> dict:
    cfg = config
    if not cfg.CODEX_HISTORY.exists():
        raise FileNotFoundError(f"codex history db not found: {cfg.CODEX_HISTORY}")

    run_id = db.start_run(conn, SOURCE)
    hist = db.open_snapshot(cfg.CODEX_HISTORY)

    thread_meta: dict[str, dict] = {}
    if cfg.CODEX_STATE.exists():
        st = db.open_snapshot(cfg.CODEX_STATE)
        for r in st.execute(
            """SELECT id, title, cwd, model, git_origin_url, git_branch,
                      created_at_ms, updated_at_ms, tokens_used
               FROM threads"""
        ):
            thread_meta[r["id"]] = dict(r)
        st.close()

    # Only threads that actually have content.
    ids = [
        r[0]
        for r in hist.execute(
            "SELECT thread_id, COUNT(*) c FROM thread_items GROUP BY thread_id ORDER BY c DESC"
        )
    ]
    if limit_threads:
        ids = ids[:limit_threads]

    n_items = 0
    n_sessions = 0
    for tid in ids:
        meta = thread_meta.get(tid, {})
        db.upsert_session(
            conn,
            dict(
                session_id=tid,
                source=SOURCE,
                title=(meta.get("title") or "")[:400] or None,
                cwd=meta.get("cwd"),
                repo=_norm_repo(meta.get("git_origin_url"))
                or extract.repo_from_cwd(meta.get("cwd")),
                branch=meta.get("git_branch"),
                model=meta.get("model"),
                tokens_used=meta.get("tokens_used"),
                created_at=meta.get("created_at_ms"),
                updated_at=meta.get("updated_at_ms"),
            ),
        )
        n_sessions += 1

        for r in hist.execute(
            """SELECT turn_id, status, started_at, completed_at, duration_ms, rollout_ordinal
               FROM thread_turns WHERE thread_id = ? ORDER BY rollout_ordinal""",
            (tid,),
        ):
            db.insert_turn(
                conn,
                dict(
                    session_id=tid,
                    turn_id=r["turn_id"],
                    status=r["status"],
                    started_at=(r["started_at"] * 1000) if r["started_at"] else None,
                    completed_at=(r["completed_at"] * 1000) if r["completed_at"] else None,
                    duration_ms=r["duration_ms"],
                    ordinal=r["rollout_ordinal"] or 0,
                ),
            )

        for row in _iter_item_rows(hist, tid):
            if not db.insert_item(conn, row):
                continue  # already ingested; do NOT recount mentions
            n_items += 1
            if row["text"]:
                for eid, kind, display, count in extract.iter_entities(row["text"]):
                    db.upsert_entity(conn, eid, kind, display, row["created_at"])
                    db.add_mention(conn, eid, SOURCE, row["item_id"], count)

        if verbose and n_sessions % 25 == 0:
            print(f"  codex: {n_sessions} sessions, {n_items} new items", flush=True)

    hist.close()
    db.recompute_session_counts(conn)
    db.set_cursor(conn, SOURCE, str(max((thread_meta.get(t, {}).get("updated_at_ms") or 0) for t in ids) if ids else 0))
    db.end_run(conn, run_id, n_sessions, n_items, f"limit_threads={limit_threads}")
    conn.commit()
    return dict(sessions=n_sessions, items=n_items)
