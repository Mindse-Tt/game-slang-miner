"""N-gram 候选抽取（中文）。

挖掘流水线的第一步：把评论语料切成「字」序列，按窗口枚举 2~4gram 候选字串
并统计频次。这里特意做两件事：

1. **字级 n-gram**：黑话往往是分词器切不开的新词（如「御三家」「一套带走」），
   因此候选枚举走「字级」窗口而非直接用 jieba 分词结果，避免新词被切碎。
2. **jieba 仅作辅助统计**：jieba 的分词结果用于后续 PMI 计算中
   「单字/单元的全局频次」的归一基准，以及切句去噪，不直接决定候选边界。

只处理中文连续片段：把每条评论按非中文字符（标点、英文、数字、空白）切成若干
「纯中文小段」，再在每个小段内部滑窗，避免跨标点产生无意义的 n-gram。

Python 3.9 兼容：统一使用 ``typing`` 泛型。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List, Tuple

# 仅保留中文字符（基本汉字区 + 扩展A）作为可成词字符；其余视为天然边界。
_CHINESE_RUN = re.compile(r"[㐀-䶿一-鿿]+")


def split_chinese_runs(text: str) -> List[str]:
    """把一段文本切成若干「连续中文小段」。

    非中文字符（标点 / 英文 / 数字 / 空白 / emoji）都当作硬边界，
    这样滑窗只会在语义连续的中文片段内部进行。

    Args:
        text: 原始评论文本。

    Returns:
        连续中文小段列表（已去除空串）。
    """
    if not text:
        return []
    return _CHINESE_RUN.findall(text)


def iter_char_ngrams(run: str, n: int) -> List[str]:
    """在单个中文小段内滑窗，产出所有长度为 ``n`` 的字级 n-gram。

    Args:
        run: 一段连续中文文本（不含标点）。
        n: n-gram 字数。

    Returns:
        该小段内所有长度 n 的子串（保留重复，供频次统计）。
    """
    if n <= 0 or len(run) < n:
        return []
    return [run[i : i + n] for i in range(len(run) - n + 1)]


def extract_ngrams(
    texts: List[str],
    ngram_min: int,
    ngram_max: int,
) -> Tuple[Counter, Dict[int, Counter], int]:
    """从语料中抽取 ``ngram_min``~``ngram_max`` 的字级 n-gram 频次。

    同时统计「单字频次」与「逐阶 n-gram 频次表」，供 PMI 计算复用，
    避免下游重复扫描语料。

    Args:
        texts: 评论正文列表。
        ngram_min: 最短 n-gram 字数（含）。
        ngram_max: 最长 n-gram 字数（含）。

    Returns:
        三元组 ``(ngram_freq, freq_by_n, total_chars)``：
            - ngram_freq: 所有阶 n-gram 合并后的频次 Counter（term -> freq）。
            - freq_by_n: 按 n 分层的频次表，``freq_by_n[1]`` 即单字频次，
              用于 PMI 的分母（含 1 到 ngram_max 全部阶）。
            - total_chars: 语料总字数（PMI 概率归一用的样本规模）。
    """
    if ngram_min < 1:
        ngram_min = 1
    if ngram_max < ngram_min:
        ngram_max = ngram_min

    # 为支撑 PMI 切分子串概率，单字（n=1）始终统计，故下限从 1 开始累计。
    freq_by_n: Dict[int, Counter] = {n: Counter() for n in range(1, ngram_max + 1)}
    total_chars = 0

    for text in texts:
        for run in split_chinese_runs(text):
            total_chars += len(run)
            for n in range(1, ngram_max + 1):
                grams = iter_char_ngrams(run, n)
                if grams:
                    freq_by_n[n].update(grams)

    # 对外暴露的候选频次只含 [ngram_min, ngram_max]，单字一般不作候选。
    ngram_freq: Counter = Counter()
    for n in range(ngram_min, ngram_max + 1):
        ngram_freq.update(freq_by_n[n])

    return ngram_freq, freq_by_n, total_chars
