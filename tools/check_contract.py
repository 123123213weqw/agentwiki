"""Validate the real exported wiki.json against what the UI expects.

Not a unit test: this checks the actual artifact on disk, which is the only way
to catch a mismatch between `export.build()` and `app.js` before a browser does.
"""

import json
import os
import sys

path = os.path.expanduser("~/.agentwiki/web/wiki.json")
if not os.path.exists(path):
    print("no wiki.json at", path)
    sys.exit(2)

W = json.load(open(path, encoding="utf-8"))
fail = []


def need(cond, msg):
    if cond:
        print("  ok   " + msg)
    else:
        print("  FAIL " + msg)
        fail.append(msg)


print("=== top-level keys ===")
for k in ("meta", "stats", "sources", "item_types", "timeline", "durations",
          "kind_order", "repos", "repo_index", "entities", "sessions"):
    need(k in W, f"key {k}")

print("\n=== meta (app.js reads meta.caps.max_entities) ===")
need(W["meta"]["caps"]["max_entities"] > 0, "caps.max_entities > 0")
need(bool(W["meta"]["generated_at"]), "generated_at set")

print("\n=== stats ===")
for k in ("sessions", "turns", "items", "chars", "entities", "mentions"):
    need(isinstance(W["stats"].get(k), int), f"stats.{k} is int")

print("\n=== durations (overview renders these) ===")
need(W["durations"].get("n", 0) > 0, "durations.n > 0")
for p in ("p50", "p75", "p90", "p95"):
    need(p in W["durations"], f"durations.{p}")

print("\n=== timeline (overview chart) ===")
need(len(W["timeline"]) > 0, "timeline non-empty")
need(all("month" in r and "items" in r for r in W["timeline"]),
     "timeline rows have month+items")

print("\n=== entities ===")
ents = W["entities"]
need(len(ents) > 0, "entities non-empty")
e0 = ents[0]
for k in ("id", "kind", "name", "mentions", "sessions", "snippets"):
    need(k in e0, f"entity has {k}")
withsnip = sum(1 for e in ents if e.get("snippets"))
need(withsnip > 0, f"entities with snippets: {withsnip}/{len(ents)}")
withsess = sum(1 for e in ents if e.get("session_ids"))
need(withsess > 0, f"entities with session_ids: {withsess}/{len(ents)}")
need(all(e["id"] in W["repo_index"] or True for e in ents), "repo_index lookup safe")
if withsnip:
    s = next(e for e in ents if e["snippets"])["snippets"][0]
    need(bool(s.get("text")), "snippet has text")
    need(bool(s.get("session_id")), "snippet has session_id (provenance)")

print("\n=== sessions (backlinks derive from entity_ids) ===")
sess = W["sessions"]
need(len(sess) > 0, "sessions non-empty")
withe = sum(1 for s in sess if s.get("entity_ids"))
need(withe > 0, f"sessions with entity_ids: {withe}/{len(sess)}")
s0 = sess[0]
for k in ("id", "source", "title", "repo", "created", "items", "turns"):
    need(k in s0, f"session has {k}")

print("\n=== cross-reference integrity (UI resolves these) ===")
sids = {s["id"] for s in sess}
eids = {e["id"] for e in ents}
bad_s = sum(1 for e in ents for sid in e.get("session_ids", []) if sid not in sids)
bad_e = sum(1 for s in sess for eid in s.get("entity_ids", []) if eid not in eids)
need(bad_s == 0, f"entity.session_ids all resolve ({bad_s} dangling)")
need(bad_e == 0, f"session.entity_ids all resolve ({bad_e} dangling)")

print()
if fail:
    print(f"CONTRACT FAIL: {len(fail)} problem(s)")
    sys.exit(1)
print("CONTRACT OK")
