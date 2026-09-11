"""Find a string anywhere in the wiki database.

Used to confirm that a credential you accidentally captured has really been
purged, and that only `[REDACTED_*]` placeholders remain.

The needle is passed on the command line, never hardcoded -- this file is
published, so it must not contain anyone's real key.

    python tools/find_needle.py sk-abc123        # scan every text column
    python tools/find_needle.py --placeholders   # count redaction markers
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

DEFAULT_DB = os.path.expanduser("~/.agentwiki/wiki.db")
PLACEHOLDERS = ("[REDACTED_KEY]", "[REDACTED_GH]", "[REDACTED_AWS]",
                "[REDACTED_SLACK]", "[REDACTED_JWT]", "[REDACTED]")


def _text_columns(con, table: str) -> list[str]:
    return [c[1] for c in con.execute(f'pragma table_info("{table}")')]


def scan(con, needle: str, context: int = 50) -> int:
    tables = [r[0] for r in con.execute(
        "select name from sqlite_master where type='table'")]
    total = 0
    for t in tables:
        for c in _text_columns(con, t):
            try:
                n = con.execute(
                    f'select count(*) from "{t}" where "{c}" like ?',
                    (f"%{needle}%",),
                ).fetchone()[0]
            except sqlite3.Error:
                continue
            if not n:
                continue
            total += n
            print(f"HIT {t}.{c}  rows={n}")
            for (v,) in con.execute(
                f'select "{c}" from "{t}" where "{c}" like ? limit 1',
                (f"%{needle}%",),
            ):
                v = v or ""
                i = v.find(needle)
                lo = max(0, i - context)
                print("   ...", v[lo:i + context].replace("\n", "\\n"))
    return total


def count_placeholders(con) -> None:
    print("=== redaction placeholders present ===")
    for marker in PLACEHOLDERS:
        n = con.execute(
            "select count(*) from items where text like ?", (f"%{marker}%",)
        ).fetchone()[0]
        print(f"  {marker:<18} {n} items")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("needle", nargs="?", help="literal string to search for")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--placeholders", action="store_true",
                    help="report redaction marker counts instead of scanning")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"no database at {args.db}", file=sys.stderr)
        return 2

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        if args.placeholders:
            count_placeholders(con)
            return 0
        if not args.needle:
            ap.error("needle is required unless --placeholders is given")
        total = scan(con, args.needle)
        print(f"  -> total rows: {total}")
        if total:
            print("\nNOT CLEAN: re-run ingest after fixing redact.py", file=sys.stderr)
            return 1
        print("\nCLEAN")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
