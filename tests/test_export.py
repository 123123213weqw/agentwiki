"""Contract tests for `wiki.json` -- the seam between the core and the web UI.

These are hermetic: a throwaway in-memory database is seeded with a handful of
rows, so the tests do not depend on the user's real 227 MB wiki.

The first test is a regression test.  `build()` gathers the session -> entity
relation over *all* entities but only exports the top `max_entities` of them.
The first version shipped 1,922 dangling ids that way, and the UI rendered links
to entity pages that did not exist.  The fixture therefore caps entities at 2
while mentioning 5, which is the shape that used to break.
"""

import sqlite3

from agentwiki import db, export

SEED_TEXT = "打开 preprocessor.py 看了一下，然后又看了 model.py 和 trainer.py"


def _mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    return conn


def _seed(conn: sqlite3.Connection) -> None:
    for i, sid in enumerate(("s1", "s2", "s3"), start=1):
        conn.execute(
            """INSERT INTO sessions
               (session_id, source, title, cwd, repo, branch, model,
                tokens_used, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (sid, "codex", f"title {sid}", "D:/proj", "me/proj", "main",
             "gpt-5", 100 * i, 1700000000000 + i, 1700000000000 + i),
        )
        # Two items per session, both carrying the same text so any entity
        # mentioned in SEED_TEXT gets a provable snippet.
        for k, itype in enumerate(("userMessage", "agentMessage")):
            conn.execute(
                """INSERT INTO items
                   (source, item_id, session_id, turn_id, ord, created_at,
                    item_type, role, phase, text, text_len, truncated,
                    content_hash, raw_json)
                   VALUES ('codex',?,?,?,?,?,?,?,?,?,?,0,?,NULL)""",
                (f"{sid}-i{k}", sid, f"{sid}-t1", k, 1700000000000 + i,
                 itype, "user" if k == 0 else "assistant", "final_answer",
                 SEED_TEXT, len(SEED_TEXT), f"h-{sid}-{k}"),
            )
        conn.execute(
            """INSERT INTO turns
               (session_id, turn_id, status, started_at, completed_at,
                duration_ms, ordinal)
               VALUES (?,?,'completed',?,?,?,1)""",
            (sid, f"{sid}-t1", 1700000000000, 1700000006000, 6000),
        )

    # Five entities, all of allowed kinds, with descending mention counts so the
    # ORDER BY / LIMIT picks a deterministic pair.
    for n, (eid, kind, name) in enumerate([
        ("file:preprocessor.py", "file", "preprocessor.py"),
        ("file:model.py", "file", "model.py"),
        ("file:trainer.py", "file", "trainer.py"),
        ("path:d/proj", "path", "d/proj"),
        ("repo:me/proj", "repo", "me/proj"),
    ], start=1):
        conn.execute(
            """INSERT INTO entities
               (entity_id, kind, canonical, first_seen, last_seen,
                mention_count, session_count)
               VALUES (?,?,?,?,?,?,?)""",
            (eid, kind, name, 1700000000000, 1700000000000, 100 - n, 3),
        )
        # Every entity is mentioned in every session, so the un-pruned relation
        # would reference all five from each session.
        for sid in ("s1", "s2", "s3"):
            for k in (0, 1):
                conn.execute(
                    """INSERT INTO mentions (entity_id, item_id, source, n)
                       VALUES (?,?,'codex',1)""",
                    (eid, f"{sid}-i{k}"),
                )
    conn.commit()


def test_no_dangling_entity_references():
    """Regression: capping entities must not leave dangling session->entity ids."""
    conn = _mem()
    _seed(conn)
    data = export.build(conn, max_entities=2, snippets=2)

    assert len(data["entities"]) == 2, "cap not applied"
    exported = {e["id"] for e in data["entities"]}

    for s in data["sessions"]:
        for eid in s["entity_ids"]:
            assert eid in exported, f"session {s['id']} points at missing {eid}"

    for e in data["entities"]:
        for sid in e["session_ids"]:
            assert any(s["id"] == sid for s in data["sessions"])


def test_contract_keys_present():
    """The UI reads these exact paths; a rename here breaks the page silently."""
    conn = _mem()
    _seed(conn)
    data = export.build(conn, max_entities=5)

    for key in ("meta", "stats", "sources", "item_types", "timeline",
                "durations", "kind_order", "repos", "repo_index",
                "entities", "sessions"):
        assert key in data, f"missing top-level key: {key}"

    assert data["meta"]["caps"]["max_entities"] == 5
    assert data["meta"]["export_version"] == export.EXPORT_VERSION
    for key in ("sessions", "turns", "items", "chars", "entities", "mentions"):
        assert isinstance(data["stats"][key], int)
    assert data["durations"]["p50"] > 0
    assert data["timeline"] and data["timeline"][0]["month"]


def test_snippets_carry_provenance():
    """Provenance is the reason this is a wiki and not a pile of vectors."""
    conn = _mem()
    _seed(conn)
    data = export.build(conn, max_entities=5, snippets=2)

    ent = next(e for e in data["entities"] if e["id"] == "file:preprocessor.py")
    assert ent["snippets"], "expected at least one snippet"
    for snip in ent["snippets"]:
        assert snip["session_id"], "snippet without a session cannot be traced"
        assert snip["item_id"], "snippet without an item_id cannot be traced"
        assert "preprocessor.py" in snip["text"]
        assert snip["source"] == "codex"


def test_kind_filter_excludes_unknown_kinds():
    """Only browseable kinds are exported; a junk kind must not leak in."""
    conn = _mem()
    _seed(conn)
    conn.execute(
        """INSERT INTO entities (entity_id, kind, canonical, first_seen,
                                 last_seen, mention_count, session_count)
           VALUES ('noise:x','nonexistent_kind','x',1,1,9999,1)"""
    )
    conn.commit()

    data = export.build(conn, max_entities=10)
    assert "noise:x" not in {e["id"] for e in data["entities"]}


def test_rare_but_meaningful_kind_survives_the_cap():
    """Regression: ranking by frequency alone buried almost every error.

    Measured on the real corpus, a pure `ORDER BY mention_count DESC LIMIT 400`
    exported 292 files, 70 paths and exactly **1** of 55 error entities: a file
    name is mentioned on every tool call that touches it, so frequency selects
    for the incidental.  The per-kind floors exist to make that impossible.

    Here 20 files out-rank one error on every frequency signal, and the error
    must still be exported.
    """
    conn = _mem()
    for i in range(20):
        conn.execute(
            """INSERT INTO entities (entity_id, kind, canonical, first_seen,
                                     last_seen, mention_count, session_count)
               VALUES (?, 'file', ?, 1, 1, ?, ?)""",
            (f"file:noisy{i}.py", f"noisy{i}.py", 5000 - i, 90),
        )
    # Last on every ranking: one mention, one session, one kind.
    conn.execute(
        """INSERT INTO entities (entity_id, kind, canonical, first_seen,
                                 last_seen, mention_count, session_count)
           VALUES ('error:RareButMeaningful', 'error', 'RareButMeaningful', 1, 1, 1, 1)"""
    )
    conn.commit()

    # Cap well below the 20 files + 1 error, so a pure top-N would drop it.
    data = export.build(conn, max_entities=6)
    kinds = {e["kind"] for e in data["entities"]}

    assert "error" in kinds, (
        "the only error entity was dropped: floors are not being applied"
    )
    assert "error:RareButMeaningful" in {e["id"] for e in data["entities"]}


def test_floors_never_exceed_the_cap():
    """Floors are a preference, not an override: the cap is still the cap."""
    conn = _mem()
    for kind in export.KIND_FLOOR:
        for i in range(80):
            conn.execute(
                """INSERT INTO entities (entity_id, kind, canonical, first_seen,
                                         last_seen, mention_count, session_count)
                   VALUES (?, ?, ?, 1, 1, ?, 1)""",
                (f"{kind}:e{i}", kind, f"{kind}-{i}", 100 - i),
            )
    conn.commit()

    for cap in (1, 7, 50, 400):
        data = export.build(conn, max_entities=cap)
        assert len(data["entities"]) <= cap, f"cap {cap} exceeded"


def test_ranking_prefers_breadth_over_raw_mentions():
    """A name smeared across many sessions should outrank one hammered inside a
    single session -- that is the whole reason for ordering by session_count."""
    conn = _mem()
    conn.execute(
        """INSERT INTO entities (entity_id, kind, canonical, first_seen,
                                 last_seen, mention_count, session_count)
           VALUES ('file:narrow.py','file','narrow.py',1,1,9999,2)"""
    )
    conn.execute(
        """INSERT INTO entities (entity_id, kind, canonical, first_seen,
                                 last_seen, mention_count, session_count)
           VALUES ('file:broad.py','file','broad.py',1,1,30,40)"""
    )
    conn.commit()

    data = export.build(conn, max_entities=1)
    assert data["entities"][0]["id"] == "file:broad.py"


def test_write_is_atomic_and_reports_shape(tmp_path):
    conn = _mem()
    _seed(conn)
    out = tmp_path / "nested" / "wiki.json"
    res = export.write(conn, out, max_entities=3, snippets=1)

    assert out.exists()
    assert res["entities"] == 3
    assert res["sessions"] == 3
    assert res["bytes"] > 0
    assert not out.with_suffix(".json.tmp").exists(), "temp file left behind"
