# AgentWiki —— 用外部 LLM 持续爬你自己的 agent 会话，编译成个人知识 wiki

## 0. 结论先说

**能做，而且你的机器上原料已经齐了、还是结构化的。**

不需要"监听"、不需要"检测等待"、不需要 hook。你过去半年的每一次 vibe coding，
agent 都替你记了笔记——只是没人去读。三张表 join 起来就是一个完整的知识语料库：

```
state_5.sqlite : threads                 → 会话元数据（title, cwd, git_branch, model, tokens_used, 142 行）
thread_history_1.sqlite : thread_turns   → 每一轮 + duration_ms（2837 行）
thread_history_1.sqlite : thread_items   → 全部内容（39305 行，item_json）
```

join key 都是 `thread_id`。**这三张表就是 spine。**

### 实测的原料清单

| 源 | 位置 | 量 | 价值 |
|---|---|---|---|
| Codex 会话库（**主力**） | `~/.codex/thread_history_1.sqlite` | 39305 items / 2837 turns / 142 threads | ★★★★★ |
| Codex 会话元数据 | `~/.codex/state_5.sqlite` | `threads` 表 | ★★★★★（脊梁） |
| Codex 原始 rollout | `~/.codex/sessions/**/*.jsonl` | 134 文件（最大 130 MB） | ★★★（兜底/原文） |
| Codex 内置记忆 | `~/.codex/memories_1.sqlite` | `stage1_outputs` 表现为空 | ★★（可搭车） |
| Claude Code | `~/.claude/projects/**/*.jsonl` | 149 文件 | ★★★★ |
| **xharness 自己的会话** | `state/sessions/*.jsonl` | **1052 文件**（最大 186 MB） | ★★★★★ |
| Cursor | `~/.cursor/ai-tracking` | — | ★★ |

### Codex 会话里的 item 类型分布（实测）

```
agentMessage      17288   ← agent 的回答（含 final_answer）
reasoning          7899   ← 大多 summary 为空，噪音
fileChange         5899   ← 带真实 diff！代码演化史
commandExecution   3173   ← 跑过什么命令
userMessage        2784   ← 你的意图（最值钱的信号）
mcpToolCall         778
webSearch           661   ← 带 query + 结果标题 + URL（你的好奇心轨迹）
contextCompaction   401   ← 注意：长会话被压缩过
subAgentActivity    256
imageView           166
```

### 顺带白捡的东西：等待时长分布（实测 2351 个 completed turn）

```
p10    14.1 s
p25    25.0 s
p50    65.5 s      ← 一半的等待在一分钟以上
p75   220.0 s
p90   670.1 s
p95  1230.8 s
p99 12644.7 s
```

**上一次讨论的"死时间学习"里我让你"预测等待时长"——不用做预测模型，
这张表就是答案。** 按 `git_branch` / `command` 分组还能更准。两个产品可以共用同一套采集层。

---

## 1. 最重要的一条判断：**别做摘要，做抽取**

这是整个项目的成败分界。

- ❌ **会话摘要**：142 个 thread 变成 142 篇"本次会话我们讨论了……"。没人会读第二遍。它只是把日志换了个格式。
- ✅ **实体页 + 断言 + 出处**：`IvorySQL/Oracle兼容` 这一页，聚合了你在 12 个 thread 里
  说过的 37 条相关断言，每条都能跳回 `thread_id/turn_id/item_id`。

**wiki 的四要件**（缺一个就退化成 RAG）：

| 要件 | 为什么必须有 |
|---|---|
| **页面（实体）** | 知识要有"家"。散落的片段等于没有 |
| **链接（图）** | 知识靠关系增值。`KDA` ↔ `线性注意力` ↔ `Ling-3.0-tiny` |
| **出处（provenance）** | 每个断言可回溯到原文 span，否则就是幻觉农场 |
| **版本（演化）** | 信念会变。**这是 wiki 相对 RAG 的唯一压倒性优势** |

