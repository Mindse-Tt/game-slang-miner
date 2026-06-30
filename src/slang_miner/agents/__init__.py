"""Agents 子包：3 个串行审查 Agent（本产品核心）。

串行链路：
    Agent1 ClassifierAgent  是不是黑话？7 类分类
        ↓
    Agent2 DefinerAgent     什么意思？给释义 + 原文例句
        ↓
    Agent3 ReviewerAgent    是否真指代游戏内事物？严格兜底 keep/reject

共用基础设施见 :mod:`slang_miner.agents.base`（LLMClient / BaseAgent / parse_json）。
所有 Agent 在 mock 模式下均可无 API key 端到端跑通。
"""

from __future__ import annotations

from .base import BaseAgent, LLMClient, parse_json
from .classifier import ClassifierAgent
from .definer import DefinerAgent
from .reviewer import ReviewerAgent

__all__ = [
    "LLMClient",
    "BaseAgent",
    "parse_json",
    "ClassifierAgent",
    "DefinerAgent",
    "ReviewerAgent",
]
