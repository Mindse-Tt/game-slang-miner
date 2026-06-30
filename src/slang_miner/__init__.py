"""game-slang-miner：游戏玩家社区「黑话」自动挖掘流水线 v2。

业务链路（忠实于产品流程图）：

    输入三源（玩家评论 / 精灵+技能过滤表 / 已确认黑话种子）
        → 自动挖掘（N-gram + PMI + 左右熵，过滤专有名词）得到候选
        → 3 个 Agent 串行审查
            Agent1 Classifier  是不是黑话？7 类分类
            Agent2 Definer     什么意思？给释义 + 原文例句
            Agent3 Reviewer    是不是真指代游戏内事物？严格兜底 keep/reject
        → 输出周报 xlsx（人工填 correct/modified/incorrect）
        → 闭环回灌：确认词回灌种子词典，下轮自动过滤更准

本包对外暴露「一站式」公共 API，方便作为库被 import 使用；命令行入口见
:mod:`slang_miner.cli`（控制台脚本 ``slang-miner``）。
"""

from __future__ import annotations

# --- 数据契约（唯一真相源）-------------------------------------------------
from .schema import (
    Candidate,
    ClassifierResult,
    Comment,
    DefinerResult,
    ReviewerResult,
    SlangCategory,
    SlangEntry,
)

# --- 自动挖掘 ---------------------------------------------------------------
from .mining import mine

# --- 三个串行 Agent + LLM 基础设施 -----------------------------------------
from .agents import (
    BaseAgent,
    ClassifierAgent,
    DefinerAgent,
    LLMClient,
    ReviewerAgent,
    parse_json,
)

# --- 流水线编排 / 输出 / 闭环 ------------------------------------------------
from .pipeline import run_agents, run_pipeline
from .review import export_review_xlsx, import_verdicts
from .feedback import ingest_reviewed

__version__ = "0.2.0"

__all__ = [
    "__version__",
    # schema
    "SlangCategory",
    "Comment",
    "Candidate",
    "ClassifierResult",
    "DefinerResult",
    "ReviewerResult",
    "SlangEntry",
    # mining
    "mine",
    # agents
    "LLMClient",
    "BaseAgent",
    "parse_json",
    "ClassifierAgent",
    "DefinerAgent",
    "ReviewerAgent",
    # pipeline
    "run_pipeline",
    "run_agents",
    # review
    "export_review_xlsx",
    "import_verdicts",
    # feedback
    "ingest_reviewed",
]