> **RAG 检索原始片段，平铺给你，让你自己分辨新旧对错。**
> **wiki 抽取、去重、排序、标时间、标已推翻——然后才给你。**

你的语料里 80% 是噪音（reasoning 空摘要、重复的工具输出、失败的尝试）。
RAG 会把这些一并检索出来。wiki 的价值就在于**它已经替你做过减法**。

---

## 2. 架构：四层，按成本阶梯

```
┌─ L0 采集（无 LLM，$0，分钟级）────────────────────┐
│  sqlite/jsonl → 规范化 → 脱敏 → 实体骨架          │
│  产出：sessions / turns / items / 正则实体 / 时长统计 │
└───────────────────────┬──────────────────────────┘
                        ↓
┌─ L1 抽取（便宜模型，按 turn，一次性 + 增量）────────┐
│  每个 turn → N 条 claim（带 evidence span）        │
│  产出：claims / entities / aliases                │
└───────────────────────┬──────────────────────────┘
                        ↓
┌─ L2 编译（贵模型，按实体，低频）───────────────────┐
│  同一实体的所有 claim → 一页 markdown + 冲突处理    │
│  产出：pages / links / backlinks                  │
└───────────────────────┬──────────────────────────┘
                        ↓
┌─ L3 演化（最贵，极低频）──────────────────────────┐
│  跨页发现矛盾、标记过时、生成"我改变了什么看法"     │
└───────────────────────────────────────────────────┘
```

**关键：L0 立刻有产出、且免费。** 不要一上来就调 LLM。先把骨架搭出来看有没有价值。

---

## 3. 数据模型（灵魂在这）

```sql
-- ===== L0：从 codex/xharness 直接映射 =====
CREATE TABLE sessions (
  session_id   TEXT PRIMARY KEY,        -- codex thread_id / xharness session-*
  source       TEXT NOT NULL,           -- 'codex' | 'claude' | 'xharness'
  title        TEXT,
  cwd          TEXT,
  repo         TEXT,                    -- git_origin_url 归一化后的仓库名
  branch       TEXT,
  model        TEXT,
  tokens_used  INTEGER,
  created_at   INTEGER,
  updated_at   INTEGER
);

CREATE TABLE turns (
  session_id   TEXT, turn_id TEXT,
  status       TEXT,                    -- completed/interrupted/failed
  started_at   INTEGER, duration_ms INTEGER,
  item_count   INTEGER,
  PRIMARY KEY (session_id, turn_id)
);

CREATE TABLE items (
  item_id      TEXT PRIMARY KEY,        -- codex item_id
  session_id   TEXT, turn_id TEXT,
  ord          INTEGER,
  created_at   INTEGER,
  item_type    TEXT,                    -- agentMessage/fileChange/userMessage/...
  text         TEXT,                    -- 脱敏后的正文 / diff / 命令
  content_hash TEXT,                    -- 变了才重抽（幂等关键）
  raw_json     TEXT,                    -- 保留原文，可回溯
  embedded     INTEGER DEFAULT 0
);
CREATE INDEX ix_items_session ON items(session_id, ord);
CREATE INDEX ix_items_type    ON items(item_type, created_at);

-- ===== L1：抽取结果 =====
CREATE TABLE entities (
  entity_id    TEXT PRIMARY KEY,        -- 规范化 slug: repo/ivorysql, lib/kda
  kind         TEXT,                    -- repo|lib|api|concept|person|pr|error|tool
  canonical    TEXT,                    -- 显示名
  first_seen   INTEGER, last_seen INTEGER,
  mention_count INTEGER DEFAULT 0
);

CREATE TABLE entity_aliases (
  alias        TEXT PRIMARY KEY,
  entity_id    TEXT,
  source       TEXT                     -- 'regex' | 'llm'
);

-- ★ 全项目的核心表
CREATE TABLE claims (
  claim_id     TEXT PRIMARY KEY,
  entity_id    TEXT,                    -- 主体：这条断言是关于谁的
  kind         TEXT NOT NULL,           -- decision|fact|puzzle|preference|solution
  statement    TEXT NOT NULL,           -- 一句话断言（wiki 页面上的一行）
  evidence     TEXT NOT NULL,           -- ★原文引用 span，没有就不许入库
  source_item  TEXT NOT NULL,           -- ★溯源：items.item_id
  session_id   TEXT,
  stated_at    INTEGER,                 -- 何时说的
  confidence   REAL DEFAULT 0.7,
  valid_from   INTEGER, valid_to INTEGER,
  superseded_by TEXT,                   -- ★ 被哪条 claim 推翻（不删！）
  dedup_key    TEXT UNIQUE              -- hash(entity|kind|归一化statement)
);

-- ===== L2：编译产物 =====
CREATE TABLE pages (
  entity_id    TEXT PRIMARY KEY,
  path         TEXT,                    -- wiki/ivorysql-oracle-compat.md
  revision     INTEGER DEFAULT 0,
  body_md      TEXT,
  compiled_at  INTEGER,
  compiled_from_watermark INTEGER
);

CREATE TABLE links (
  from_entity  TEXT, to_entity TEXT,
  kind         TEXT,                    -- mentions|depends_on|contradicts|derived_from
  weight       INTEGER DEFAULT 1,
  PRIMARY KEY (from_entity, to_entity, kind)
);

-- ===== 增量游标 =====
CREATE TABLE ingest_cursor (
  source       TEXT PRIMARY KEY,
  watermark    TEXT,                    -- byte offset / last item_id
  updated_at   INTEGER
);
```

