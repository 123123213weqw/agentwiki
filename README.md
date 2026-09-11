# AgentWiki

**把你的 agent 会话编译成个人知识 wiki。**

L0 已实现并跑通：把本机所有 agent 会话（Codex / Claude Code / xharness）归一化进一个
sqlite 库，并用纯正则抽出知识图谱的**骨架**。**零 LLM 调用，零成本，可反复运行。**

它只回答一个问题：**值不值得为这些数据花钱做 LLM 抽取？**

完整设计见 [`DESIGN.md`](DESIGN.md)。

---

## 快速开始

```bash
python -m agentwiki ingest --source all   # 采集全部来源（约 25 秒）
python -m agentwiki terrain               # 生成知识地形图
python -m agentwiki export                # 导出 wiki.json（UI 的数据接缝）
python -m agentwiki serve --open          # 起本地 UI（http://127.0.0.1:8787）
python -m agentwiki stats                 # 看库内容摘要
python -m agentwiki shell                 # 交互式 SQL
```

产出：`~/.agentwiki/reports/terrain.md` + `~/.agentwiki/wiki.db`

## 实测结果（本机）

| 指标 | 值 |
|---|---|
| 会话 sessions | 161 |
| 轮次 turns | 2,903 |
| 条目 items | 62,331 |
| 正文字符 | 21,184,827 |
| 正则实体 entities | 5,527 |

| 来源 | 会话 | items | 实现 |
|---|---|---|---|
| codex | 88 | 18,196 | `thread_history_1.sqlite` + `state_5.sqlite` |
| claude | 59 | 38,923 | `~/.claude/projects/**/*.jsonl` |
| xharness | 14 | 5,239 | `state/sessions/*.jsonl` |

等待时长分布（p50 **1.1 分钟**，p90 **11.8 分钟**）——直接喂给"死时间学习"产品。

---

## Web UI

```bash
python -m agentwiki export      # sqlite -> ~/.agentwiki/web/wiki.json
python -m agentwiki serve       # http://127.0.0.1:8787（只绑 localhost）
```

UI 是**零构建**的静态页面（`web/`，原生 JS + CSS，无 npm、无框架），由标准库
http 服务器喂数据。四个页面：概览（地形 / 等待时长 / 时间线）、实体、会话、图谱。

设计上有一条硬边界：

> **接缝是一个文件，不是一次导入。** `export.py` 把库压成一个 `wiki.json`，
> UI 只认这个文件。前端换语言、换框架都不影响核心；核心也**永远不会**长出
> 对展示层的依赖。

`wiki.json` 是**有意封顶**的（默认前 400 实体 × 3 条片段，实测 0.57 MB）。
227 MB 的库不可能也不应该变成 227 MB 的 JSON：UI 要的是地形**形状**，加上足够
的**出处**让人点得下去，而不是每个字节。所有上限都是 `export.py` 的参数。

### 选哪 400 个：这是整个 UI 最容易做错的地方

封顶意味着**"没被选中" = "在 UI 里搜不到"**，不是"排在后面"。所以选择规则
决定了你能看见什么。踩过的三个坑，都实测过：

1. **按提及次数排序是错的。** 文件名在每次工具调用里都被提到，会在**一两个会话内**
   堆到几千次；而真正贯穿你工作的东西分散在很多会话里。

   | | 提及 | 会话 |
   |---|---|---|
   | `d:/kaiwu_15.0.1_202604071608` | 1767 | **2** |
   | `github.com` | 767 | **65** |

   所以主排序用**广度**（跨多少会话），不用原始频次。

2. **光换排序指标不够。** 文件实体有 4107 个，靠排序压不下去：实测
   `file+path` 只从 90.5% 降到 **80.5%**。必须有**每类配额**。

3. **配额不能按池子顺序发。** 高广度的类型会在第一轮就填满上限并
   `return`，排在后面的稀有类型（错误）永远拿不到配额。真实数据里恰好没暴露
   （400 > 350 配额和），是测试抓出来的。改成**轮转发**后才对任意上限成立。

最终规则：**配额按广度发，剩余名额按提及数发**——两个信号各有一个发声渠道，
因为"广而浅"和"窄而深"都是有意义的知识，只用一个就会静默删掉另一种。

| | 修之前 | 修之后 |
|---|---|---|
| `file+path` | 90.5% | **38.8%** |
| 错误实体 | **1** | **55**（全部） |
| PR / issue | 3 / 3 | 40 / 35 |

> 榜首因此从"被 grep 最多的路径"变成 `github.com`(65 会话)、`memory.md`(25)、
> `skill.md`(20)——真正贯穿你工作的东西。

