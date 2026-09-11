import sqlite3, os, datetime
p = os.path.expanduser("~/.agentwiki/wiki.db")
c = sqlite3.connect(p); c.row_factory = sqlite3.Row
def show(t, sql):
    print("### " + t)
    try:
        for r in c.execute(sql):
            print("   ", dict(r))
    except Exception as e:
        print("   ERR", e)
    print()

show("items per source", "select source, count(*) n, sum(text_len) chars from items group by source")
show("sessions per source / item_count", "select source, count(*) n, sum(item_count) sum_ic, max(item_count) max_ic from sessions group by source")
show("created_at ranges per source (raw)",
     "select source, min(created_at) mn, max(created_at) mx from items group by source")
show("sample claude session rows", "select session_id, item_count, turn_count, created_at, updated_at from sessions where source='claude' limit 3")
show("sample codex session rows", "select session_id, item_count, turn_count, created_at, updated_at from sessions where source='codex' limit 3")
show("sample xharness session rows", "select session_id, item_count, turn_count, created_at, updated_at from sessions where source='xharness' limit 3")
show("xharness items created_at", "select item_type, created_at from items where source='xharness' limit 5")
show("turns per source", "select substr(session_id,1,6) sid, status, duration_ms from turns limit 5")
show("turns count by session prefix",
     "select count(*) from turns")
show("bad path entities", "select entity_id, mention_count from entities where kind='path' and (entity_id like '%system32%' or entity_id like '%usr/bin%' or entity_id like 'path:c:/users') order by mention_count desc limit 10")
show("repo-ish entities", "select entity_id, mention_count from entities where kind='repo' order by mention_count desc limit 12")
