"""点互信息 PMI（内部凝固度）计算。

PMI 衡量一个候选字串「内部各部分是否真的爱黏在一起」，是判断新词成词性的
经典指标之一。对中文新词发现，常用做法是取该字串「所有二分切点」中
凝固度最低的一种切分作为该词的 PMI 值（即最弱黏合处），这样能惩罚那些
「只是因为某个高频子串而被带高频次」的伪词。

定义（以最弱切分为准）::

    PMI(w) = min over split (a, b) of  log2( P(w) / (P(a) * P(b)) )

其中 P(x) = freq(x) / total_chars，按字级概率估计。PMI 越大表示越凝固。

Python 3.9 兼容。
"""

from __future__ import annotations

import math
from typing import Counter as CounterT
from typing import Dict


def _prob(token: str, freq_by_n: Dict[int, CounterT], total_chars: int) -> float:
    """估计某子串的出现概率 P(token) = freq / total_chars。

    Args:
        token: 子串（任意长度）。
        freq_by_n: 按长度分层的频次表（来自 ngram 抽取）。
        total_chars: 语料总字数。

    Returns:
        概率值；频次缺失或语料为空时返回 0.0。
    """
    if total_chars <= 0:
        return 0.0
    n = len(token)
    layer = freq_by_n.get(n)
    if layer is None:
        return 0.0
    f = layer.get(token, 0)
    if f <= 0:
        return 0.0
    return f / total_chars


def compute_pmi(
    term: str,
    freq_by_n: Dict[int, CounterT],
    total_chars: int,
) -> float:
    """计算单个候选词的 PMI（取所有二分切分中的最小值 = 最弱凝固处）。

    长度为 1 的串没有内部切点，约定 PMI=0.0（不具备「成词凝固」语义）。

    Args:
        term: 候选字串。
        freq_by_n: 按长度分层的频次表。
        total_chars: 语料总字数。

    Returns:
        PMI 值（log2）。无法计算（概率为 0）时返回 0.0。
    """
    if len(term) < 2 or total_chars <= 0:
        return 0.0

    p_whole = _prob(term, freq_by_n, total_chars)
    if p_whole <= 0.0:
        return 0.0

    min_pmi = math.inf
    # 枚举每一个二分切点 a=term[:i], b=term[i:]，取最弱黏合（PMI 最小）的那种。
    for i in range(1, len(term)):
        left = term[:i]
        right = term[i:]
        p_left = _prob(left, freq_by_n, total_chars)
        p_right = _prob(right, freq_by_n, total_chars)
        if p_left <= 0.0 or p_right <= 0.0:
            # 某半边在语料里不存在（数据稀疏），跳过这种切分。
            continue
        pmi = math.log2(p_whole / (p_left * p_right))
        if pmi < min_pmi:
            min_pmi = pmi

    if min_pmi is math.inf:
        return 0.0
    return min_pmi
