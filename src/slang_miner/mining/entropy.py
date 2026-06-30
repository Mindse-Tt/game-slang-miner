"""左右邻接熵（边界自由度）计算。

一个真正的「词」，左右两侧应该能搭配多种不同的字（边界自由）；而一个伪词
（如某长词的一部分）往往左侧或右侧总跟着固定的字。邻接熵正是量化这种自由度：

    H_side(w) = - sum_c  P(c|w) * log2 P(c|w)

其中 c 取遍出现在 w 左侧（或右侧）的所有邻接字，P(c|w) 为该邻接字在所有
出现位置中的占比。熵越大，边界越自由，越像一个独立成词。

实现上一次扫描语料即可收集每个候选词的左右邻接字分布。

Python 3.9 兼容。
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Tuple

from .ngram import iter_char_ngrams, split_chinese_runs

# 句首 / 句尾用特殊占位符表示一种「边界邻接」，同样计入分布以体现自由度。
_BOS = "\x02"  # beginning-of-run
_EOS = "\x03"  # end-of-run


def _entropy(counter: Counter) -> float:
    """根据邻接字计数分布计算香农熵（log2）。

    Args:
        counter: 邻接字 -> 次数。

    Returns:
        熵值；空分布返回 0.0。
    """
    total = sum(counter.values())
    if total <= 0:
        return 0.0
    h = 0.0
    for cnt in counter.values():
        p = cnt / total
        h -= p * math.log2(p)
    return h


def collect_adjacency(
    texts: List[str],
    terms: Iterable[str],
    ngram_max: int,
) -> Dict[str, Tuple[Counter, Counter]]:
    """一次扫描语料，收集每个候选词的左 / 右邻接字分布。

    为提高效率，只关心传入的候选 ``terms`` 集合；在每个中文小段上按候选词的
    实际长度滑窗匹配，记录其紧邻的左字、右字（含句首 / 句尾占位）。

    Args:
        texts: 评论正文列表。
        terms: 需要统计邻接分布的候选词集合。
        ngram_max: 候选最大字数（限制需要匹配的窗口长度）。

    Returns:
        ``term -> (left_counter, right_counter)`` 的映射。
    """
    term_set = set(terms)
    if not term_set:
        return {}

    # 按长度分组，扫描时每个长度只滑一遍窗。
    lengths = sorted({len(t) for t in term_set if 1 <= len(t) <= ngram_max})

    left_adj: Dict[str, Counter] = defaultdict(Counter)
    right_adj: Dict[str, Counter] = defaultdict(Counter)

    for text in texts:
        for run in split_chinese_runs(text):
            run_len = len(run)
            for n in lengths:
                if run_len < n:
                    continue
                for i in range(run_len - n + 1):
                    gram = run[i : i + n]
                    if gram not in term_set:
                        continue
                    left_char = run[i - 1] if i - 1 >= 0 else _BOS
                    right_char = run[i + n] if i + n < run_len else _EOS
                    left_adj[gram][left_char] += 1
                    right_adj[gram][right_char] += 1

    result: Dict[str, Tuple[Counter, Counter]] = {}
    for term in term_set:
        result[term] = (left_adj.get(term, Counter()), right_adj.get(term, Counter()))
    return result


def compute_entropies(
    texts: List[str],
    terms: Iterable[str],
    ngram_max: int,
) -> Dict[str, Tuple[float, float]]:
    """计算每个候选词的 (左熵, 右熵)。

    Args:
        texts: 评论正文列表。
        terms: 候选词集合。
        ngram_max: 候选最大字数。

    Returns:
        ``term -> (left_entropy, right_entropy)`` 的映射。
    """
    adjacency = collect_adjacency(texts, terms, ngram_max)
    out: Dict[str, Tuple[float, float]] = {}
    for term, (left_c, right_c) in adjacency.items():
        out[term] = (_entropy(left_c), _entropy(right_c))
    return out


# 说明：iter_char_ngrams 在本模块未直接使用，但保留 import 以表明 entropy 与
# ngram 共享同一套「中文小段 + 字级滑窗」的切分语义，便于读者对照。
_ = iter_char_ngrams
