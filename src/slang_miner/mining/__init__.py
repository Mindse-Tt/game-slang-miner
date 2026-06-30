"""自动挖掘子包：N-gram + PMI + 左右熵 的中文黑话候选发现。

对外主入口为 :func:`mine`；其余子模块（ngram / pmi / entropy）为可独立测试的
纯函数实现。
"""

from __future__ import annotations

from .miner import mine

__all__ = ["mine"]
