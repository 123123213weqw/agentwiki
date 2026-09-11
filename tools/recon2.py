import json, os, re, collections, glob

OUT = []
def p(*a):
    OUT.append(" ".join(str(x) for x in a))

# ---------- CLAUDE ----------
claude_root = os.path.expanduser(r"~\.claude\projects")
types = collections.Counter()
sess = 0
msgs = 0
sizes = 0
bad = 0
for path in glob.glob(os.path.join(claude_root, "**", "*.jsonl"), recursive=True):
    sess += 1
    sizes += os.path.getsize(path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    bad += 1
                    continue
                types[d.get("type")] += 1
                if d.get("type") in ("user", "assistant"):
                    msgs += 1
    except Exception as e:
        p("  ERR", path, e)

p("======================================================================")
p("CLAUDE: files=%d  total=%.1f MB  badlines=%d  user+assistant=%d" % (sess, sizes / 1048576, bad, msgs))
for t, c in types.most_common(25):
    p("   %-28s %d" % (t, c))

# sample a real user/assistant line's keys
p("")
p("CLAUDE sample message keys:")
shown = 0
for path in glob.glob(os.path.join(claude_root, "**", "*.jsonl"), recursive=True):
    if shown >= 2:
        break
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") in ("user", "assistant"):
                p("   ", d.get("type"), "->", sorted(d.keys()))
                msg = d.get("message")
                if isinstance(msg, dict):
                    p("        message.role=%s keys=%s" % (msg.get("role"), sorted(msg.keys())))
                    c = msg.get("content")
                    p("        content kind=%s" % type(c).__name__)
                    if isinstance(c, list) and c:
                        p("        content[0] keys=%s" % sorted(c[0].keys()) if isinstance(c[0], dict) else "")
                shown += 1
                break

# ---------- XHARNESS ----------
xh_root = os.path.join(os.environ["APPDATA"], "com.xlang.xharness", "state", "sessions")
rec_re = re.compile(r'"record"\s*:\s*"([^"]+)"')
ev_re = re.compile(r'"event"\s*:\s*\{\s*"type"\s*:\s*"([^"]+)"')
recs = collections.Counter()
evs = collections.Counter()
nfiles = 0
tot = 0
for path in glob.glob(os.path.join(xh_root, "*.jsonl")):
    nfiles += 1
    tot += os.path.getsize(path)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = rec_re.search(line)
            if m:
                recs[m.group(1)] += 1
            for e in ev_re.findall(line):
                evs[e] += 1

p("")
p("======================================================================")
p("XHARNESS: files=%d  total=%.1f MB" % (nfiles, tot / 1048576))
p("  record types:")
for t, c in recs.most_common(15):
    p("     %-24s %d" % (t, c))
p("  event types (top 40):")
for t, c in evs.most_common(40):
    p("     %-40s %d" % (t, c))

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "recon2.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(OUT))
print("written")
