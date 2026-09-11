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
python -m pytest tests -q            # 单元测试（脱敏规则回归，7 例）
python tools/prepublish_scan.py      # 发布前扫描工作树
python tools/verify_remote.py        # 扫描 GitHub 上真实发布的内容
```

MIT License. 见 [`LICENSE`](LICENSE)。

## 下一步（W1）

`claims` / `pages` / `links` 表已在 `db.py` 建好，无需迁移。W1 只需：

1. 用 `iter_entities` 的产物做种子，对 `final_answer` + `userMessage` 跑 LLM 抽 claim。
2. **强制 `evidence` 非空**，无原文佐证的断言直接丢弃。
3. 100 条样本上人工核对，准确率过 80% 再全量。
