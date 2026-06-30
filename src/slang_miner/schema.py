"""数据契约层（schema）。

本模块定义整条流水线共享的数据结构，是所有 Agent / 挖掘模块 / 输出模块
都必须 import 使用的「唯一真相源」。字段名一经确定不得擅自改名，否则会破坏
上下游依赖。Python 3.9 兼容：使用 ``from __future__ import annotations``，
并统一用 ``typing.List`` 而非内建泛型 ``list[...]``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List


class SlangCategory(str, Enum):
    """黑话的 7 类分类标签。

    继承 ``str`` 便于直接序列化（如写入 csv/xlsx/json）时取到中文字面值。
    Agent1（Classifier）的 ``category`` 字段取值应来自本枚举。
    """

    ROLE_NICKNAME = "角色称呼"      # 对角色/精灵的玩家自创称呼，如「奶妈」「肉盾」
    GAMEPLAY_TERM = "玩法术语"      # 玩法/机制相关，如「打本」「速刷」
    EQUIPMENT_ITEM = "装备道具"     # 装备/道具相关黑话
    OPERATION_SKILL = "操作技巧"    # 操作/连招技巧，如「一套带走」「秒了」
    NUMERIC_MECHANIC = "数值机制"   # 数值/概率/机制，如「欧皇」「非酋」
    COMMUNITY_MEME = "社区梗缩写"   # 社区流行梗与缩写，如「白嫖」
    OTHER = "其他"                  # 无法归入以上任一类

    def __str__(self) -> str:  # 便于直接 print / 写文件取中文值
        return self.value


@dataclass
class Comment:
    """一条玩家评论（流水线输入的最小单元）。

    Attributes:
        id: 评论唯一标识。
        source: 来源渠道，如 "bilibili" / "tieba" / "taptap"。
        text: 评论正文。
        ts: 时间戳（ISO8601 或任意字符串，可空）。
    """

    id: str
    source: str
    text: str
    ts: str = ""


@dataclass
class Candidate:
    """自动挖掘阶段产出的「疑似黑话」候选词。

    Attributes:
        term: 候选字串。
        freq: 在语料中的出现频次。
        pmi: 点互信息（内部凝聚度，越高越像一个固定词）。
        left_entropy: 左邻字熵（左侧搭配越自由越像独立词）。
        right_entropy: 右邻字熵（右侧搭配越自由越像独立词）。
        score: 综合打分（由 freq/pmi/左右熵加权得到）。
        examples: 命中该候选词的原文例句列表。
    """

    term: str
    freq: int
    pmi: float
    left_entropy: float
    right_entropy: float
    score: float
    examples: List[str] = field(default_factory=list)


@dataclass
class ClassifierResult:
    """Agent1 Classifier 的输出：这是不是黑话？属于哪一类？

    Attributes:
        term: 被判定的候选词。
        is_slang: 是否为黑话。
        category: 分类标签（应取 ``SlangCategory`` 的某个值）。
        confidence: 置信度 [0, 1]。
    """

    term: str
    is_slang: bool
    category: str
    confidence: float


@dataclass
class DefinerResult:
    """Agent2 Definer 的输出：这个黑话是什么意思？

    Attributes:
        term: 被释义的词。
        definition: 释义文本。
        example: 取自原文的代表性例句。
    """

    term: str
    definition: str
    example: str


@dataclass
class ReviewerResult:
    """Agent3 Reviewer 的输出：是否真正指代游戏内事物？最终裁决。

    Attributes:
        term: 被复核的词。
        refers_in_game_entity: 是否真正指代游戏内的事物/概念。
        verdict: 最终裁决，取值限定为 {"keep", "reject"}。
        reason: 裁决理由。
    """

    term: str
    refers_in_game_entity: bool
    verdict: str  # 取值限定 {"keep", "reject"}
    reason: str


@dataclass
class SlangEntry:
    """最终输出 / 人工审核 / 闭环回灌共用的黑话词条。

    Attributes:
        term: 词条。
        category: 分类标签。
        definition: 释义。
        example: 例句。
        confidence: 综合置信度 [0, 1]。
        sources: 命中来源渠道列表（如 ["bilibili", "tieba"]）。
        status: 人工审核状态，取值限定 {pending, correct, modified, incorrect}。
    """

    term: str
    category: str
    definition: str
    example: str
    confidence: float
    sources: List[str] = field(default_factory=list)
    status: str = "pending"  # 取值限定 {pending, correct, modified, incorrect}
