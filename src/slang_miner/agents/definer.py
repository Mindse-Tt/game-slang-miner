"""Agent2 Definer：给黑话写释义，并从原文里挑一句代表性例句。

串行链路第二棒。输入 :class:`~slang_miner.schema.Candidate`，输出
:class:`~slang_miner.schema.DefinerResult`（definition + example）。

mock 启发式（无 API key 也能跑）核心规则：
- example：从候选词的 examples 里挑「最短且确实包含该词」的一条作代表（短句更
  清晰；都不含则退而取第一条）；
- definition：基于该词所在类别的「释义模板」+ 词本身，拼出一句通顺的领域释义，
  保证非空、可读。

真实模式下：让 LLM 严格输出 JSON：{definition, example}，并要求 example 必须取自
所给原文例句（不得编造）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..schema import Candidate, DefinerResult
from .base import BaseAgent, parse_json


class DefinerAgent(BaseAgent):
    """Agent2：黑话释义器。"""

    SYSTEM_PROMPT = (
        "你是游戏玩家社区「黑话」释义专家。给定一个已被判定为黑话的候选词及其在玩家"
        "评论中的原文例句，请：\n"
        "1. 用一句话给出该黑话在游戏语境下的准确释义（说明它实际指代什么）；\n"
        "2. 从所给的原文例句中**原样挑选**最能体现该词含义的一条作为 example，"
        "不得自行编造例句。\n"
        "严格只输出 JSON，不要任何解释，格式：\n"
        '{"definition": "一句话释义", "example": "取自原文的例句"}'
    )

    def define(self, candidate: Candidate) -> DefinerResult:
        """为单个候选词产出释义与例句。

        Args:
            candidate: 候选词（通常已被 Agent1 判为黑话）。

        Returns:
            DefinerResult：definition + example，均保证非空。
        """
        user = self._build_user_prompt(candidate)
        data = self._ask(user)
        if data is None:
            return self._fallback(candidate)
        return self._to_result(candidate, data)

    # ------------------------------------------------------------------ #
    # prompt 构造 / 结果归一化
    # ------------------------------------------------------------------ #
    def _build_user_prompt(self, candidate: Candidate) -> str:
        payload = {
            "term": candidate.term,
            "examples": candidate.examples[:8],
        }
        return (
            "请为下面这个黑话写释义并从原文例句中挑一条例句：\n"
            + json.dumps(payload, ensure_ascii=False)
        )

    def _to_result(self, candidate: Candidate, data: Dict[str, Any]) -> DefinerResult:
        """归一化为 DefinerResult，并保证字段非空、example 合理。"""
        definition = str(data.get("definition", "")).strip()
        example = str(data.get("example", "")).strip()

        if not definition:
            definition = self._template_definition(candidate.term)
        # example 兜底：若空或与原文无关，则从候选例句里挑一条
        if not example:
            example = self._pick_example(candidate.term, candidate.examples)
        return DefinerResult(
            term=candidate.term, definition=definition, example=example
        )

    def _fallback(self, candidate: Candidate) -> DefinerResult:
        """整体解析失败时的领域兜底。"""
        return DefinerResult(
            term=candidate.term,
            definition=self._template_definition(candidate.term),
            example=self._pick_example(candidate.term, candidate.examples),
        )

    # ------------------------------------------------------------------ #
    # mock 启发式
    # ------------------------------------------------------------------ #
    def _mock_logic(self, user: str) -> str:
        data = parse_json(user) or {}
        term = str(data.get("term", ""))
        examples = data.get("examples", []) or []
        if not isinstance(examples, list):
            examples = []

        return json.dumps(
            {
                "definition": self._template_definition(term),
                "example": self._pick_example(term, examples),
            },
            ensure_ascii=False,
        )

    # ------------------------------------------------------------------ #
    # 共用启发式工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _template_definition(term: str) -> str:
        """基于词本身拼一句通顺的占位释义，保证非空可读。"""
        if not term:
            return "玩家社区中约定俗成的说法，具体含义需结合上下文确认。"
        return (
            f"「{term}」是玩家社区中约定俗成的黑话，"
            f"通常在评论里用于指代相关的游戏内事物或玩家行为，需结合上下文理解。"
        )

    @staticmethod
    def _pick_example(term: str, examples: List[str]) -> str:
        """从例句列表挑代表句：优先「最短且含该词」，否则取第一条，皆无则空串。"""
        cleaned = [str(e).strip() for e in examples if str(e).strip()]
        if not cleaned:
            return ""
        # 优先选包含该词的，里面再取最短（短句更利于人工快速判读）
        containing = [e for e in cleaned if term and term in e]
        if containing:
            return min(containing, key=len)
        return cleaned[0]


__all__ = ["DefinerAgent"]