四条 wiki 要求里，L0 已经给到 **出处（provenance）**：每条片段都带
`session_id` + `item_id`，所以页面上任何一句话都能追回原始条目。

**反向链接**在客户端从 `session.entity_ids` 现算，而不是导出预计算的邻接表——
更小，且前端能自由派生共现、按仓库聚合等任意视图。

> 维护提示：`web/app.js` 输出什么类名，`web/styles.css` 就必须定义什么。两者
> 手工维护会**静默漂移**——页面照样能打开，但元素丢了布局（统计卡片会竖着堆成
> 一列，而且没有任何报错）。`tools/class_audit.py` 把这个 bug 变响；它自己能被
> 注入的缺失类名验证，所以它报 OK 是可信的。

---

## 安全设计（重要）

数据源旁边就是 `~/.codex/auth.json` 和 `.sandbox-secrets/`，所以：

1. **白名单**：只读 `config.py` 里显式列出的文件，绝不 glob `~/.codex`。
2. **快照**：别人的 sqlite 先复制再读（`HAS_WAL` 库只读打开不可靠，且可能与活进程抢锁）。
3. **统一脱敏关卡**：`db.insert_item()` 是 item 写入的唯一入口，`text` 和 `raw_json`
   **都**过 `redact.scrub()`。早期的 bug 正是只脱敏了 `text`，导致 `raw_json` 泄露真实密钥。
4. **`\b` 陷阱**：Python 正则里中文属于 `\w`，所以 `嗯sk-xxx` 中 `\bsk-` **永不匹配**。
   所有 token 规则改用 ASCII 后顾断言 `(?<![A-Za-z0-9_])`。
5. **发布前自检**：`tools/prepublish_scan.py` 扫工作树，`tools/verify_remote.py` 拉取
   **GitHub 上真实发布的内容**重扫（只信本地提交是不够的——push hook、remote 配错、
   分支搞错都会让发布内容与扫描内容不一致）。`tools/audit.py` 查库内完整性，
   `tools/find_needle.py <串>` 定位任意字符串在库里的位置。

   ⚠️ 写这类扫描器时踩过的坑：把真实用户名放进 `ALLOWLIST`（等于放行）、
   路径正则要双反斜杠（匹配不到单反斜杠的真实路径）——**安全工具误报 CLEAN
   比不检查更危险**，所以它必须自己能被验证（拿已知的泄露样本喂它）。

> 自我反馈环：调试 wiki 的过程本身会被 xharness 记录并回采。调试时不要把密钥原文粘进对话。

## 已知限制

- `project_root()` 是启发式（跳过容器目录后取一层）。`C:\Users\me\OneDrive\文档\New project`
  只能得到 `c:/文档`。**权威的项目归属请用 `sessions.cwd` / `sessions.repo` 表**，那里是精确值。
- `reasoning` 类 item 大多 `summary` 为空，是噪音；L1 应只取 `agentMessage.phase == 'final_answer'`。
- 长会话有 `contextCompaction` 标记（134 处），其上下文是**残缺**的，不要当成原始陈述。
- codex 的 `thread_history_1.sqlite` 是 codex 自己的投影，字段随版本变化；
  `thread_history_projection_state` 里有它的消费游标，可搭车做增量。

## 开发 / 测试

```bash
python -m pytest tests -q            # 单元测试（脱敏 7 例 + 导出契约 8 例）
python tools/class_audit.py          # 校验 CSS 类名 + CSS 变量（防 UI 静默漂移）
python tools/check_contract.py       # 校验 wiki.json 契约与引用完整性
python tools/render_check.py         # 真浏览器打开每条路由，确认真的画出来了
python tools/rank_compare.py         # 对比"提及数"与"广度"两种排序
python tools/prepublish_scan.py      # 发布前扫描工作树
python tools/verify_remote.py        # 扫描 GitHub 上真实发布的内容
```

⚠️ `node --check` **只查语法**。它看不见"引用了一个另一作用域里的变量"这类错误——
页面会白屏卡在"正在加载"，而语法检查返回 0。`tools/render_check.py` 就是为此存在的
（它自己也被注入过同一个 bug 验证过）。

MIT License. 见 [`LICENSE`](LICENSE)。

## 下一步（W1）

`claims` / `pages` / `links` 表已在 `db.py` 建好，无需迁移。W1 只需：

1. 用 `iter_entities` 的产物做种子，对 `final_answer` + `userMessage` 跑 LLM 抽 claim。
2. **强制 `evidence` 非空**，无原文佐证的断言直接丢弃。
3. 100 条样本上人工核对，准确率过 80% 再全量。
