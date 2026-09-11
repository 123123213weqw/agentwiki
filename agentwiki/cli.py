"""AgentWiki command line.

    python -m agentwiki ingest  --source codex|claude|xharness|all
    python -m agentwiki terrain [--open]
    python -m agentwiki stats
    python -m agentwiki shell
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time

from . import config, db, terrain


def _enc() -> None:
    """Make stdout utf-8 so we never crash on a CJK title."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _ascii(s) -> str:
    return str(s).encode("ascii", "replace").decode()


def cmd_ingest(args) -> int:
    from .ingest import claude, codex, xharness

    sources = ["codex", "claude", "xharness"] if args.source == "all" else [args.source]
    conn = db.store()
    grand = {}
    for src in sources:
        mod = {"codex": codex, "claude": claude, "xharness": xharness}[src]
        print(f"[ingest] {src} ...", flush=True)
        t0 = time.time()
        try:
            res = mod.ingest(conn, verbose=not args.quiet)
        except FileNotFoundError as e:
            print(f"[ingest] {src}: SKIP ({e})")
            continue
        except Exception as e:  # keep going: one broken source must not block the rest
            print(f"[ingest] {src}: FAILED: {type(e).__name__}: {e}")
            continue
        conn.commit()
        res["seconds"] = round(time.time() - t0, 1)
        grand[src] = res
        print(f"[ingest] {src}: {res}", flush=True)

    print("[ingest] recomputing counts ...", flush=True)
    db.recompute_session_counts(conn)
    conn.commit()
    print("[ingest] done.")
    return 0


def cmd_terrain(args) -> int:
    conn = db.store()
    md = terrain.build(conn)
    summary = terrain.console_summary(conn)
    config.ensure_dirs()
    out = config.REPORT_DIR / "terrain.md"
    out.write_text(md, encoding="utf-8")
    print(_ascii(summary))
    print()
    print(f"full report -> {out}")
    return 0


def cmd_stats(args) -> int:
    conn = db.store()
    for row in conn.execute("select source, count(*) n, sum(item_count) items from sessions group by source"):
        print(f"  source={row['source']:<10} sessions={row['n']:<6} items={row['items']}")
    for row in conn.execute("select kind, count(*) n from entities group by kind order by n desc"):
        print(f"  entity kind={row['kind']:<8} n={row['n']}")
    for row in conn.execute("select id, source, started_at, n_sessions, n_items from ingest_runs order by id desc limit 8"):
        print(f"  run#{row['id']} {row['source']} sessions={row['n_sessions']} items={row['n_items']}")
    return 0


def cmd_shell(args) -> int:
    conn = db.store()
    print(f"sqlite3 {config.DB_PATH}")
    print("tables:", ", ".join(r[0] for r in conn.execute(
        "select name from sqlite_master where type='table' order by name")))
    while True:
        try:
            q = input("sql> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not q:
            continue
        if q in ("exit", "quit", ".q"):
            return 0
        try:
            cur = conn.execute(q)
            cols = [d[0] for d in cur.description] if cur.description else []
            if cols:
                print(" | ".join(cols))
                for r in cur.fetchmany(50):
                    print(_ascii(" | ".join(str(x)[:60] for x in r)))
            else:
                conn.commit()
                print(f"ok, rowcount={cur.rowcount}")
        except sqlite3.Error as e:
            print("error:", e)


def main(argv=None) -> int:
    _enc()
    p = argparse.ArgumentParser(prog="agentwiki", description="Personal knowledge wiki built from your agent sessions.")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("ingest", help="read agent session sources into the wiki db")
    i.add_argument("--source", default="all", choices=["codex", "claude", "xharness", "all"])
    i.add_argument("--quiet", action="store_true")
    i.set_defaults(fn=cmd_ingest)

    t = sub.add_parser("terrain", help="build the knowledge terrain map report")
    t.set_defaults(fn=cmd_terrain)

    s = sub.add_parser("stats", help="show db contents summary")
    s.set_defaults(fn=cmd_stats)

    sh = sub.add_parser("shell", help="interactive sql shell over the wiki db")
    sh.set_defaults(fn=cmd_shell)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
