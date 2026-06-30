"""Agent1 Classifier：判断候选词「是不是黑话」并做 7 类分类。

串行链路中的第一棒。输入一个 :class:`~slang_miner.schema.Candidate`，输出
:class:`~slang_miner.schema.ClassifierResult`（is_slang + category + confidence）。

mock 启发式（无 API key 也能跑）核心规则：
- 候选词若命中常见黑话「形态特征 / 关键字」→ 判为黑话，并据特征归到 7 类之一；
- 候选词的挖掘得分（score）越高、频次越高 → confidence 越高；
- 否则判为「不是黑话」，category 给「其他」，低 confidence。

真实模式下：让 LLM 严格输出 JSON：{is_slang, category, confidence}。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from ..schema import Candidate, ClassifierResult, SlangCategory
from .base import BaseAgent, LLMClient

# ----------------------------------------------------------------------------
# mock 启发式所用的「类别关键词表」。
# 每一类给一组高判别力的形态/字符特征，命中即倾向于该类。
# 顺序即优先级（靠前的类别优先匹配），覆盖不到则归「其他」。
# ----------------------------------------------------------------------------
_CATEGORY_KEYWORDS: List[Tuple[SlangCategory, Tuple[str, ...]]] = [
    (SlangCategory.NUMERIC_MECHANIC, ("欧皇", "非酋", "脸黑", "保底", "歪", "真伤", "暴击", "概率")),
    (SlangCategory.OPERATION_SKILL, ("秒", "一套", "连招", "带走", "风筝", "拉怪", "越塔", "集火", "突进")),
    (SlangCategory.EQUIPMENT_ITEM, ("神装", "毕业装", "红装", "装备", "碎片", "道具")),
    (SlangCategory.ROLE_NICKNAME, ("奶妈", "肉盾", "坦克", "脆皮", "输出", "工具人", "混子", "之子")),
    (SlangCategory.GAMEPLAY_TERM, ("打本", "速刷", "搬砖", "肝", "氪", "毕业", "养老", "副本", "刷")),
    (SlangCategory.COMMUNITY_MEME, ("白嫖", "吃书", "梗", "哭", "笑")),
]


class ClassifierAgent(BaseAgent):
    """Agent1：黑话分类器。"""

    SYSTEM_PROMPT = (
        "你是游戏玩家社区「黑话」识别专家。给定一个从玩家评论中自动挖掘出的候选词"
        "及其例句，判断它是否为玩家黑话（区别于官方术语和普通词语），并归入 7 类之一："
        "角色称呼 / 玩法术语 / 装备道具 / 操作技巧 / 数值机制 / 社区梗缩写 / 其他。\n"
        "黑话的特征：玩家自创、约定俗成、字面义与实际所指有偏差、在社区高频使用。\n"
        "严格只输出 JSON，不要任何解释，格式：\n"
        '{"is_slang": true/false, "category": "上述7类之一的中文", "confidence": 0~1的小数}'
    )

    def classify(self, candidate: Candidate) -> ClassifierResult:
        """对单个候选词做分类判定。

        Args:
            candidate: 自动挖掘阶段产出的候选词（含 freq/score/examples）。

        Returns:
            ClassifierResult：is_slang + category + confidence。
        """
        user = self._build_user_prompt(candidate)
        data = self._ask(user)
        if data is None:
            # 解析失败的领域兜底：保守判为非黑话、低置信
            return ClassifierResult(
                term=candidate.term,
                is_slang=False,
                category=SlangCategory.OTHER.value,
                confidence=0.0,
            )
        return self._to_result(candidate.term, data)

    # ------------------------------------------------------------------ #
    # prompt 构造 / 结果归一化
    # ------------------------------------------------------------------ #
    def _build_user_prompt(self, candidate: Candidate) -> str:
        """构造 user 消息：用 JSON 携带候选词全部特征，便于真实/мock 双模解析。"""
        payload = {
            "term": candidate.term,
            "freq": candidate.freq,
            "pmi": round(candidate.pmi, 4),
            "left_entropy": round(candidate.left_entropy, 4),
            "right_entropy": round(candidate.right_entropy, 4),
            "score": round(candidate.score, 4),
            "examples": candidate.examples[:5],
        }
        return (
            "请判断下面这个候选词是不是玩家黑话并分类：\n"
            + json.dumps(payload, ensure_ascii=False)
        )

    def _to_result(self, term: str, data: Dict[str, Any]) -> ClassifierResult:
        """把解析出的 dict 归一化为 ClassifierResult，并做取值校验。"""
        is_slang = bool(data.get("is_slang", False))
        category = str(data.get("category", SlangCategory.OTHER.value))
        # 校验 category 合法性，非法值回退「其他」
        if category not in {c.value for c in SlangCategory}:
            category = SlangCategory.OTHER.value
        confidence = self._clamp(data.get("confidence", 0.0))
        return ClassifierResult(
            term=term,
            is_slang=is_slang,
            category=category,
            confidence=confidence,
        )

    @staticmethod
    def _clamp(value: Any) -> float:
        """把任意输入收敛到 [0, 1] 的浮点置信度。"""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, v))

    # ------------------------------------------------------------------ #
    # mock 启发式
    # ------------------------------------------------------------------ #
    def _mock_logic(self, user: str) -> str:
        """离线启发式：基于关键词形态 + 挖掘得分给出分类与置信度。"""
        payload = self._extract_payload(user)
        term = payload.get("term", "")
        score = float(payload.get("score", 0.0) or 0.0)
        freq = int(payload.get("freq", 0) or 0)

        category, matched = self._guess_category(term)
        # 判定是否黑话：命中关键词，或频次/得分达到经验阈值
        is_slang = matched or (freq >= 3 and score >= 1.0)

        # 置信度：命中关键词给高基线，叠加 score/freq 的小幅加成
        base = 0.75 if matched else (0.4 if is_slang else 0.15)
        bonus = min(0.2, score * 0.05 + freq * 0.01)
        confidence = round(min(0.99, base + (bonus if is_slang else 0.0)), 3)

        return json.dumps(
            {
                "is_slang": is_slang,
                "category": category.value if is_slang else SlangCategory.OTHER.value,
                "confidence": confidence,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _guess_category(term: str) -> Tuple[SlangCategory, bool]:
        """按关键词表猜类别；返回 (类别, 是否命中)。未命中默认「其他」。"""
        for category, keywords in _CATEGORY_KEYWORDS:
            for kw in keywords:
                if kw in term:
                    return category, True
        return SlangCategory.OTHER, False

    @staticmethod
    def _extract_payload(user: str) -> Dict[str, Any]:
        """从 user 消息里取回 JSON payload（mock 与真实共用同一封装）。"""
        from .base import parse_json

        data = parse_json(user)
        return data if isinstance(data, dict) else {}


__all__ = ["ClassifierAgent"]
