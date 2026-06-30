"""闭环回灌（feedback）。

把人工审核确认（correct / modified）的黑话词条，回灌进「已确认黑话种子词典」
``data/seeds/known_slang.csv``，实现产品的核心闭环：

    人工审核结果（xlsx 或 SlangEntry 列表）
        → 取出 correct / modified 的词
        → 合并进种子词典（去重 / 更新释义）
        → 下一轮自动挖掘据此「自动过滤已知词」+ Agent 拿到更多种子，越用越准

种子 CSV 列约定（与 data/seeds/known_slang.csv 一致，顺序不可改）：

    term,definition,category

设计要点：
- **入参双形态**：``ingest_reviewed`` 既接受 xlsx 路径（复用 review.exporter 的
  导入逻辑），也接受已解析好的 ``SlangEntry`` 列表，便于测试与上层编排。
- **只回灌确认词**：仅 status ∈ {correct, modified} 的词进入种子；incorrect /
  pending 一律忽略。
- **幂等合并**：以 term 为主键，已存在则按需更新释义（modified 覆盖），不重复追加；
  读出全量 → 在内存中构造新表 → 整文件写回（不可变风格，避免就地追加导致重复）。
- **Python 3.9 兼容**：``from __future__ import annotations`` + ``typing`` 泛型。
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Dict, List, Sequence, Union

from .schema import SlangEntry

logger = logging.getLogger("slang_miner.feedback")

# 种子 CSV 表头（顺序即列序，务必与现有种子文件保持一致）。
SEED_HEADER: List[str] = ["term", "definition", "category"]

# 会被回灌进种子词典的人工判定状态。
CONFIRMED_STATUSES = ("correct", "modified")

# xlsx 文件扩展名（用于判断入参是否为 Excel 路径）。
_XLSX_SUFFIXES = (".xlsx", ".xlsm")


def ingest_reviewed(
    xlsx_or_entries: Union[str, Path, Sequence[SlangEntry]],
    seeds_path: Union[str, Path],
) -> int:
    """把人工确认（correct/modified）的词回灌种子词典，返回新增/更新的词条数。

    Args:
        xlsx_or_entries: 三种形态之一——
            * 人工填好的审核 xlsx 的路径（str / Path）；
            * 已解析好的 ``SlangEntry`` 列表 / 序列。
        seeds_path: 种子词典 CSV 的绝对路径（``data/seeds/known_slang.csv``）。
            文件不存在时会新建（含表头）；父目录自动创建。

    Returns:
        实际写入种子词典的「确认词」数量（新增 + 释义更新，去重后）。

    Raises:
        FileNotFoundError: 当传入的是 xlsx 路径但文件不存在时。
        ValueError: 当 xlsx 解析或种子文件读取出现结构性错误时。
    """
    entries = _coerce_to_entries(xlsx_or_entries)
    confirmed = [e for e in entries if e.status in CONFIRMED_STATUSES]

    if not confirmed:
        logger.info("没有 correct/modified 的词条，种子词典保持不变。")
        return 0

    seeds_file = Path(seeds_path)
    existing = _read_seeds(seeds_file)

    merged, affected_terms = _merge_seeds(existing, confirmed)
    _write_seeds(seeds_file, merged)

    logger.info(
        "闭环回灌完成：确认词 %d 条 → 种子词典更新 %d 条（去重后），现有种子 %d 条。",
        len(confirmed),
        len(affected_terms),
        len(merged),
    )
    return len(affected_terms)


# ---------------------------------------------------------------------------
# 入参归一化
# ---------------------------------------------------------------------------


def _coerce_to_entries(
    xlsx_or_entries: Union[str, Path, Sequence[SlangEntry]],
) -> List[SlangEntry]:
    """把多形态入参统一成 ``List[SlangEntry]``。

    - str / Path：当作 xlsx 路径，复用 review.exporter.import_verdicts 解析。
    - 序列：逐元素校验必须是 SlangEntry。
    """
    if isinstance(xlsx_or_entries, (str, Path)):
        path = Path(xlsx_or_entries)
        if path.suffix.lower() not in _XLSX_SUFFIXES:
            raise ValueError(
                f"期望 xlsx 文件路径，但得到后缀 {path.suffix!r}：{path}"
            )
        # 延迟 import，避免在「只用 SlangEntry 列表」的场景强依赖 openpyxl。
        from .review.exporter import import_verdicts

        return list(import_verdicts(path))

    entries: List[SlangEntry] = []
    for idx, item in enumerate(xlsx_or_entries):
        if not isinstance(item, SlangEntry):
            raise ValueError(
                f"entries[{idx}] 期望 SlangEntry，实际为 {type(item).__name__}"
            )
        entries.append(item)
    return entries


# ---------------------------------------------------------------------------
# 种子 CSV 读 / 合并 / 写
# ---------------------------------------------------------------------------


def _read_seeds(seeds_file: Path) -> "List[Dict[str, str]]":
    """读出现有种子为「行字典」列表；文件不存在 → 空列表。

    每行字典固定含 SEED_HEADER 三个键。容错：旧文件若缺列，缺失键以空串补齐。
    """
    if not seeds_file.exists():
        return []

    rows: List[Dict[str, str]] = []
    with seeds_file.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            rows.append(
                {
                    "term": (raw.get("term") or "").strip(),
                    "definition": (raw.get("definition") or "").strip(),
                    "category": (raw.get("category") or "").strip(),
                }
            )
    # 丢弃无 term 的脏行。
    return [r for r in rows if r["term"]]


def _merge_seeds(
    existing: "List[Dict[str, str]]",
    confirmed: List[SlangEntry],
) -> "tuple":
    """以 term 为主键，把确认词合并进现有种子，返回 (新种子列表, 受影响 term 集合)。

    合并语义（幂等）：
    - 新 term：追加。
    - 已存在 term：
        * modified：用新释义 / 类别覆盖（人工修正过的更权威）。
        * correct：仅在原释义为空时补齐，否则保持原样（避免无谓改动）；
                   类别同理仅补空缺。
    - 同一批 confirmed 内出现重复 term：后者按上述规则继续叠加合并。

    不修改入参（existing 中的字典会被复制后再改），遵循不可变风格。
    """
    # 先复制一份，保持入参不被原地修改。
    merged: List[Dict[str, str]] = [dict(row) for row in existing]
    index_by_term: Dict[str, int] = {row["term"]: i for i, row in enumerate(merged)}

    affected: "set" = set()

    for entry in confirmed:
        term = (entry.term or "").strip()
        if not term:
            continue

        new_def = (entry.definition or "").strip()
        new_cat = (entry.category or "").strip()

        if term not in index_by_term:
            merged.append(
                {"term": term, "definition": new_def, "category": new_cat}
            )
            index_by_term[term] = len(merged) - 1
            affected.add(term)
            continue

        row = merged[index_by_term[term]]
        updated = False

        if entry.status == "modified":
            if new_def and new_def != row["definition"]:
                row["definition"] = new_def
                updated = True
            if new_cat and new_cat != row["category"]:
                row["category"] = new_cat
                updated = True
        else:  # correct：仅补空缺，不覆盖既有非空值
            if not row["definition"] and new_def:
                row["definition"] = new_def
                updated = True
            if not row["category"] and new_cat:
                row["category"] = new_cat
                updated = True

        if updated:
            affected.add(term)

    return merged, affected


def _write_seeds(seeds_file: Path, rows: "List[Dict[str, str]]") -> None:
    """整文件写回种子 CSV（表头 + 全部行）。

    采用「整体重写」而非追加，确保去重 / 更新结果落盘且无重复行。
    父目录不存在则创建。
    """
    seeds_file.parent.mkdir(parents=True, exist_ok=True)
    with seeds_file.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=SEED_HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "term": row.get("term", ""),
                    "definition": row.get("definition", ""),
                    "category": row.get("category", ""),
                }
            )
