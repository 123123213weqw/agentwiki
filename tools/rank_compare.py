"""Compare candidate rankings for which entities the UI should show.

The UI exported the top 400 entities by `mention_count`, which turned out to be
a bad proxy for importance: a file name is mentioned in *every* tool call that
touches it, so it accumulates enormous counts inside a single session, while an
error surfaces a handful of times and is far more informative.

`session_count` (how many distinct conversations mention it) is already in the
schema and measures breadth rather than volume.  This script prints both
rankings side by side so the choice is made on evidence, not intuition.

    python tools/rank_compare.py
"""

from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentwiki import config  # noqa: E402

KINDS = ("repo", "path", "error", "pr", "issue", "pkg", "domain", "file")
PLACEHOLDERS = ",".join("?" * len(KINDS))
LIMIT = 400

RANKINGS = {
    "mentions (current)": "mention_count DESC",
    "breadth (sessions)": "session_count DESC, mention_count DESC",
}


def main() -> int:
    # Read-only URI: the ingester may be running, and this must never write.
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    for label, order in RANKINGS.items():
        print(f"=== {label}: top 12 ===")
        rows = conn.execute(
            f"""SELECT kind, canonical, mention_count m, session_count s
                FROM entities WHERE kind IN ({PLACEHOLDERS})
                ORDER BY {order} LIMIT 12""",
            KINDS,
        )
        for r in rows:
            print("  %-7s %-40s mentions=%-5d sessions=%d"
                  % (r["kind"], r["canonical"][:40], r["m"], r["s"]))

        kept = conn.execute(
            f"""SELECT kind, canonical FROM entities WHERE kind IN ({PLACEHOLDERS})
                ORDER BY {order} LIMIT {LIMIT}""",
            KINDS,
        ).fetchall()
        counts = Counter(r["kind"] for r in kept)
        errs = [r["canonical"] for r in kept if r["kind"] == "error"]
        tech = counts["file"] + counts["path"]
        print("  distribution: "
              + "  ".join(f"{k}={counts[k]}" for k in sorted(counts, key=lambda x: -counts[x])))
        print(f"  file+path = {tech}/{LIMIT} = {100 * tech / LIMIT:.1f}%")
        print(f"  errors kept: {len(errs)}"
              + ("  -> " + ", ".join(errs[:8]) if errs else ""))
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
