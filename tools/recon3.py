import json, glob, os, sys

SESS = os.path.join(os.environ["APPDATA"], "com.xlang.xharness", "state", "sessions")
want = {"user/message": 2, "assistant/message": 2, "session/title": 1, "turn/end": 1}
got = {k: 0 for k in want}
lines_out = []
files = sorted(glob.glob(os.path.join(SESS, "*.jsonl")))
for fp in files:
    if all(got[k] >= want[k] for k in want):
        break
    try:
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if all(got[k] >= want[k] for k in want):
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("record") != "batch":
                    continue
                for ev in rec.get("events", []):
                    et = (ev.get("event") or {}).get("type")
                    if et in want and got[et] < want[et]:
                        got[et] += 1
                        lines_out.append("### %s  (%s)" % (et, os.path.basename(fp)))
                        lines_out.append(json.dumps(ev, ensure_ascii=False)[:1500])
                        lines_out.append("")
    except OSError:
        continue

with open("recon3.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(lines_out))
print("done", {k: got[k] for k in want})
