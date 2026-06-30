"""端到端串行流水线（pipeline）。

本模块是整个产品的**核心编排层**，对外暴露一个高层入口 :func:`run_pipeline`，
忠实复现产品流程图的「自动挖掘 → 3 个 Agent 串行审查」两段：

    评论语料
        → 自动挖掘（N-gram + PMI + 左右熵；过滤精灵名/技能名/官方术语）得到候选
        → Agent1 Classifier（是不是黑话？7 类分类）
        → Agent2 Definer（什么意思？给释义 + 原文例句）
        → Agent3 Reviewer（是不是真指代游戏内事物？严格兜底 keep/reject）
        → 最终 entries（status="pending"，等待人工审核）

设计要点：
- **高层 + 低层双入口**：
    * :func:`run_pipeline` —— CLI 使用的高层入口，吃「评论 + 整份 config」，
      内部完成「读 entities → 挖掘 → 构造 LLM/Agent → 三段漏斗」。
    * :func:`run_agents` —— 仅跑「候选 → 三个 Agent」的低层入口，便于单测注入
      mock Agent，不触碰挖掘与磁盘。
- **串行短路**：Classifier 判定非黑话的候选直接淘汰，不浪费后两个 Agent 的
  （潜在）LLM 调用；Reviewer 裁决 reject 的同样淘汰。
- **解耦 + 鸭子类型**：本模块不关心 Agent 内部如何实现（mock / 真实 LLM），
  只依赖 schema.py 约定的结果数据类，以及 Agent 暴露的 ``classify/define/review``
  方法。通过轻量适配层兼容各方法签名的细微差异（如 Reviewer 复核的是组装好的
  :class:`SlangEntry` 而非 Candidate），降低与 agents 模块的耦合。
- **可观测**：打印漏斗各环节通过数，便于评估转化（候选 → 分类通过 → 定义 →
  终审通过）。

Python 3.9 兼容：``from __future__ import annotations`` + ``typing`` 泛型。
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .schema import (
    Candidate,
    ClassifierResult,
    Comment,
    DefinerResult,
    ReviewerResult,
    SlangEntry,
)

logger = logging.getLogger("slang_miner.pipeline")


# ---------------------------------------------------------------------------
# 高层入口：评论 → 候选 → 三 Agent → 词条（CLI 调用此函数）
# ---------------------------------------------------------------------------
def run_pipeline(
    comments: Sequence[Comment],
    config: Mapping[str, Any],
    *,
    classifier: Optional[Any] = None,
    definer: Optional[Any] = None,
    reviewer: Optional[Any] = None,
) -> List[SlangEntry]:
    """端到端运行流水线：评论 → 自动挖掘 → 3 Agent 串行 → 待审词条。

    Args:
        comments: 玩家评论列表（:class:`~slang_miner.schema.Comment`）。
        config: 整份配置字典（见 ``config/config.yaml``），含 mining / llm / paths。
        classifier / definer / reviewer: 可选，直接注入已构造好的 Agent 实例
            （便于测试注入 mock 替身）。任一为 None 时按 ``config.llm`` 构造默认
            Agent（离线 mock 或真实 provider）。

    Returns:
        通过三道关卡的黑话词条列表，每条 ``status="pending"``，等待人工审核。
    """
    # --- 自动挖掘（含过滤精灵/技能/官方术语）-------------------------------
    candidates = _mine_candidates(comments, config)
    logger.info("自动挖掘完成：候选 %d 条。", len(candidates))

    # --- 构造 / 复用三个 Agent ---------------------------------------------
    classifier, definer, reviewer = _ensure_agents(
        config, classifier, definer, reviewer
    )

    # --- 候选 → 三段漏斗 → 词条，并按命中来源回填 sources ------------------
    entries = run_agents(candidates, classifier, definer, reviewer)
    _backfill_sources(entries, comments)
    return entries


# ---------------------------------------------------------------------------
# 低层入口：候选 → 三 Agent → 词条（单测注入 mock Agent 时直接用此函数）
# ---------------------------------------------------------------------------
def run_agents(
    candidates: Sequence[Candidate],
    classifier: Any,
    definer: Any,
    reviewer: Any,
) -> List[SlangEntry]:
    """把候选词依次过三个 Agent，漏斗筛选成待审核词条。

    Args:
        candidates: 自动挖掘阶段产出的候选词列表。
        classifier: Agent1，需可调用 ``classify(candidate) -> ClassifierResult``。
        definer: Agent2，需可调用 ``define(candidate) -> DefinerResult``。
        reviewer: Agent3，需可调用 ``review(entry: SlangEntry) -> ReviewerResult``
            （亦兼容 ``review(candidate, ...)`` 的旧签名，见适配层）。

    Returns:
        终审 keep 的词条列表（status="pending"）。
    """
    if not candidates:
        logger.info("候选为空，流水线直接返回空结果。")
        return []

    total = len(candidates)
    classified_slang: List[Tuple[Candidate, ClassifierResult]] = []
    defined: List[Tuple[Candidate, ClassifierResult, DefinerResult]] = []
    entries: List[SlangEntry] = []

    # --- 环节 1：Classifier ------------------------------------------------
    for cand in candidates:
        clf: ClassifierResult = classifier.classify(cand)
        if getattr(clf, "is_slang", False):
            classified_slang.append((cand, clf))

    # --- 环节 2：Definer（仅处理被判为黑话的）-----------------------------
    for cand, clf in classified_slang:
        dfn: DefinerResult = definer.define(cand)
        defined.append((cand, clf, dfn))

    # --- 环节 3：Reviewer（严格兜底，仅 keep 的进入最终结果）--------------
    kept = 0
    for cand, clf, dfn in defined:
        entry = _build_entry(cand, clf, dfn)
        rev: ReviewerResult = _review_entry(reviewer, entry, cand, clf, dfn)
        if getattr(rev, "verdict", "reject") == "keep":
            entries.append(entry)
            kept += 1

    logger.info(
        "流水线漏斗：候选 %d → 分类通过 %d → 定义 %d → 终审通过 %d",
        total,
        len(classified_slang),
        len(defined),
        kept,
    )
    return entries


# ---------------------------------------------------------------------------
# 自动挖掘
# ---------------------------------------------------------------------------
def _mine_candidates(
    comments: Sequence[Comment],
    config: Mapping[str, Any],
) -> List[Candidate]:
    """读取 entities 过滤表并调用挖掘模块。

    挖掘模块 :func:`slang_miner.mining.mine` 的签名为
    ``mine(comments, entities, config)``，其中 entities 是「行字典列表」
    （每行含 ``term`` 键）。本函数负责把 ``config.paths.entities`` 指向的 csv
    读成该形态。
    """
    from .mining import mine  # 延迟 import，避免顶层循环依赖

    entities = _load_entities(config)
    return list(mine(comments, entities, config))


def _load_entities(config: Mapping[str, Any]) -> List[Dict[str, str]]:
    """从 config.paths.entities 读取过滤表为行字典列表；缺路径/缺文件 → 空表。

    返回元素形如 ``{"term": "岩石巨人", "type": "精灵"}``。
    """
    rel = _dig(config, "paths", "entities", default="")
    if not rel:
        return []
    path = _abs_path(config, rel)
    if not path.is_file():
        logger.warning("entities 过滤表不存在，跳过专有名词过滤：%s", path)
        return []

    rows: List[Dict[str, str]] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            term = (raw.get("term") or "").strip()
            if term:
                rows.append({"term": term, "type": (raw.get("type") or "").strip()})
    return rows


# ---------------------------------------------------------------------------
# Agent 构造（默认从 config.llm 起一个共享 LLMClient）
# ---------------------------------------------------------------------------
def _ensure_agents(
    config: Mapping[str, Any],
    classifier: Optional[Any],
    definer: Optional[Any],
    reviewer: Optional[Any],
) -> Tuple[Any, Any, Any]:
    """补齐缺省 Agent；已全部注入则原样返回（测试场景不触发 import）。"""
    if classifier is not None and definer is not None and reviewer is not None:
        return classifier, definer, reviewer

    # 延迟 import：仅在确需默认 Agent 时才依赖 agents 模块。
    from .agents import (
        ClassifierAgent,
        DefinerAgent,
        LLMClient,
        ReviewerAgent,
    )

    provider = str(_dig(config, "llm", "provider", default="mock"))
    model = str(_dig(config, "llm", "model", default=""))
    offline = bool(_dig(config, "llm", "offline", default=True))

    # 各 Agent 各持一个 client（system prompt 不同，避免 mock handler 串味）。
    classifier = classifier or ClassifierAgent(
        LLMClient(provider=provider, model=model, offline=offline)
    )
    definer = definer or DefinerAgent(
        LLMClient(provider=provider, model=model, offline=offline)
    )
    reviewer = reviewer or ReviewerAgent(
        LLMClient(provider=provider, model=model, offline=offline)
    )
    return classifier, definer, reviewer


# ---------------------------------------------------------------------------
# 适配层 / 组装
# ---------------------------------------------------------------------------
def _review_entry(
    reviewer: Any,
    entry: SlangEntry,
    cand: Candidate,
    clf: ClassifierResult,
    dfn: DefinerResult,
) -> ReviewerResult:
    """调用 Reviewer，兼容两种方法签名。

    - 产品默认实现：``review(entry: SlangEntry) -> ReviewerResult``（复核组装好的
      词条，能利用释义/例句/置信度做严格判定）。
    - 旧式/测试替身：``review(candidate, classifier_result=, definer_result=)``。

    先按「传 entry」尝试；若因参数不符抛 TypeError，再退回旧式签名。
    """
    review = getattr(reviewer, "review", None)
    if not callable(review):
        if callable(reviewer):
            review = reviewer
        else:
            raise TypeError(f"Reviewer {reviewer!r} 不可调用，无法接入流水线。")

    try:
        return review(entry)
    except TypeError:
        # 退回旧签名（候选 + 上游结果）
        return review(cand, classifier_result=clf, definer_result=dfn)


def _build_entry(
    cand: Candidate,
    clf: ClassifierResult,
    dfn: DefinerResult,
) -> SlangEntry:
    """把三段 Agent 结果与候选词信息合成一条 ``SlangEntry``（status=pending）。

    - example 优先取 Definer 给出的例句；若为空则回退到候选词自带的第一条例句。
    - sources 先留空，由 :func:`_backfill_sources` 据命中评论回填。
    - confidence 直接采用 Classifier 的置信度。
    """
    example = (getattr(dfn, "example", "") or "").strip()
    if not example and cand.examples:
        example = cand.examples[0]

    return SlangEntry(
        term=cand.term,
        category=clf.category,
        definition=dfn.definition,
        example=example,
        confidence=clf.confidence,
        sources=[],
        status="pending",
    )


def _backfill_sources(
    entries: Sequence[SlangEntry],
    comments: Sequence[Comment],
) -> None:
    """据「哪些评论包含该词」回填每条词条的来源渠道（去重、保序）。

    就地补充 entry.sources（此处确为「按命中事实补充」而非语义篡改，
    且仅在 sources 为空时填充，符合产品对来源可追溯的要求）。
    """
    for entry in entries:
        if entry.sources:
            continue
        seen: List[str] = []
        for c in comments:
            if entry.term and entry.term in (c.text or "") and c.source not in seen:
                seen.append(c.source)
        entry.sources = seen


# ---------------------------------------------------------------------------
# 配置小工具
# ---------------------------------------------------------------------------
def _dig(config: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    """安全按层级取嵌套配置值，任一层缺失返回 default。"""
    cur: Any = config
    for key in keys:
        if not isinstance(cur, Mapping) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _abs_path(config: Mapping[str, Any], rel: str) -> Path:
    """把相对路径解析为绝对路径。

    优先使用 config._meta.repo_root（CLI 注入）；否则按本文件位置推导仓库根
    （``<root>/src/slang_miner/pipeline.py`` 向上 3 级）。
    """
    p = Path(rel)
    if p.is_absolute():
        return p
    repo_root = _dig(config, "_meta", "repo_root", default="")
    base = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]
    return base / p
