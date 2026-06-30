# game-slang-miner · 游戏玩家社区「黑话」自动挖掘流水线 v2

> **Game-Community Slang Auto-Mining Pipeline v2**
> 从玩家评论中自动挖掘「黑话」候选 → 3 个 Agent 串行审查 → 导出人工审核周报 → 闭环回灌，越用越准。
> An end-to-end pipeline that mines community slang candidates from player comments, runs a 3-agent serial review, exports a human-review workbook, and feeds confirmed terms back into the dictionary.

[![CI](https://github.com/OWNER/game-slang-miner/actions/workflows/ci.yml/badge.svg)](../../actions)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

---

## 简介 · Overview

游戏运营 / 内容团队每天面对海量玩家评论，里面充斥着官方词典查不到的「黑话」：
`御三家`、`奶妈`、`非酋`、`打本`、`白嫖`…… 人工逐条整理既慢又容易遗漏。

本项目把这件事做成一条**可复现、可离线跑通、可闭环迭代**的流水线：

1. **自动挖掘**：用 `N-gram + PMI(点互信息) + 左右邻接熵` 从评论里挑「长得像词的新串」，
   并自动过滤掉精灵名 / 技能名 / 官方术语；
2. **3 个 Agent 串行审查**（产品核心）：`Classifier`（是不是黑话？7 类分类）→
   `Definer`（什么意思？给释义 + 原文例句）→ `Reviewer`（是否真指代游戏内事物？严格兜底 keep/reject）；
3. **输出 + 人工审核**：导出周报 `xlsx`，运营只需在下拉里填 `correct / modified / incorrect`；
4. **闭环回灌**：人工确认的词回灌「已确认黑话词典」，下一轮自动过滤更干净、Agent 判断更准。

> 关键设计：**所有 LLM 调用都支持离线 mock 模式**——无任何 API key、无网络也能一条命令端到端跑通；
> 配置环境变量后可一键切换到真实模型（Anthropic / OpenAI）。

---

## 流水线全景图 · Pipeline at a Glance

```text
┌──────────────────────────── 输入三源 / 3 Inputs ────────────────────────────┐
│  ① 玩家评论  comments.jsonl   (B站 / 贴吧 / TapTap)                          │
│  ② 精灵名+技能名  entities.csv (公司知识库专有名词 → 过滤表)                 │
│  ③ 已确认黑话种子  known_slang.csv (上一轮回灌的结果)                        │
└───────────────┬─────────────────────────────────────────────────────────────┘
                │
                ▼
┌──────────────────────────── 自动挖掘 / Auto-Mining ─────────────────────────┐
│  字级 N-gram 枚举  →  频次过滤                                               │
│        │                                                                     │
│        ├─ PMI 点互信息   (内部凝固度：各部分是否真的爱黏在一起)              │
│        ├─ 左熵 / 右熵    (边界自由度：左右能不能接多种字)                    │
│        └─ 综合 score 排序                                                    │
│  →  过滤精灵名/技能名/官方术语(②)  →  截断 max_candidates                    │
│  →  候选 Candidate{ term, freq, pmi, left/right_entropy, score, examples }   │
└───────────────┬─────────────────────────────────────────────────────────────┘
                │  候选 N 个
                ▼
┌─────────────── 3 个 Agent 串行审查 / 3-Agent Serial Review ─────────────────┐
│                                                                              │
│   Agent1 Classifier   ── 是不是黑话？归入 7 类 ──┐  非黑话直接淘汰(短路)     │
│                                                  ▼                           │
│   Agent2 Definer      ── 什么意思？释义 + 原文例句 ──┐                       │
│                                                      ▼                       │
│   Agent3 Reviewer     ── 是否真指代游戏内事物？ keep / reject (严格兜底)     │
│                                                  reject 直接淘汰             │
└───────────────┬─────────────────────────────────────────────────────────────┘
                │  keep 的词条 SlangEntry(status="pending")
                ▼
┌──────────────────────── 输出 + 人工审核 / Review ───────────────────────────┐
│  导出周报 review_<时间戳>.xlsx                                              │
│  运营逐条在下拉填： correct / modified / incorrect                          │
└───────────────┬─────────────────────────────────────────────────────────────┘
                │  人工填好的 xlsx
                ▼
┌──────────────────────── 闭环回灌 / Feedback Loop ───────────────────────────┐
│  correct / modified 的词  →  回灌「已确认黑话词典」 known_slang.csv (③)      │
│  下一轮挖掘自动过滤这些词 + Agent 拿到更多种子  →  越用越准  ───┐            │
└────────────────────────────────────────────────────────────────┘            │
        ▲                                                                       │
        └───────────────────────────────────────────────────────────────────────┘
                                （闭环 / closed loop）
```

<details>
<summary>同一张图的 Mermaid 版本（GitHub 可直接渲染）</summary>

```mermaid
flowchart TD
    subgraph IN["输入三源 / Inputs"]
        A1["① 玩家评论<br/>comments.jsonl"]
        A2["② 精灵名+技能名<br/>entities.csv（过滤表）"]
        A3["③ 已确认黑话种子<br/>known_slang.csv"]
    end

    subgraph MINE["自动挖掘 / Auto-Mining"]
        M1["字级 N-gram + 频次过滤"]
        M2["PMI 点互信息<br/>左熵 / 右熵"]
        M3["综合 score 排序 +<br/>过滤专有名词 + 截断"]
        M1 --> M2 --> M3
    end

    subgraph AGENTS["3 个 Agent 串行审查（核心）"]
        G1["Agent1 Classifier<br/>是不是黑话？7 类分类"]
        G2["Agent2 Definer<br/>释义 + 原文例句"]
        G3["Agent3 Reviewer<br/>keep / reject（严格兜底）"]
        G1 -- "是黑话" --> G2 --> G3
        G1 -. "非黑话淘汰" .-> X1[淘汰]
        G3 -. "reject 淘汰" .-> X2[淘汰]
    end

    XLSX["输出周报 review.xlsx<br/>+ 人工审核 correct/modified/incorrect"]
    SEED["闭环回灌：confirmed 词<br/>→ known_slang.csv"]

    A1 --> M1
    A2 --> M3
    A3 --> M3
    M3 -->|候选 Candidate| G1
    G3 -->|keep 词条 SlangEntry| XLSX
    XLSX --> SEED
    SEED -.->|下一轮自动过滤 + 更准| A3
```
</details>

---

## 效率对比 · End-to-End vs. Manual

| 维度 | 纯人工整理 | 本流水线（端到端 + 人工只做审核） |
| --- | --- | --- |
| 候选发现 | 逐条读评论、凭记忆挑词，易漏 | 统计指标自动挖掘，高频新词无遗漏、可复现 |
| 专有名词过滤 | 人工记住所有精灵/技能名 | `entities.csv` 自动过滤，含子串碎片 |
| 分类 + 释义 | 逐词查证、手写释义 | 3 Agent 串行自动产出初稿，运营只做「判对错」 |
| 质量兜底 | 全靠人盯 | Reviewer 严格闸门，宁可错杀不放过 |
| 人工动作 | 从 0 整理 | **只在 xlsx 下拉里点 correct/modified/incorrect** |
| 越用越准 | 经验在个人脑子里 | 确认词自动回灌，下一轮过滤 + Agent 越来越准 |

> 一句话：**机器把「读评论 → 挖词 → 分类 → 释义 → 兜底」全包了，人只做最后一步「判定」**，
> 并且每一次判定都沉淀进词典，形成正反馈。

---

## 安装 · Installation

需要 Python **3.9+**。

```bash
git clone https://github.com/OWNER/game-slang-miner.git
cd game-slang-miner

python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -e .          # 装上控制台脚本 slang-miner（src 布局）
```

运行时依赖只有三个轻量包：`jieba`（中文切分辅助）、`openpyxl`（xlsx 读写）、`pyyaml`（读配置）。
真实 LLM provider 为**可选**依赖，离线 mock 模式不需要安装。

---

## 快速开始 · Quick Start（离线 mock，一条命令端到端）

仓库自带样例数据（60 条评论 / 44 条过滤词 / 34 条种子），开箱即跑，**无需任何 API key**：

```bash
slang-miner run
```

预期输出（节选）：

```text
[流水线] 端到端流水线启动
[流水线]   LLM 模式：provider=mock offline=True
[流水线] 第 1 步｜读取评论
[流水线]   评论数：60
[流水线] 第 2 步｜自动挖掘 + 第 3 步｜Agent1 分类 → Agent2 释义 → Agent3 终审
[流水线]   终审通过（待人工审核）词条数：17
[流水线] 第 4 步｜导出周报 xlsx（供人工填写 correct/modified/incorrect）
[流水线] 完成｜review 周报已写入：.../outputs/review_<时间戳>.xlsx
```

三个子命令（对应流程图三段）：

```bash
# 只做自动挖掘，把候选存成 json（便于调参 / 复核挖掘质量）
slang-miner mine

# 端到端：挖掘 → 3 Agent 串行 → 导出 review 周报 xlsx
slang-miner run

# 闭环回灌：把人工填好的 xlsx 中 correct/modified 的词回灌种子词典
slang-miner feedback outputs/review_<时间戳>.xlsx
```

也可作为库直接调用：

```python
from slang_miner import run_pipeline, export_review_xlsx, Comment

comments = [Comment(id="1", source="bilibili", text="打本必带奶妈，非酋本酋")]
cfg = {"mining": {"min_freq": 1, "min_pmi": 0.0, "min_entropy": 0.0},
       "llm": {"provider": "mock", "offline": True}, "paths": {}}

entries = run_pipeline(comments, cfg)        # 含挖掘 + 3 Agent 串行
export_review_xlsx(entries, "review.xlsx")   # 导出人工审核周报
```

---

## 配置说明 · Configuration

默认读取 `config/config.yaml`，可用 `slang-miner --config <path> <command>` 覆盖。

```yaml
mining:                 # 自动挖掘参数（N-gram + PMI + 左右熵）
  ngram_min: 2          # 最短 n-gram（字数）
  ngram_max: 4          # 最长 n-gram（字数）
  min_freq: 3           # 候选最低频次（低频统计不可靠）
  min_pmi: 1.0          # 候选最低 PMI（内部凝固度阈值）
  min_entropy: 1.0      # 左右熵较小值的阈值（边界自由度）
  max_candidates: 3000  # 输出候选上限

llm:                    # LLM 配置
  provider: "mock"      # mock | anthropic | openai
  model: ""             # 真实模型名（provider != mock 时填，留空用默认）
  offline: true         # true=强制离线 mock；false=走真实 API

paths:                  # 输入 / 输出（相对路径相对仓库根，运行时解析为绝对路径）
  comments: "data/samples/comments.jsonl"
  entities: "data/knowledge/entities.csv"
  seeds:    "data/seeds/known_slang.csv"
  out_dir:  "outputs"
```

数据文件格式：

- `comments.jsonl`：每行一个 JSON `{"id","source","text","ts"}`；
- `entities.csv`：`term,type`（精灵 / 技能 / 官方术语过滤表）；
- `known_slang.csv`：`term,definition,category`（已确认黑话种子，也是回灌目标）。

---

## 真实 LLM 接入 · Using a Real LLM

默认离线 mock 用本地启发式规则保证可跑通；要换成真实模型，只需改配置 + 设环境变量：

```yaml
llm:
  provider: "anthropic"   # 或 "openai"
  model: ""               # 留空则用默认：anthropic→claude-opus-4-8，openai→gpt-4o
  offline: false
```

```bash
# 二选一
export ANTHROPIC_API_KEY="sk-ant-..."     # provider: anthropic
export OPENAI_API_KEY="sk-..."            # provider: openai

pip install -e ".[anthropic]"   # 或 .[openai]，按需安装 SDK
slang-miner run
```

**降级保护**：若 `offline: false` 但缺少对应 API key，或真实调用抛错，
客户端会**自动降级为 mock 并打印告警**，绝不会让整条流水线中断。

---

## 目录结构 · Project Layout

```text
game-slang-miner/
├── README.md
├── pyproject.toml                 # 打包 / 控制台脚本 / pytest 配置（src 布局）
├── requirements.txt
├── config/config.yaml             # 全局配置
├── data/
│   ├── samples/comments.jsonl     # 样例玩家评论
│   ├── knowledge/entities.csv     # 精灵名/技能名/官方术语 过滤表
│   └── seeds/known_slang.csv      # 已确认黑话种子（也是回灌目标）
├── src/slang_miner/
│   ├── __init__.py                # 公共 API 导出
│   ├── schema.py                  # 数据契约（唯一真相源）
│   ├── mining/                    # 自动挖掘
│   │   ├── ngram.py               #   字级 N-gram 抽取
│   │   ├── pmi.py                 #   点互信息（内部凝固度）
│   │   ├── entropy.py             #   左右邻接熵（边界自由度）
│   │   └── miner.py               #   编排：mine(comments, entities, config)
│   ├── agents/                    # 3 个串行 Agent（核心）
│   │   ├── base.py                #   LLMClient / BaseAgent / parse_json
│   │   ├── classifier.py          #   Agent1 ClassifierAgent
│   │   ├── definer.py             #   Agent2 DefinerAgent
│   │   └── reviewer.py            #   Agent3 ReviewerAgent
│   ├── pipeline.py                # 端到端编排 run_pipeline / run_agents
│   ├── review/exporter.py         # 周报 xlsx 导出 / 回读
│   ├── feedback.py                # 闭环回灌 ingest_reviewed
│   └── cli.py                     # 命令行入口（mine / run / feedback）
├── tests/test_smoke.py            # 离线端到端冒烟测试
└── .github/workflows/ci.yml       # CI：pip install + pytest
```

### 核心数据契约 · Core Contracts（`schema.py`）

| 类型 | 说明 |
| --- | --- |
| `SlangCategory` | 7 类枚举：角色称呼 / 玩法术语 / 装备道具 / 操作技巧 / 数值机制 / 社区梗缩写 / 其他 |
| `Comment` | 输入评论 `{id, source, text, ts}` |
| `Candidate` | 挖掘候选 `{term, freq, pmi, left_entropy, right_entropy, score, examples}` |
| `ClassifierResult` | Agent1 输出 `{term, is_slang, category, confidence}` |
| `DefinerResult` | Agent2 输出 `{term, definition, example}` |
| `ReviewerResult` | Agent3 输出 `{term, refers_in_game_entity, verdict(keep/reject), reason}` |
| `SlangEntry` | 最终词条 `{term, category, definition, example, confidence, sources, status}` |

---

## 测试 · Testing

```bash
pytest -v                                  # src 布局已在 pyproject 配好 pythonpath
pytest --cov=src --cov-report=term-missing # 覆盖率
```

`tests/test_smoke.py` 在离线 mock 下端到端验证：挖掘产出候选并过滤专有名词、
三 Agent 串行产出合法词条、xlsx 导出 + 回读、闭环回灌只收 correct/modified。

---

## 路线图 · Roadmap

- [ ] 挖掘：把 `score` 权重与停用词表外移到 config，支持按游戏调参。
- [ ] Agent：支持 prompt 模板热加载、批量调用与提示缓存以降本提速。
- [ ] 输出：周报增加「本轮新增 vs 历史」对比页、按类别分 sheet。
- [ ] 闭环：记录每个词的审核历史与版本，支持回滚与审计。
- [ ] 数据源：内置 B站 / 贴吧 / TapTap 评论采集适配器（可选、合规）。
- [ ] 评测：构造黄金集，量化挖掘召回率与 Agent 准确率，做回归看护。

---

## License

MIT