### 为什么 `claims` 要这么设计

三个字段撑起了整个 wikiness：

1. **`evidence`（原文 span）** —— **强制非空**。LLM 抽的断言如果没有原文佐证，直接丢弃。
   这一条就能把幻觉率压到可用范围，因为你可以一键跳到原文核对。
2. **`source_item`** —— 答案是"我什么时候、在哪个会话里、因为什么说出这句话的"。可点击。
3. **`superseded_by` 而不是 DELETE** —— 这是 wiki 和 RAG 的分水岭。
   页面上渲染成：

   > ~~CHAR 和 OID 0 比较直接走 OID btree~~ *（2026-09-05 认为）*
   > **零长度 CHAR 必须走自己的比较路径** ✅ *（2026-09-11 修正）*

   **RAG 会把这两条一起给你，然后你被误导。** 这是最贵的一条设计，也是最值钱的一条。

---

## 4. 抽取什么：五类知识

对 vibe coding，只有这五类值得进 wiki：

| kind | 定义 | 例（来自你的真实数据） |
|---|---|---|
| **decision** ⭐ | 选 A 不选 B，因为 C | 「Ubuntu 的 ext4.vhdx 迁到 `D:\WSL\Ubuntu`，因为 C 盘只剩 22 GB」 |
| **fact** | 关于世界/代码库的稳定事实 | 「Oracle 把零长度 CHAR 当作 NULL 参与比较」 |
| **puzzle** | 症状 → 根因 → 修法 | 「`Get-ChildItem -Force` 在权限目录上抛错 → 改用 `[IO.DirectoryInfo]` 枚举」 |
| **preference** | 你的口味（最有个人价值） | 「不喜欢 `try/catch { continue }` 吞异常，宁可显式跳过」 |
| **solution** | 可复用的操作配方 | 「Tailscale 走 DERP 中继延迟 197–215 ms，用 `--exit-node` 切出口」 |

**decision 是最值钱的一类**，因为它是**你在特定约束下做的取舍**，任何教科书都不会有，
别的模型也抄不走。你的 wiki 的护城河就是这些。

**明确不抽的**：纯工具输出、空 reasoning、文件内容本身、一次性的路径碎片。

---

## 5. 冷启动：怎么把这 39305 条啃下来

**分层，按性价比从高到低。**

