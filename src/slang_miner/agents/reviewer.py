"""Agent3 Reviewer：严格兜底，判断词条「是否真正指代游戏内事物」。

串行链路最后一棒，也是质量闸门。输入一个组装好的
:class:`~slang_miner.schema.SlangEntry`（已带类别 / 释义 / 例句 / 置信度），输出
:class:`~slang_miner.schema.ReviewerResult`（refers_in_game_entity + verdict + reason）。
verdict 取值限定 {"keep", "reject"}。

设计原则——**严格**：宁可错杀，不可放过。只有当词条确实像「指向游戏内的角色/
玩法/装备/操作/数值机制/社区用语」且证据（释义 + 例句）自洽时才 keep；否则 reject。

mock 启发式（无 API key 也能跑）核心规则（任一不满足即 reject）：
- 必须有非空释义；
- 必须有非空、且实际包含该词的原文例句（防止释义/例句对不上）；
- 上游置信度不能过低（低于阈值视为证据不足）；
- 词条本身不能是纯标点 / 过短噪声。

真实模式下：让 LLM 严格输出 JSON：{refers_in_game_entity, verdict, reason}。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from ..schema import ReviewerResult, SlangEntry
from .base import BaseAgent, parse_json

# 经验阈值：低于此置信度直接视为证据不足（严格兜底）
_MIN_CONFIDENCE = 0.3
# 词条最短长度（中文按字符数），过短易为噪声
_MIN_TERM_LEN = 2


class ReviewerAgent(BaseAgent):
    """Agent3：严格复核器 / 质量闸门。"""

    SYSTEM_PROMPT = (
        "你是游戏黑话词典的严格质检员，是流水线的最后一道关卡。给定一个候选黑话词条"
        "（含释义与原文例句），请严格判断：它是否**真正指代游戏内的事物或玩家行为**"
        "（角色/玩法/装备/操作/数值机制/社区用语），而不是无意义噪声、断句残片或与"
        "游戏无关的普通词语。\n"
        "判定从严：释义与例句必须自洽、例句须真实包含该词、证据充分才可保留。\n"
        "严格只输出 JSON，不要任何解释，格式：\n"
        '{"refers_in_game_entity": true/false, "verdict": "keep" 或 "reject", '
        '"reason": "一句话理由"}'
    )

    def review(self, entry: SlangEntry) -> ReviewerResult:
        """对单个词条做最终复核。

        Args:
            entry: 经 Agent1/Agent2 组装的词条（含 definition/example/confidence）。

        Returns:
            ReviewerResult：refers_in_game_entity + verdict(keep|reject) + reason。
        """
        user = self._build_user_prompt(entry)
        data = self._ask(user)
        if data is None:
            # 解析失败：严格起见，按 reject 兜底
            return ReviewerResult(
                term=entry.term,
                refers_in_game_entity=False,
                verdict="reject",
                reason="复核结果解析失败，按严格兜底原则拒绝。",
            )
        return self._to_result(entry.term, data)

    # ------------------------------------------------------------------ #
    # prompt 构造 / 结果归一化
    # ------------------------------------------------------------------ #
    def _build_user_prompt(self, entry: SlangEntry) -> str:
        payload = {
            "term": entry.term,
            "category": entry.category,
            "definition": entry.definition,
            "example": entry.example,
            "confidence": round(float(entry.confidence), 4),
        }
        return (
            "请严格复核下面这个黑话词条是否真正指代游戏内事物：\n"
            + json.dumps(payload, ensure_ascii=False)
        )

    def _to_result(self, term: str, data: Dict[str, Any]) -> ReviewerResult:
        """归一化为 ReviewerResult，并校验 verdict 取值。"""
        refers = bool(data.get("refers_in_game_entity", False))
        verdict = str(data.get("verdict", "reject")).lower().strip()
        if verdict not in ("keep", "reject"):
            # 非法裁决一律按 reject 处理（严格兜底）
            verdict = "reject"
        reason = str(data.get("reason", "")).strip() or "未提供理由。"
        # 一致性约束：判定不指代游戏内事物，就不允许 keep
        if not refers and verdict == "keep":
            verdict = "reject"
            reason = f"判定不指代游戏内事物，强制拒绝。原因：{reason}"
        return ReviewerResult(
            term=term,
            refers_in_game_entity=refers,
            verdict=verdict,
            reason=reason,
        )

    # ------------------------------------------------------------------ #
    # mock 启发式
    # ------------------------------------------------------------------ #
    def _mock_logic(self, user: str) -> str:
        data = parse_json(user) or {}
        term = str(data.get("term", "")).strip()
        definition = str(data.get("definition", "")).strip()
        example = str(data.get("example", "")).strip()
        try:
            confidence = float(data.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0

        ok, reason = self._judge(term, definition, example, confidence)
        return json.dumps(
            {
                "refers_in_game_entity": ok,
                "verdict": "keep" if ok else "reject",
                "reason": reason,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _judge(
        term: str, definition: str, example: str, confidence: float
    ) -> "tuple[bool, str]":
        """严格判定逻辑：任一条件不满足即 reject，并给出具体理由。"""
        # 1) 词条本身有效性
        if not term or len(term) < _MIN_TERM_LEN:
            return False, "词条过短或为空，疑似噪声/断句残片。"
        if re.fullmatch(r"[\W_]+", term):
            return False, "词条为纯标点/符号，非有效黑话。"

        # 2) 必须有释义
        if not definition:
            return False, "缺少释义，证据不足。"

        # 3) 必须有例句，且例句须真实包含该词
        if not example:
            return False, "缺少原文例句，无法佐证指代关系。"
        if term not in example:
            return False, "例句未包含该词，释义与例句对不上。"

        # 4) 上游置信度不能过低
        if confidence < _MIN_CONFIDENCE:
            return False, f"上游置信度 {confidence:.2f} 过低（< {_MIN_CONFIDENCE}），证据不足。"

        return True, "释义与例句自洽，确为指代游戏内事物的黑话，保留。"


__all__ = ["ReviewerAgent"]
