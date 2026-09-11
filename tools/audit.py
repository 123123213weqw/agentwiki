"""Safety + integrity audit for the L0 store."""

import re
import sys

sys.path.insert(0, ".")
from agentwiki import config, db  # noqa: E402

PATTERNS = {
    "openai_key": re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    "github_pat": re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    "aws_key": re.compile(r"AKIA[0-9A-Z]{12,}"),
    "bearer": re.compile(r"[Bb]earer\s+[A-Za-z0-9._\-]{20,}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "password_assign": re.compile(r"(?i)password\s*[=:]\s*[\"']?[^\s\"']{6,}"),
    "api_key_assign": re.compile(r"(?i)(api[_-]?key|secret|token)\s*[=:]\s*[\"'][^\s\"']{12,}[\"']"),
}

conn = db.store()
print("### unit sanity on data model")
bad = conn.execute(
    "select count(*) from items where created_at is not null and created_at < 100000000000"
).fetchone()[0]
print("  items with second-resolution created_at (<1e11):", bad)
bad2 = conn.execute(
    "select count(*) from sessions where updated_at is not null and updated_at < 100000000000"
).fetchone()[0]
print("  sessions with second-resolution updated_at   :", bad2)
print("  sessions with NULL repo                      :",
      conn.execute("select count(*) from sessions where repo is null").fetchone()[0])
print("  turns with duration_ms < 0                   :",
      conn.execute("select count(*) from turns where duration_ms < 0").fetchone()[0])

print()
print("### forbidden paths ever catalogued?")
for row in conn.execute("select distinct path from source_files"):
    p = row["path"]
    if config.is_forbidden(__import__("pathlib").Path(p)):
        print("  LEAK:", p)
print("  checked", conn.execute("select count(*) from source_files").fetchone()[0], "files")

print()
print("### secret scan over stored text (sampled: all rows, regex only)")
hits = {k: 0 for k in PATTERNS}
examples = {}
for row in conn.execute("select item_id, text from items where text_len > 0"):
    t = row["text"]
    for name, pat in PATTERNS.items():
        m = pat.search(t)
        if m:
            hits[name] += 1
            examples.setdefault(name, (row["item_id"], m.group(0)[:40]))
for k, v in sorted(hits.items(), key=lambda x: -x[1]):
    print(f"  {k:<16} {v}" + (f"   e.g. {examples[k][1]!r}" if k in examples else ""))

print()
print("### idempotency / integrity")
print("  sessions:", conn.execute("select count(*) from sessions").fetchone()[0])
print("  items   :", conn.execute("select count(*) from items").fetchone()[0])
print("  dup item_ids:",
      conn.execute("select count(*) from (select source,item_id from items group by 1,2 having count(*)>1)").fetchone()[0])
print("  orphan items (no session):",
      conn.execute("select count(*) from items i left join sessions s on s.session_id=i.session_id where s.session_id is null").fetchone()[0])
