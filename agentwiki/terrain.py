"""Build the L0 "knowledge terrain map" report.

This is the decision document: before spending a single LLM token on
extraction (L1), look at this and decide whether the corpus contains gold.

Everything here is derived mechanically from the sqlite store: counts,
joins and percentiles.  No model is involved.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict

from . import config, db

PCTS = (10, 25, 50, 75, 90, 95, 99)


def _pct(vals, ps=PCTS):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return {}
    n = len(vals)
    return {p: vals[min(n - 1, max(0, int(round(p / 100 * (n - 1)))))] for p in ps}


def _fmt_ms(ms: float) -> str:
    if ms is None:
        return "-"
    s = ms / 1000.0
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        return f"{s / 60:.1f}m"
    if s < 86400:
        return f"{s / 3600:.1f}h"
    return f"{s / 86400:.1f}d"


def _ts(ms):
    if not ms:
        return "-"
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ms / 1000))
    except (OSError, ValueError):
        return "-"


def _q(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def _table(rows, headers, limit=None):
    rows = list(rows)
    if limit:
        rows = rows[:limit]
    if not rows:
        return "_（无数据 / no data）_\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        out.append("| " + " | ".join("" if c is None else str(c) for c in r) + " |")
    return "\n".join(out) + "\n"


def build(conn) -> str:
    L = []
    A = L.append

    A("# AgentWiki — L0 知识地形图 / Knowledge Terrain Map\n")
    A(f"_生成时间 / generated: {time.strftime('%Y-%m-%d %H:%M:%S')}_\n")
    A("> 这一层完全没有调用 LLM。它只回答一个问题：**值不值得为这些数据花钱做抽取？**\n")

    # ---------------------------------------------------------------- totals
    n_sessions = _q(conn, "SELECT COUNT(*) FROM sessions")[0][0]
    n_turns = _q(conn, "SELECT COUNT(*) FROM turns")[0][0]
    n_items = _q(conn, "SELECT COUNT(*) FROM items")[0][0]
    n_chars = _q(conn, "SELECT COALESCE(SUM(text_len),0) FROM items")[0][0]
    n_ent = _q(conn, "SELECT COUNT(*) FROM entities")[0][0]
    n_ment = _q(conn, "SELECT COUNT(*) FROM mentions")[0][0]
    n_files = _q(conn, "SELECT COUNT(*) FROM source_files")[0][0]
    A("## 1. 总量 / Corpus totals\n")
    A(_table(
        [
            ("会话 sessions", n_sessions),
            ("轮次 turns", n_turns),
            ("条目 items", n_items),
            ("正文字符 text chars", f"{n_chars:,}"),
            ("抽取实体 entities (regex)", n_ent),
            ("提及关系 mentions", n_ment),
            ("目录中登记的原始文件 source_files", n_files),
        ],
        ["指标 metric", "值 value"],
    ))

    # ---------------------------------------------------------------- sources
    A("\n## 2. 来源分布 / By source\n")
    A(_table(
        _q(conn, """SELECT source, COUNT(DISTINCT session_id) n_sessions,
                      COUNT(*) n_items, SUM(text_len) chars
                    FROM items GROUP BY source ORDER BY n_items DESC"""),
        ["source", "sessions", "items", "chars"],
    ))
    A("\n### items 类型分布 / item_type\n")
    A(_table(
        _q(conn, """SELECT source, item_type, COUNT(*) n, SUM(text_len) chars
                    FROM items GROUP BY source, item_type ORDER BY n DESC"""),
        ["source", "item_type", "n", "chars"], limit=30,
    ))

    # ---------------------------------------------------------------- repos
    A("\n## 3. 仓库地形 / Where your work actually lives\n")
    A("_决定 wiki 该按什么切分页面的最重要一张表。_\n")
    A(_table(
        _q(conn, """SELECT COALESCE(repo,'(none)') repo, COUNT(*) sessions,
                      COALESCE(SUM(tokens_used),0) tokens,
                      strftime('%Y-%m-%d', MIN(created_at)/1000, 'unixepoch') first,
                      strftime('%Y-%m-%d', MAX(updated_at)/1000, 'unixepoch') last
                    FROM sessions GROUP BY repo ORDER BY sessions DESC"""),
        ["repo", "sessions", "tokens_used", "first", "last"], limit=25,
    ))

    A("\n### 会话工作目录 / session cwd (authoritative)\n")
    A("_这张表不靠正则猜，直接来自每个会话的 `cwd`，是判断 wiki 切分粒度的首选依据。_\n")
    A(_table(
        _q(conn, """SELECT cwd, COUNT(*) sessions, SUM(item_count) items
                    FROM sessions WHERE cwd IS NOT NULL AND cwd != ''
                    GROUP BY cwd ORDER BY sessions DESC"""),
        ["cwd", "sessions", "items"], limit=25,
    ))

    A("\n### 高频项目目录 / top project paths (regex-mined)\n")
    A(_table(
        _q(conn, """SELECT canonical, mention_count, session_count
                    FROM entities WHERE kind='path'
                    ORDER BY mention_count DESC"""),
        ["path", "mentions", "sessions"], limit=25,
    ))

    # ---------------------------------------------------------------- hot files
    A("\n## 4. 热点文件 / Hot files (the actual battlegrounds)\n")
    A(_table(
        _q(conn, """SELECT canonical, mention_count, session_count
                    FROM entities WHERE kind='file'
                    ORDER BY mention_count DESC"""),
        ["file", "mentions", "sessions"], limit=30,
    ))

    # ---------------------------------------------------------------- domains
    A("\n## 5. 研究目的地 / What you look up (domains)\n")
    A(_table(
        _q(conn, """SELECT canonical, mention_count, session_count
                    FROM entities WHERE kind='domain'
                    ORDER BY mention_count DESC"""),
        ["domain", "mentions", "sessions"], limit=25,
    ))

    # ---------------------------------------------------------------- errors
    A("\n## 6. 报错地形 / Recurring pain (highest-value wiki pages)\n")
    A("_同一种错误反复出现 = 你反复忘记同一件事。这是 L1 最该优先抽取的对象。_\n")
    A(_table(
        _q(conn, """SELECT canonical, mention_count, session_count
                    FROM entities WHERE kind='error'
                    ORDER BY mention_count DESC"""),
        ["error", "mentions", "sessions"], limit=25,
    ))

    # ---------------------------------------------------------------- deps
    A("\n## 7. 依赖地形 / Packages you install\n")
    A(_table(
        _q(conn, """SELECT canonical, mention_count
                    FROM entities WHERE kind='pkg'
                    ORDER BY mention_count DESC"""),
        ["package", "mentions"], limit=20,
    ))

    # ---------------------------------------------------------------- repos refs
    A("\n## 8. 你碰过的开源仓库 / Upstream repos you touched\n")
    A(_table(
        _q(conn, """SELECT canonical, mention_count, session_count
                    FROM entities WHERE kind IN ('repo','pr','issue')
                    ORDER BY kind, mention_count DESC"""),
        ["entity", "mentions", "sessions"], limit=30,
    ))

    # ---------------------------------------------------------------- duration
    A("\n## 9. 等待时长 / Turn durations — the waiting-time dataset\n")
    A("_这是另一个产品（把等待变成学习）直接要用的数据。_\n")
    rows = _q(conn, "SELECT status, duration_ms FROM turns WHERE duration_ms IS NOT NULL AND duration_ms > 0")
    by = defaultdict(list)
    for st, d in rows:
        by[st or "?"].append(d)
    A(_table(
        [(st, len(v), _fmt_ms(_pct(v).get(50)), _fmt_ms(_pct(v).get(75)),
          _fmt_ms(_pct(v).get(90)), _fmt_ms(_pct(v).get(95)), _fmt_ms(max(v)))
         for st, v in sorted(by.items(), key=lambda kv: -len(kv[1]))],
        ["status", "n", "p50", "p75", "p90", "p95", "max"],
    ))
    A("\n### 按仓库分组 / p50 by repo\n")
    rows = _q(conn, """SELECT COALESCE(s.repo,'(none)') repo, t.duration_ms
                       FROM turns t JOIN sessions s ON s.session_id=t.session_id
                       WHERE t.duration_ms>0""")
    br = defaultdict(list)
    for repo, d in rows:
        br[repo].append(d)
    A(_table(
        [(r, len(v), _fmt_ms(_pct(v).get(50)), _fmt_ms(_pct(v).get(90)))
         for r, v in sorted(br.items(), key=lambda kv: -len(kv[1]))],
        ["repo", "n", "p50", "p90"], limit=20,
    ))

    # ---------------------------------------------------------------- timeline
    A("\n## 10. 时间线 / Growth over time\n")
    A(_table(
        _q(conn, """SELECT strftime('%Y-%m', created_at/1000, 'unixepoch') m,
                      COUNT(*) items, COUNT(DISTINCT session_id) sessions
                    FROM items WHERE created_at IS NOT NULL
                    GROUP BY m ORDER BY m"""),
        ["month", "items", "sessions"],
    ))

    # ---------------------------------------------------------------- verdict
    A("\n## 11. 判读 / Verdict\n")
    A("先看三个数，再决定要不要进入 L1：\n")
    top_err = _q(conn, """SELECT canonical, mention_count FROM entities
                          WHERE kind='error' ORDER BY mention_count DESC LIMIT 1""")
    top_repo = _q(conn, """SELECT COALESCE(repo,'(none)'), COUNT(*) FROM sessions
                           GROUP BY repo ORDER BY 2 DESC LIMIT 1""")
    A(f"1. **体量**：{n_items:,} 条 item / {n_chars:,} 字符。")
    A("   如果字符数只有几十万，L1 没必要做；如果上了千万，值得。")
    if top_repo:
        A(f"2. **集中度**：最大仓库 `{top_repo[0][0]}` 占 {top_repo[0][1]} 个会话。")
        A("   集中度高 → 少量实体页就能覆盖大部分价值，wiki 会很紧实。")
    if top_err:
        A(f"3. **复用痛点**：出现最多的错误是 `{top_err[0][0]}`（{top_err[0][1]} 次）。")
        A("   同一个错反复犯 → 这正是 wiki 最能救命的地方。")
    A("")
    A("**如果上面三项都成立，L1 抽取（每轮抽 claim + 原文出处）就是值得做的。**\n")
    return "\n".join(L)


def console_summary(conn) -> str:
    """Short ASCII-safe summary for the terminal."""
    out = []
    g = lambda sql, a=(): _q(conn, sql, a)[0][0]  # noqa: E731
    out.append("items=%s  turns=%s  sessions=%s  entities=%s  chars=%s" % (
        g("SELECT COUNT(*) FROM items"), g("SELECT COUNT(*) FROM turns"),
        g("SELECT COUNT(*) FROM sessions"), g("SELECT COUNT(*) FROM entities"),
        g("SELECT COALESCE(SUM(text_len),0) FROM items")))
    out.append("")
    out.append("top repos:")
    for repo, n in _q(conn, """SELECT COALESCE(repo,'(none)'), COUNT(*) FROM sessions
                               GROUP BY repo ORDER BY 2 DESC LIMIT 8"""):
        out.append("   %-46s %4d sessions" % ((repo or "-")[:46], n))
    out.append("")
    out.append("top files:")
    for c, n in _q(conn, "SELECT canonical,mention_count FROM entities WHERE kind='file' ORDER BY mention_count DESC LIMIT 8"):
        out.append("   %-46s %4d" % (c[:46], n))
    out.append("")
    out.append("top errors:")
    for c, n in _q(conn, "SELECT canonical,mention_count FROM entities WHERE kind='error' ORDER BY mention_count DESC LIMIT 8"):
        out.append("   %-46s %4d" % (c[:46], n))
    out.append("")
    rows = _q(conn, "SELECT duration_ms FROM turns WHERE duration_ms>0")
    p = _pct([r[0] for r in rows])
    if p:
        out.append("turn durations: n=%d p50=%s p75=%s p90=%s p95=%s" % (
            len(rows), _fmt_ms(p[50]), _fmt_ms(p[75]), _fmt_ms(p[90]), _fmt_ms(p[95])))
    return "\n".join(out)
