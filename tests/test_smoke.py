"""端到端冒烟测试（离线 mock 模式）。

目标：在**无任何 API key / 无网络**的离线 mock 模式下，验证整条流水线可端到端
跑通，并守住几条关键契约：

1. 自动挖掘能从评论里挑出非空候选，且产出的 :class:`Candidate` 字段齐备、
   官方专有名词（精灵/技能名）被正确过滤；
2. 流水线（挖掘 → Agent1 分类 → Agent2 释义 → Agent3 终审）能产出非空、
   合法的 :class:`SlangEntry`；
3. 导出周报 xlsx 成功落盘，且能被回读（import_verdicts）还原；
4. 闭环回灌（ingest_reviewed）能把人工确认词写入种子词典。

测试不依赖仓库的样例数据文件，全部用内联小语料构造，保证可独立、可复现运行。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import pytest

from slang_miner import (
    Candidate,
    Comment,
    SlangCategory,
    SlangEntry,
    export_review_xlsx,
    import_verdicts,
    ingest_reviewed,
    mine,
    run_agents,
    run_pipeline,
)


# ---------------------------------------------------------------------------
# 测试夹具：内联小语料
# ---------------------------------------------------------------------------
@pytest.fixture
def comments() -> List[Comment]:
    """构造一份会反复出现「黑话」与「官方专有名词」的小语料。

    刻意让黑话词（如「奶妈」「打本」「非酋」）高频出现，使其能越过
    min_freq / PMI / 熵阈值成为候选；同时混入官方精灵名「岩石巨人」，
    用于验证过滤。
    """
    raw = [
        ("c01", "bilibili", "这奶妈奶量真的足，打本必带奶妈"),
        ("c02", "tieba", "打本就靠奶妈奶住，奶妈站后排"),
        ("c03", "taptap", "我又当非酋了，保底都歪了非酋石锤"),
        ("c04", "bilibili", "非酋出门，十连全白板，非酋本酋"),
        ("c05", "tieba", "打本速度太慢了，奶妈快奶一下"),
        ("c06", "taptap", "岩石巨人钢铁壁垒一开根本打不动"),  # 含官方精灵名
        ("c07", "bilibili", "打本打本，天天打本，非酋打本更难受"),
        ("c08", "tieba", "奶妈别站前面，奶妈是脆皮"),
    ]
    return [Comment(id=i, source=s, text=t) for i, s, t in raw]


@pytest.fixture
def entities() -> List[Dict[str, str]]:
    """官方精灵 / 技能名过滤表（含「岩石巨人」用于验证过滤）。"""
    return [
        {"term": "岩石巨人", "type": "精灵"},
        {"term": "钢铁壁垒", "type": "技能"},
    ]


@pytest.fixture
def mining_config() -> Dict[str, object]:
    """放宽阈值的挖掘配置，便于小语料也能产出候选。"""
    return {
        "mining": {
            "ngram_min": 2,
            "ngram_max": 4,
            "min_freq": 2,
            "min_pmi": 0.0,
            "min_entropy": 0.0,
            "max_candidates": 100,
        },
        "llm": {"provider": "mock", "model": "", "offline": True},
    }


# ---------------------------------------------------------------------------
# 1) 自动挖掘
# ---------------------------------------------------------------------------
def test_mine_produces_candidates_and_filters_entities(
    comments, entities, mining_config
):
    """挖掘出非空候选；字段齐备；官方精灵名被过滤掉。"""
    candidates = mine(comments, entities, mining_config)

    assert candidates, "离线挖掘应产出至少 1 个候选"
    assert all(isinstance(c, Candidate) for c in candidates)

    # 候选字段齐备且类型正确
    top = candidates[0]
    assert isinstance(top.term, str) and top.term
    assert isinstance(top.freq, int) and top.freq >= 2
    assert isinstance(top.pmi, float)
    assert isinstance(top.left_entropy, float)
    assert isinstance(top.right_entropy, float)
    assert isinstance(top.score, float)
    assert isinstance(top.examples, list) and top.examples

    # 官方专有名词及其碎片不应出现在候选里
    terms = {c.term for c in candidates}
    assert "岩石巨人" not in terms
    assert "钢铁壁垒" not in terms

    # 至少能挖到我们刻意高频植入的黑话之一
    assert terms & {"奶妈", "打本", "非酋"}, f"未挖到预期黑话，实得：{terms}"


# ---------------------------------------------------------------------------
# 2) 三 Agent 串行（注入 mock Agent，直连低层入口）
# ---------------------------------------------------------------------------
def test_run_agents_with_default_mock_agents(comments, entities, mining_config):
    """用默认（离线 mock）Agent 跑「候选 → 三段漏斗」，产出合法词条。"""
    from slang_miner import ClassifierAgent, DefinerAgent, ReviewerAgent

    candidates = mine(comments, entities, mining_config)
    entries = run_agents(
        candidates,
        ClassifierAgent(),  # 无 client → 自动构造离线 mock client
        DefinerAgent(),
        ReviewerAgent(),
    )

    assert entries, "三 Agent 串行后应保留至少 1 条词条"
    valid_categories = {c.value for c in SlangCategory}
    for e in entries:
        assert isinstance(e, SlangEntry)
        assert e.term
        assert e.category in valid_categories
        assert e.definition, "终审通过的词条释义不应为空"
        assert e.status == "pending"
        assert 0.0 <= e.confidence <= 1.0


# ---------------------------------------------------------------------------
# 3) 端到端高层入口 run_pipeline
# ---------------------------------------------------------------------------
def test_run_pipeline_end_to_end(comments, entities, mining_config, tmp_path):
    """run_pipeline（含挖掘 + 三 Agent）端到端产出词条，并能回填来源。"""
    # 把 entities 写到临时 csv，并让 config 指向它（验证 pipeline 自行加载过滤表）。
    entities_csv = tmp_path / "entities.csv"
    entities_csv.write_text(
        "term,type\n" + "\n".join(f"{e['term']},{e['type']}" for e in entities),
        encoding="utf-8",
    )
    cfg = dict(mining_config)
    cfg["paths"] = {"entities": str(entities_csv)}

    entries = run_pipeline(comments, cfg)

    assert entries, "端到端流水线应产出至少 1 条待审词条"
    assert all(isinstance(e, SlangEntry) for e in entries)
    # 来源应被回填（每条词条至少命中一个渠道）
    assert any(e.sources for e in entries), "应据命中评论回填 sources"


# ---------------------------------------------------------------------------
# 4) 导出 xlsx 成功 + 回读
# ---------------------------------------------------------------------------
def test_export_review_xlsx_and_reimport(comments, entities, mining_config, tmp_path):
    """导出周报 xlsx 成功落盘，且能被 import_verdicts 回读还原。"""
    entities_csv = tmp_path / "entities.csv"
    entities_csv.write_text("term,type\n岩石巨人,精灵\n", encoding="utf-8")
    cfg = dict(mining_config)
    cfg["paths"] = {"entities": str(entities_csv)}

    entries = run_pipeline(comments, cfg)
    assert entries

    xlsx_path = tmp_path / "review.xlsx"
    written = export_review_xlsx(entries, xlsx_path)

    assert Path(written).is_file(), "xlsx 应成功落盘"
    assert Path(written).stat().st_size > 0

    # 回读：人工未填判定 → status 应为 pending
    reloaded = import_verdicts(xlsx_path)
    assert len(reloaded) == len(entries)
    assert all(r.status == "pending" for r in reloaded)
    assert {r.term for r in reloaded} == {e.term for e in entries}


# ---------------------------------------------------------------------------
# 5) 闭环回灌
# ---------------------------------------------------------------------------
def test_feedback_closes_the_loop(tmp_path):
    """人工确认（correct/modified）的词回灌种子词典；incorrect 被丢弃。"""
    reviewed = [
        SlangEntry(
            term="奶妈",
            category="角色称呼",
            definition="以治疗为主的辅助角色",
            example="打本必带奶妈",
            confidence=0.9,
            sources=["bilibili"],
            status="correct",
        ),
        SlangEntry(
            term="打本",
            category="玩法术语",
            definition="组队刷副本",
            example="天天打本",
            confidence=0.8,
            sources=["tieba"],
            status="modified",
        ),
        SlangEntry(
            term="噪声词",
            category="其他",
            definition="x",
            example="x",
            confidence=0.1,
            sources=[],
            status="incorrect",  # 应被丢弃
        ),
    ]
    seeds_csv = tmp_path / "known_slang.csv"

    affected = ingest_reviewed(reviewed, seeds_csv)

    assert affected == 2, "只应回灌 correct/modified 两条"
    assert seeds_csv.is_file()
    text = seeds_csv.read_text(encoding="utf-8")
    assert "奶妈" in text and "打本" in text
    assert "噪声词" not in text, "incorrect 的词不应进入种子词典"
    # 表头顺序契约
    assert text.splitlines()[0] == "term,definition,category"