### 第 1 步：L0 全量（$0，几十分钟）
纯 SQL + 正则，不用模型：
- join 三张表，灌进 `sessions/turns/items`
- 正则抽实体骨架：仓库名、文件路径、命令、URL、PR 号、包名、报错类型
- **产出一张"知识地形图"**：哪些 repo / 哪些概念被你反复触碰
- **关键决策点：先看这张图，再决定要不要花钱。** 可能光这一步就够你用了

### 第 2 步：L1 只跑"有知识密度"的 turn（便宜，一次性）
不要 39305 条全跑。过滤规则：

```
用：userMessage 2784          ← 你的意图
   + agentMessage (final)     ← 只有 final，不要中间过程
   + fileChange 的 diff 摘要
   + webSearch 的 query       ← 好奇心轨迹
跳过：reasoning（7899，空摘要）、commandExecution 原文、mcpToolCall 结果
```

**约 5000 条 → 按 turn 聚合 → 约 2800 次 LLM 调用。**
用小模型 + 强制 JSON schema，批量并发。可控。

### 第 3 步：L2 只编译 top-N 实体
按 `mention_count` 排序，取前 100–300 个实体编译成页。其余保持"stub 页 + 反向链接列表"即可。

### 第 4 步：日常增量
每天新增 turn 大概 10–50 个 → 每轮结束跑一次 `ingest`，成本可忽略。

---

## 6. 增量与幂等（工程上最容易翻车的地方）

**两个好消息：**

1. **Codex 已经替你维护了水位线**：
   `thread_history_projection_state(thread_id, next_rollout_byte_offset, next_rollout_ordinal)`
   这是 codex 自己消费 rollout 的游标。你可以直接搭车。

2. **`content_hash` 是最终保险**。`items` 用 `content_hash` 比对，内容没变就跳过。

**规则：**
- 主键用 `(source, item_id)`，天然幂等，重复 ingest 无害
- claim 用 `dedup_key = hash(entity_id | kind | 归一化statement)` 去重
- **会话文件可能被追加写**：读 sqlite 时一律 `file:...?mode=ro&immutable=1`，
  **绝不要碰 WAL**，否则可能和正在运行的 codex 抢锁
- **`contextCompaction` 有 401 条**：说明长会话被压缩过。
  遇到压缩标记，要意识到该 turn 的上下文是**残缺**的，别把压缩摘要当原始陈述

---

## 7. 三种消费界面（第三个才是重点）

### (a) 静态 wiki 站点
Quartz / MkDocs Material。要求：
- 全文搜索
- **反向链接（backlinks）** —— 必须有，这是 wiki 的灵魂
- 每个 claim 是一个可点的小徽章，点了跳到对应会话原文
- 时间轴视图：「我什么时候学了 X」

### (b) 时间轴 / 「我这一年」
纯粹的自我回顾。按 `sessions.repo` 分组，看知识地图怎么长的。这个的情感价值极高。

### (c) ★ MCP server —— 闭环在这
**这才是让项目产生复利的一步。**

把 wiki 暴露成一个 MCP：
```
wiki.search(query)              → 相关实体页
wiki.assertions(entity)         → 该实体的所有 claims（含已推翻的）
wiki.history(entity)            → 信念演化时间线
wiki.related(entity)            → 图上的邻居
```

然后下次 agent 干活时，它能**自动查你的 wiki**：
> "你 3 个月前在 IvorySQL 里做过类似的 Oracle 兼容改动，当时的结论是 X。
>  这次要沿用还是推翻？"

于是形成**真正的闭环**：

```
agent 干活 → 会话落盘 → 抽取成 wiki → 下次 agent 读 wiki → 干得更好 → ...
```

**wiki 不只是给人看的，是给下一个 agent 当 context 的。** 这一步做完，
这个项目就从"知识管理玩具"变成了"你的私人 context 层基础设施"。

---

## 8. 坑（按危险程度排序）

