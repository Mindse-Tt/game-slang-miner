---
name: game-slang-miner
description: 从游戏/社区玩家评论里自动挖掘「黑话」(梗/缩写/俗称)并产出可人工审核的词典周报。当用户说"挖黑话/挖掘黑话/整理玩家黑话/从评论里找新词/社区术语挖掘/给我做个黑话词表"，或提供一批评论/一个评论文件要找其中的黑话时使用。链路:N-gram+PMI+左右熵挖候选 → 3个Agent串行(分类→释义→兜底) → 导出审核xlsx → 人工确认回灌词典。默认离线mock零依赖可跑，配 API key 可切真实 LLM。
---

# game-slang-miner · 游戏黑话自动挖掘 Skill

把"游戏玩家社区黑话自动挖掘流水线 v2"包成一个可在 Claude Code 里直接调用的技能。
仓库与实现:https://github.com/Mindse-Tt/game-slang-miner

## 何时用本 Skill
- 用户给一批玩家/社区评论(或评论文件、或要从某来源拿评论),想**找出其中的黑话/梗/缩写/俗称**并整理成词典。
- 用户说:挖黑话、黑话筛选、社区新词挖掘、整理玩家术语、做个黑话词表、评论里有哪些黑话。
- 不只游戏:电商行业黑话、垂直圈层术语同理可用(换数据源 + 实体过滤表)。

## 前置:定位/安装
1. 若本机已 clone 仓库,`cd` 进去;否则
   `git clone https://github.com/Mindse-Tt/game-slang-miner.git && cd game-slang-miner`
2. 安装(轻量,3 个依赖): `pip install -e .`
3. 自带样例数据,可直接验证: `slang-miner run`

## 标准工作流(SOP)
1. **准备输入三源**(对应 `data/`):
   - `data/samples/comments.jsonl` — 玩家评论,每行 `{"id","source","text","ts"}`。把用户给的评论写成这个格式。
   - `data/knowledge/entities.csv` — `term,type`,精灵名/技能名/官方术语**过滤表**(这些不算黑话,自动排除)。
   - `data/seeds/known_slang.csv` — `term,definition,category`,已确认黑话种子(也是回灌目标)。
2. **端到端运行**: `slang-miner run`
   - 流程:挖掘(N-gram+PMI+左右熵,过滤专有名词)→ Agent1 Classifier(是不是黑话?7类)→ Agent2 Definer(释义+原文例句)→ Agent3 Reviewer(是否真指代游戏内事物? keep/reject 兜底)。
   - 产出: `outputs/review_<时间戳>.xlsx`(待人工审核词条)。
3. **把结果给用户看**:读出 xlsx 的"待审核黑话" sheet,汇报终审通过词数 + 列出 词/类别/释义/例句/置信度。
4. **可选·只挖不审**: `slang-miner mine` → `outputs/candidates.json`(调参/复核挖掘质量用)。
5. **可选·闭环回灌**:用户在 xlsx 里填好 `correct/modified/incorrect` 后,
   `slang-miner feedback outputs/review_<时间戳>.xlsx` → 确认词回灌种子词典,下一轮更准。

## 切换真实 LLM(更准,可选)
默认 `config/config.yaml` 的 `llm.provider: mock`(无需 key)。要更准:
```yaml
llm: { provider: "anthropic", model: "", offline: false }   # 或 openai
```
并 `export ANTHROPIC_API_KEY=...`(或 `OPENAI_API_KEY`),`pip install -e ".[anthropic]"`。
缺 key 或调用失败会**自动降级 mock 并告警**,不中断。

## 作为库调用(需要嵌进别的脚本时)
```python
from slang_miner import run_pipeline, export_review_xlsx, Comment
comments = [Comment(id="1", source="bilibili", text="打本必带奶妈，非酋本酋")]
cfg = {"mining": {"min_freq":1,"min_pmi":0.0,"min_entropy":0.0},
       "llm": {"provider":"mock","offline":True}, "paths": {}}
entries = run_pipeline(comments, cfg)
export_review_xlsx(entries, "review.xlsx")
```

## 关键参数(config/config.yaml → mining)
- `min_freq`(默认3):候选最低频次;评论少时调到 1。
- `min_pmi`/`min_entropy`:凝固度/边界自由度阈值,越高越严。
- `max_candidates`:候选上限。
调参原则:**召回优先靠放宽阈值,准确度交给 3 Agent + 人工兜底**。

## 安装为常驻 Skill(可选)
把本目录复制到 `~/.claude/skills/game-slang-miner/`,即可在任意会话里被自动触发。
