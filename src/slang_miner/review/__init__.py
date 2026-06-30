"""人工审核子包：周报 xlsx 的导出与回读。

对外主入口：
    * :func:`export_review_xlsx` —— 把 :class:`SlangEntry` 列表导出为带样式 /
      下拉数据验证的「待审核黑话」周报 xlsx。
    * :func:`import_verdicts` —— 把人工填好的 xlsx 读回为 :class:`SlangEntry`
      列表（解析 correct/modified/incorrect/pending 状态），供闭环回灌使用。
"""

from __future__ import annotations

from .exporter import export_review_xlsx, import_verdicts

__all__ = ["export_review_xlsx", "import_verdicts"]