| 坑 | 后果 | 对策 |
|---|---|---|
| **秘密泄漏** ⚠️ | `~/.codex/auth.json`、`.sandbox-secrets/` 在附近 | **白名单**只读指定表/字段；正则脱敏 `token\|key\|password\|Bearer\|ghp_`；绝不 glob 整个 `~/.codex` |
| **幻觉 claim** | wiki 变成一本编造的假书 | `evidence` 强制非空 + 可跳转原文。无 span 即丢弃 |
| **RAG 化** | 做成一堆摘要，没人读 | 强制"实体页 + 断言 + 出处 + 演化"四要件 |
| **噪音淹没** | 39305 条里全是工具输出 | 白名单 item_type；只取 agentMessage 的 `final_answer` phase |
| **sqlite 锁冲突** | 搞坏正在用的 codex | 只读 + `immutable=1`；先复制一份再做批量 |
| **成本失控** | 一次性把 39k item 全丢给贵模型 | 严格分层 L0→L1→L2；每层先 on 100 条样本验价 |
| **一次编译太贵** | 增量要重编全页 | L2 页编译要记录 `compiled_from_watermark`，只重编受影响的实体 |
| **页面碎片化** | 5000 个实体页，每个 1 行 | 设 `mention_count >= 3` 才建页，其余进 stub |
| **中英混杂** | 同一实体两种写法 | `entity_aliases` 表 + 归一化（大小写、下划线、中文别名） |
| **巨文件** | 单个 session jsonl 186 MB | 流式逐行读，绝不 `read()` 整个文件 |

---

## 9. 成本量级估算

| 层 | 调用数 | 量级 | 备注 |
|---|---|---|---|
| L0 采集 | 0 | $0 | 纯本地 |
| L1 抽取（一次性回填） | ~2800 | 10–30 M tokens | 小模型 + JSON schema |
| L2 编译（一次性） | 100–300 | 1–3 M tokens | 中等模型 |
| L3 演化 | 偶尔 | 忽略 | 每周一次 |
| **日常增量** | 10–50/天 | 可忽略 | |

**结论：一次性回填的钱大概等于你一顿饭。** 不是成本问题，是设计问题。

---

## 10. 落地路线

### W0（一个周末）—— 只做 L0，先验证有没有金矿
```
agentwiki/
  ingest/
    codex.py       # 读 thread_history + state_5 → sessions/turns/items
    xharness.py    # 读 state/sessions/*.jsonl（1052 文件，流式）
    claude.py      # 读 ~/.claude/projects/**/*.jsonl
    redact.py      # 脱敏
    entities_regex.py
  db.py            # schema + upsert（幂等）
  cli.py           # python -m agentwiki ingest --source codex
```
**产出**：一张"知识地形图"——top 50 实体 + 时间分布 + 时长统计。
**先看这张图再决定下一步。** 很可能你会说"我原来在这上面花了这么多时间"。

### W1 —— L1 抽取
- 严格的 JSON schema prompt（kind / statement / evidence / entity）
- 100 条样本上人工核对准确率，**过不了 80% 就先修 prompt，别扩大规模**
- 全量回填

### W2 —— L2 编译 + 静态站点
- 按实体合成页面，带 inline 出处徽章
- Quartz，backlinks + 全文搜索
- 冲突渲染（`superseded_by`）

### W3 —— MCP server（闭环）
- `search / assertions / history / related`
- 接到 codex / claude 上，让它干活前先查

### 可选 W4 —— 和上一个产品合流
L0 采集层和"死时间学习"共用。`turns.duration_ms` 直接喂给等待时长预测。
**两个产品长在同一块地基上：一个向外（把等待变输入），一个向内（把历史变资产）。**

---

## 11. 一句话定位

> **「你的 agent 一直在替你写笔记，只是从来没人读。AgentWiki 把它编译成你会用的那本书。」**

备选：
- 「不是摘要，是断言。不是检索，是信念。」
- 「RAG 给你片段，wiki 给你结论。」
- 「半年 vibe coding 之后，你手里应该有一本书，而不是 142 个聊天记录。」

---

## 附：立刻可跑的侦察命令

工作区里已放着本次侦察脚本：`recon_schema.py`、`recon_items.py`。
