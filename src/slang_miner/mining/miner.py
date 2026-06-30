"""挖掘主流程编排：从评论里挑出「长得像黑话」的候选词。

对外只暴露一个入口 :func:`mine`，把 ngram / pmi / entropy 三个子模块串起来：

    抽 n-gram → 频次过滤 → 算 PMI → 算左右熵 → 阈值过滤 → 综合 score 排序
    → 过滤掉精灵名 / 技能名 / 官方术语（entities 表）→ 截断 max_candidates
    → 为每个候选附 2~3 条原文例句

设计要点：
- **过滤 entities**：既剔除与官方专有名词「完全相同」的候选，也剔除「是某个
  专有名词子串」的候选（如「岩石巨」是精灵「岩石巨人」的一部分，应去掉），
  避免把官方词的碎片当黑话。
- **综合打分**：score 融合频次（取 log 抑制长尾）、PMI（内部凝固度）、
  左右熵较小值（边界自由度的短板）。三者皆为「越大越像黑话」，加权求和。
- **可复现**：排序在分数相同的情况下按 term 字典序兜底，保证输出稳定。

Python 3.9 兼容。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Sequence

from ..schema import Candidate, Comment
from .entropy import compute_entropies
from .ngram import extract_ngrams, split_chinese_runs
from .pmi import compute_pmi

# score 各分量权重（经验默认值；可按需外移到 config）。
_W_FREQ = 1.0   # 频次（log 压缩后）
_W_PMI = 1.0    # 内部凝固度
_W_ENT = 1.0    # 左右熵短板

# 单条候选最多保留的例句数。
_MAX_EXAMPLES = 3


def _get_mining_cfg(config: Mapping[str, Any]) -> Dict[str, Any]:
    """从总配置里取出 mining 段，并补齐默认值。

    兼容两种传入：整份 config（含顶层 ``mining`` 键）或直接传 mining 段本身。

    Args:
        config: 配置字典。

    Returns:
        含全部挖掘参数的字典。
    """
    cfg = dict(config.get("mining", config)) if isinstance(config, Mapping) else {}
    return {
        "ngram_min": int(cfg.get("ngram_min", 2)),
        "ngram_max": int(cfg.get("ngram_max", 4)),
        "min_freq": int(cfg.get("min_freq", 3)),
        "min_pmi": float(cfg.get("min_pmi", 1.0)),
        "min_entropy": float(cfg.get("min_entropy", 1.0)),
        "max_candidates": int(cfg.get("max_candidates", 3000)),
    }


def _build_entity_blocklist(entities: Sequence[Mapping[str, str]]) -> List[str]:
    """把 entities 过滤表整理成「需要屏蔽的官方词」列表。

    每个元素形如 ``{"term": "岩石巨人", "type": "精灵"}``。这里只取 term。

    Args:
        entities: 精灵 / 技能 / 官方术语条目序列。

    Returns:
        官方专有名词字符串列表（去重、非空）。
    """
    terms: List[str] = []
    seen = set()
    for row in entities or []:
        term = (row.get("term") or "").strip() if isinstance(row, Mapping) else ""
        if term and term not in seen:
            seen.add(term)
            terms.append(term)
    return terms


def _is_blocked_by_entities(term: str, entity_terms: Sequence[str]) -> bool:
    """判断候选是否应被 entities 表屏蔽。

    屏蔽规则（命中任一即过滤）：
    1. 候选与某官方词完全相同；
    2. 候选是某官方词的子串（官方词的碎片，如「巨人」之于「岩石巨人」）；
    3. 某官方词是候选的子串（候选裹着一个完整官方词，如「选烈焰龙」含「烈焰龙」）。

    Args:
        term: 候选字串。
        entity_terms: 官方专有名词列表。

    Returns:
        True 表示应过滤。
    """
    for ent in entity_terms:
        if term == ent or term in ent or ent in term:
            return True
    return False


def _score(freq: int, pmi: float, left_e: float, right_e: float) -> float:
    """综合打分：频次(log) + PMI + 左右熵短板，三者加权求和。

    取左右熵的较小值（min）而非均值，是为了惩罚「一侧边界被固定」的伪词
    —— 只要有一侧不自由，就不像独立成词。

    Args:
        freq: 候选频次。
        pmi: 点互信息。
        left_e: 左熵。
        right_e: 右熵。

    Returns:
        综合分数（越大越像黑话）。
    """
    freq_score = math.log2(freq + 1.0)
    ent_score = min(left_e, right_e)
    return _W_FREQ * freq_score + _W_PMI * pmi + _W_ENT * ent_score


def _collect_examples(term: str, texts: Sequence[str]) -> List[str]:
    """为候选词收集最多 _MAX_EXAMPLES 条命中原文例句。

    只在「连续中文小段」层面判断包含关系，避免被标点误伤；按原文出现顺序取前 N 条。

    Args:
        term: 候选字串。
        texts: 评论正文列表。

    Returns:
        例句列表（最多 3 条）。
    """
    examples: List[str] = []
    for text in texts:
        if any(term in run for run in split_chinese_runs(text)):
            examples.append(text)
            if len(examples) >= _MAX_EXAMPLES:
                break
    return examples


def mine(
    comments: Sequence[Comment],
    entities: Sequence[Mapping[str, str]],
    config: Mapping[str, Any],
) -> List[Candidate]:
    """从评论语料中挖掘疑似黑话候选。

    Args:
        comments: 玩家评论列表（:class:`~slang_miner.schema.Comment`）。
        entities: 精灵名 / 技能名 / 官方术语过滤表，元素含 ``term`` 键。
        config: 配置（整份 config 或其 ``mining`` 段均可）。

    Returns:
        按 score 降序排列、已过滤官方词、已截断 max_candidates 的
        :class:`~slang_miner.schema.Candidate` 列表，每条附 2~3 例句。
    """
    cfg = _get_mining_cfg(config)
    texts: List[str] = [c.text for c in comments if getattr(c, "text", "")]
    if not texts:
        return []

    # 1) 抽 n-gram，拿到候选频次表 + 分层频次（PMI 复用）+ 总字数。
    ngram_freq, freq_by_n, total_chars = extract_ngrams(
        texts, cfg["ngram_min"], cfg["ngram_max"]
    )

    # 2) 频次过滤：低频串直接淘汰（统计指标在低频下不可靠）。
    survivors = [t for t, f in ngram_freq.items() if f >= cfg["min_freq"]]
    if not survivors:
        return []

    # 3) 一次性算左右熵（批量扫描，避免逐词重扫语料）。
    entropies = compute_entropies(texts, survivors, cfg["ngram_max"])

    entity_terms = _build_entity_blocklist(entities)

    # 4) 逐候选算 PMI、组装、按阈值过滤、剔除官方词。
    candidates: List[Candidate] = []
    for term in survivors:
        if _is_blocked_by_entities(term, entity_terms):
            continue

        freq = ngram_freq[term]
        pmi = compute_pmi(term, freq_by_n, total_chars)
        left_e, right_e = entropies.get(term, (0.0, 0.0))

        # PMI 与「左右熵短板」双阈值过滤。
        if pmi < cfg["min_pmi"]:
            continue
        if min(left_e, right_e) < cfg["min_entropy"]:
            continue

        score = _score(freq, pmi, left_e, right_e)
        candidates.append(
            Candidate(
                term=term,
                freq=freq,
                pmi=round(pmi, 4),
                left_entropy=round(left_e, 4),
                right_entropy=round(right_e, 4),
                score=round(score, 4),
                examples=_collect_examples(term, texts),
            )
        )

    # 5) 综合 score 降序；同分按 term 字典序兜底保证可复现。
    candidates.sort(key=lambda c: (-c.score, c.term))

    # 6) 截断到 max_candidates。
    return candidates[: cfg["max_candidates"]]
