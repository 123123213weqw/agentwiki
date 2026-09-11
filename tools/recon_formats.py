import sqlite3, json, os, glob, collections

CODEX = os.path.join(os.path.expanduser("~"), ".codex")
XH = os.path.join(os.environ["APPDATA"], "com.xlang.xharness")
out = []
def p(*a): out.append(" ".join(str(x) for x in a))

# ---------------- 1. codex ----------------
con = sqlite3.connect("file:" + CODEX + r"\thread_history_1.sqlite?mode=ro&immutable=1", uri=True)

p("=" * 70)
p("CODEX thread_items: agentMessage phases")
ph = collections.Counter()
for (j,) in con.execute("select item_json from thread_items where item_type='agentMessage'"):
    try: ph[json.loads(j).get("phase")] += 1
    except Exception: ph["<parse-err>"] += 1
for k, v in ph.most_common(): p("   %-20s %d" % (k, v))

p("")
p("CODEX top-level keys per item_type")
for it in ['agentMessage','reasoning','fileChange','commandExecution','userMessage',
           'webSearch','mcpToolCall','subAgentActivity','contextCompaction','imageView']:
    row = con.execute("select item_json from thread_items where item_type=? limit 1", (it,)).fetchone()
    try:
        d = json.loads(row[0]) if row else {}
        p("   %-18s %s" % (it, sorted(d.keys())))
    except Exception as e:
        p("   %-18s ERR %s" % (it, e))

p("")
p("CODEX userMessage content shape")
row = con.execute("select item_json from thread_items where item_type='userMessage' limit 2").fetchall()
for (j,) in row:
    d = json.loads(j)
    p("   ", json.dumps(d, ensure_ascii=False)[:400])

p("")
p("CODEX threads: top cwd")
st = sqlite3.connect("file:" + CODEX + r"\state_5.sqlite?mode=ro&immutable=1", uri=True)
for r in st.execute("select cwd, count(*) from threads group by cwd order by 2 desc limit 15"):
    p("   %4d  %s" % (r[1], r[0]))
p("")
p("CODEX threads: top git_origin_url")
for r in st.execute("select git_origin_url, count(*) from threads where git_origin_url is not null group by 1 order by 2 desc limit 15"):
    p("   %4d  %s" % (r[1], r[0]))

# ---------------- 2. xharness ----------------
p("")
p("=" * 70)
p("XHARNESS session jsonl format")
files = [f for f in glob.glob(os.path.join(XH, "state", "sessions", "*.jsonl"))]
files.sort(key=lambda f: os.path.getsize(f))
p("   total files: %d" % len(files))
sample = [f for f in files if os.path.getsize(f) > 2000][:3]
for f in sample:
    p("   --- %s (%d bytes) ---" % (os.path.basename(f), os.path.getsize(f)))
    with open(f, "r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i >= 3: break
            line = line.strip()
            if not line: continue
            try:
                d = json.loads(line)
                p("      line%d keys=%s" % (i, sorted(d.keys())))
                p("      %s" % json.dumps(d, ensure_ascii=False)[:500])
            except Exception as e:
                p("      line%d RAW(%.200s) err=%s" % (i, line, e))

# ---------------- 3. claude ----------------
p("")
p("=" * 70)
p("CLAUDE projects jsonl format")
cf = glob.glob(os.path.join(os.path.expanduser("~"), ".claude", "projects", "**", "*.jsonl"), recursive=True)
p("   total files: %d" % len(cf))
cf.sort(key=lambda f: os.path.getsize(f))
for f in [x for x in cf if os.path.getsize(x) > 3000][:2]:
    p("   --- %s (%d bytes) ---" % (os.path.basename(f), os.path.getsize(f)))
    with open(f, "r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i >= 2: break
            line = line.strip()
            if not line: continue
            try:
                d = json.loads(line)
                p("      keys=%s" % sorted(d.keys()))
                p("      %s" % json.dumps(d, ensure_ascii=False)[:600])
            except Exception as e:
                p("      RAW(%.200s) err=%s" % (line, e))

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "recon_formats.txt"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(out))
print("wrote recon_formats.txt")
