"""Export the wiki database to a single `wiki.json` consumed by the web UI.

This is the *seam* between the core and any front end.  Keeping it a file (not
a Python import) means the UI can be rewritten in any language, and the core
never grows a dependency on how the data is displayed.

Design notes
------------
* The export is deliberately capped.  A 227 MB database cannot become a 227 MB
  JSON payload, and it should not: a UI needs the *shape* of the terrain plus
  enough provenance to click through, not every byte.  Caps are parameters.
* Provenance is the point.  Every snippet carries `session_id` + `item_id`, so
  the UI can always answer "where did this come from".
* Backlinks are computed client-side from `session.entity_ids`.  Shipping the
  session->entity relation (instead of a precomputed, larger adjacency list)
  lets the front end derive co-occurrence, per-repo entity lists and the graph
  without another round trip.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from . import config

EXPORT_VERSION = 1

# Top entities get snippets; the rest are listed by count only.
DEFAULT_MAX_ENTITIES = 400
DEFAULT_SNIPPETS = 3
DEFAULT_SNIPPET_LEN = 220
MAX_ENTITIES_PER_SESSION = 40
MAX_SESSIONS_PER_ENTITY = 60

# Entity kinds that are noise in a browsing UI: every codebase has thousands of
# file names, and showing them dilutes the repos/concepts that matter.
KIND_ORDER = ["repo", "path", "error", "pr", "issue", "pkg", "domain", "file"]

# Guaranteed slots per kind, before the remaining budget goes to breadth.
#
# Ranking alone cannot fix this.  Measured on a real corpus: ordering by breadth
# still left 80.5% of the export as file names and paths, because there are 4107
# file entities and only 55 errors -- population, not ordering, decides the mix.
# Worse, raw-frequency ordering buried 54 of those 55 errors below a cut-off of
# 13 mentions, so the most actionable signal in the corpus was unreachable in the
# UI.  Floors make the composition a deliberate choice instead of an accident.
KIND_FLOOR = {
    "error": 60,     # 55 exist: show every one of them
    "repo": 45,
    "pr": 40,
    "issue": 35,
    "pkg": 35,       # 35 exist: all
    "domain": 35,
    "path": 40,
    "file": 60,
}


def _select_with_floors(pool, by_intensity, max_entities: int) -> list:
    """Give every kind its floor, then spend the rest on the deepest dives.

    `pool` must be sorted by *breadth* (how many sessions touch it); it decides
    who gets a floor slot.  `by_intensity` is the same rows sorted by raw mention
    count; it decides who gets the leftover slots.

    Two orderings, deliberately.  Breadth finds what recurs across your work
    (`github.com`, 65 sessions); intensity finds what you hammered on inside a
    handful of them (a 369-mention file touched in only 2 sessions).  Ranking by
    either one alone silently deletes the other kind of knowledge -- and because
    the export is capped, "excluded" means "unreachable in the UI", not merely
    "ranked lower".

    The floors are handed out *round-robin* (one slot per kind per round) rather
    than by walking the pool in order.  Walking the pool is order-dependent and
    quietly wrong whenever the cap is smaller than the sum of the floors: the
    highest-breadth kind fills the quota during pass 1 and returns before a rare
    kind -- an `error`, say, sitting at the bottom of the pool -- is ever
    reached.  Round-robin makes the guarantee hold for any `max_entities`.
    """
    by_kind: dict[str, list] = {}
    for r in pool:
        by_kind.setdefault(r["kind"], []).append(r)

    picked: list = []
    seen: set[str] = set()
    deepest = max(KIND_FLOOR.values()) if KIND_FLOOR else 0

    # Pass 1: round-robin up to each kind's floor.
    for depth in range(deepest):
        for kind, floor in KIND_FLOOR.items():
            if depth >= floor:
                continue
            bucket = by_kind.get(kind)
            if not bucket or depth >= len(bucket):
                continue
            r = bucket[depth]
            picked.append(r)
            seen.add(r["entity_id"])
            if len(picked) >= max_entities:
                return picked[:max_entities]

    # Pass 2: spend whatever is left on the most intensely-worked entities.
    for r in by_intensity:
        if len(picked) >= max_entities:
            break
        if r["entity_id"] not in seen:
            seen.add(r["entity_id"])
            picked.append(r)

    return picked[:max_entities]


def _iso(ms: int | None) -> str | None:
    if not ms:
        return None
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ms / 1000))
    except (OverflowError, OSError, ValueError):
        return None


def _pct(values: list[int], ps=(10, 25, 50, 75, 90, 95, 99)) -> dict[str, int]:
    if not values:
        return {}
    v = sorted(values)
    n = len(v)
    return {f"p{p}": v[min(n - 1, max(0, round(p / 100 * (n - 1))))] for p in ps}


def _snippet(text: str, needle: str, width: int) -> str | None:
    """A window of `text` centred on `needle`, with ellipses when clipped."""
    if not text or not needle:
        return None
    i = text.lower().find(needle.lower())
    if i < 0:
        return None
    half = max(20, (width - len(needle)) // 2)
    start = max(0, i - half)
    end = min(len(text), i + len(needle) + half)
    out = text[start:end].replace("\r", "").replace("\n", " ⏎ ")
    if start > 0:
        out = "…" + out
    if end < len(text):
        out = out + "…"
    return out


def build(
    conn,
    *,
    max_entities: int = DEFAULT_MAX_ENTITIES,
    snippets: int = DEFAULT_SNIPPETS,
    snippet_len: int = DEFAULT_SNIPPET_LEN,
) -> dict[str, Any]:
    q = lambda sql, args=(): conn.execute(sql, args).fetchall()  # noqa: E731

    # ---------------------------------------------------------------- stats
    tot = q(
        """SELECT (SELECT COUNT(*) FROM sessions) sessions,
                  (SELECT COUNT(*) FROM turns)    turns,
                  (SELECT COUNT(*) FROM items)    items,
                  (SELECT COALESCE(SUM(text_len),0) FROM items) chars,
                  (SELECT COUNT(*) FROM entities) entities,
                  (SELECT COUNT(*) FROM mentions) mentions"""
    )[0]

    stats = {
        "sessions": tot["sessions"],
        "turns": tot["turns"],
        "items": tot["items"],
        "chars": tot["chars"],
        "entities": tot["entities"],
        "mentions": tot["mentions"],
    }

    sources = [
        dict(r)
        for r in q(
            """SELECT source, COUNT(DISTINCT session_id) sessions,
                      COUNT(*) items, SUM(text_len) chars
               FROM items GROUP BY source ORDER BY items DESC"""
        )
    ]
    item_types = [
        dict(r)
        for r in q(
            """SELECT source, item_type, COUNT(*) n, SUM(text_len) chars
               FROM items GROUP BY source, item_type ORDER BY n DESC"""
        )
    ]
    timeline = [
        dict(r)
        for r in q(
            """SELECT strftime('%Y-%m', created_at/1000, 'unixepoch') month,
                      COUNT(*) items, COUNT(DISTINCT session_id) sessions
               FROM items WHERE created_at IS NOT NULL
               GROUP BY month ORDER BY month"""
        )
        if r["month"]
    ]

    durs = [r["duration_ms"] for r in q(
        "SELECT duration_ms FROM turns WHERE duration_ms > 0")]
    durations = {"n": len(durs), **_pct(durs)}

    # ---------------------------------------------------------------- repos
    repos = [
        dict(r)
        for r in q(
            """SELECT COALESCE(repo,'(unknown)') repo,
                      COUNT(*) sessions, COALESCE(SUM(tokens_used),0) tokens,
                      MIN(created_at) first_ms, MAX(updated_at) last_ms
               FROM sessions GROUP BY repo ORDER BY sessions DESC"""
        )
    ]
    for r in repos:
        r["first"] = _iso(r.pop("first_ms"))
        r["last"] = _iso(r.pop("last_ms"))
    # A stable colour/index per repo, used by the UI for the terrain map.
    repo_index = {r["repo"]: i for i, r in enumerate(repos)}

    # ---------------------------------------------------------------- sessions
    sessions = []
    for r in q(
        """SELECT session_id, source, title, cwd, repo, branch, model,
                  tokens_used, created_at, updated_at
           FROM sessions ORDER BY COALESCE(updated_at,0) DESC"""
    ):
        sessions.append(
            {
                "id": r["session_id"],
                "source": r["source"],
                "title": (r["title"] or "")[:200] or None,
                "cwd": r["cwd"],
                "repo": r["repo"],
                "branch": r["branch"],
                "model": r["model"],
                "tokens": r["tokens_used"] or 0,
                "created": _iso(r["created_at"]),
                "items": 0,
                "turns": 0,
                "entity_ids": [],
            }
        )
    by_id = {s["id"]: s for s in sessions}

    counts = {r["session_id"]: r for r in q(
        """SELECT session_id, COUNT(*) items FROM items GROUP BY session_id""")}
    for sid, r in counts.items():
        if sid in by_id:
            by_id[sid]["items"] = r["items"]
    for r in q("""SELECT session_id, COUNT(*) turns FROM turns GROUP BY session_id"""):
        if r["session_id"] in by_id:
            by_id[r["session_id"]]["turns"] = r["turns"]

    # session -> entities it touches (the client derives backlinks from this)
    for r in q(
        """SELECT i.session_id sid, m.entity_id eid, SUM(m.n) n
           FROM mentions m JOIN items i
             ON i.source = m.source AND i.item_id = m.item_id
           GROUP BY sid, eid ORDER BY sid, n DESC"""
    ):
        s = by_id.get(r["sid"])
        if s is None:
            continue
        if len(s["entity_ids"]) < MAX_ENTITIES_PER_SESSION:
            s["entity_ids"].append(r["eid"])

    # ---------------------------------------------------------------- entities
    # Order by *breadth* (how many distinct sessions touch it), not raw mention
    # count.  A file name is mentioned every time a tool call touches it, so it
    # accumulates huge counts inside one or two sessions (`d:/kaiwu_...` had 1767
    # mentions across 2 sessions) while something genuinely central (`github.com`)
    # spreads across 65.  Breadth is the honest signal and it is already measured.
    pool = q(
        """SELECT entity_id, kind, canonical, first_seen, last_seen,
                  mention_count, session_count
           FROM entities
           WHERE kind IN ('repo','path','error','pr','issue','pkg','domain','file')
           ORDER BY session_count DESC, mention_count DESC"""
    )
    # Same rows, ordered by the other signal, for the leftover slots.
    by_intensity = sorted(
        pool, key=lambda r: (-r["mention_count"], -r["session_count"])
    )
    rows = _select_with_floors(pool, by_intensity, max_entities)

    entities = []
    for r in rows:
        eid = r["entity_id"]
        name = r["canonical"]
        ent = {
            "id": eid,
            "kind": r["kind"],
            "name": name,
            "mentions": r["mention_count"],
            "sessions": r["session_count"],
            "first": _iso(r["first_seen"]),
            "last": _iso(r["last_seen"]),
            "session_ids": [],
            "snippets": [],
        }
        for s in q(
            """SELECT DISTINCT i.session_id sid
               FROM mentions m JOIN items i
                 ON i.source = m.source AND i.item_id = m.item_id
               WHERE m.entity_id = ? LIMIT ?""",
            (eid, MAX_SESSIONS_PER_ENTITY),
        ):
            ent["session_ids"].append(s["sid"])

        if snippets:
            for s in q(
                """SELECT i.item_id iid, i.session_id sid, i.source src,
                          i.created_at ts, i.text txt
                   FROM mentions m JOIN items i
                     ON i.source = m.source AND i.item_id = m.item_id
                   WHERE m.entity_id = ? AND length(i.text) > 0
                   ORDER BY m.n DESC LIMIT ?""",
                (eid, snippets * 4),
            ):
                snip = _snippet(s["txt"], name, snippet_len)
                if not snip:
                    continue
                ent["snippets"].append(
                    {
                        "session_id": s["sid"],
                        "item_id": s["iid"],
                        "source": s["src"],
                        "date": _iso(s["ts"]),
                        "text": snip,
                    }
                )
                if len(ent["snippets"]) >= snippets:
                    break
        entities.append(ent)

    # Referential integrity.  `entities` is capped at `max_entities`, but the
    # session -> entity relation was gathered over *all* entities.  Left alone
    # that produces thousands of dangling ids, and the UI would render links to
    # pages that do not exist.  Keep only ids present in this export.
    exported = {e["id"] for e in entities}
    for s in sessions:
        if s["entity_ids"]:
            s["entity_ids"] = [eid for eid in s["entity_ids"] if eid in exported]

    return {
        "meta": {
            "export_version": EXPORT_VERSION,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "db": str(config.DB_PATH),
            "caps": {
                "max_entities": max_entities,
                "snippets_per_entity": snippets,
                "snippet_len": snippet_len,
                "entities_per_session": MAX_ENTITIES_PER_SESSION,
            },
        },
        "stats": stats,
        "sources": sources,
        "item_types": item_types,
        "timeline": timeline,
        "durations": durations,
        "kind_order": KIND_ORDER,
        "repos": repos,
        "repo_index": repo_index,
        "entities": entities,
        "sessions": sessions,
    }


def write(conn, path=None, **kw) -> dict[str, Any]:
    path = path or config.WEB_DIR / "wiki.json"
    path = type(path)(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = build(conn, **kw)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "entities": len(data["entities"]),
        "sessions": len(data["sessions"]),
    }
