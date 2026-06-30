"""命令行总入口（CLI）。

本模块是整条「游戏黑话自动挖掘流水线 v2」的统一入口，用 argparse 暴露三个
子命令，忠实呼应产品流程图：

    输入三源 → 自动挖掘 → 3 个 Agent 串行审查 → 输出 xlsx + 人工审核 → 闭环回灌

子命令：
    * ``mine``      读评论 → N-gram+PMI+左右熵 自动挖掘 → 存候选（json）
    * ``run``       端到端：挖掘 → Agent1/2/3 串行 → 导出 review xlsx 到 outputs/
    * ``feedback``  把人工审核过的 xlsx 回灌「已确认黑话词典」种子，完成闭环

设计要点：
    * 默认读取仓库根的 ``config/config.yaml``，``--config`` 可覆盖。
    * 所有路径在落盘时解析为「绝对路径」，避免受当前工作目录影响。
    * 友好的中文进度输出（评论数 / 候选数 / 终审通过数 / 端到端耗时）。
    * 离线 mock 模式由 config.llm.offline 控制，无 API key 也能端到端跑通。

依赖的下游模块按统一 SPEC 约定的函数签名调用（见各 import 处注释）：
    * ``slang_miner.mining.miner.mine_candidates``
    * ``slang_miner.pipeline.run_pipeline``
    * ``slang_miner.review.exporter.export_review_xlsx``
    * ``slang_miner.feedback.apply_feedback``

Python 3.9 兼容。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

# 仓库根目录：本文件位于 <root>/src/slang_miner/cli.py，向上 3 级即仓库根。
REPO_ROOT = Path(__file__).resolve().parents[2]
# 默认配置文件路径（绝对）。
DEFAULT_CONFIG = REPO_ROOT / "config" / "config.yaml"


# --------------------------------------------------------------------------- #
# 配置加载与路径解析
# --------------------------------------------------------------------------- #
def load_config(config_path: Path) -> Dict[str, Any]:
    """加载 YAML 配置并做最小校验。

    Args:
        config_path: 配置文件路径（可为相对或绝对）。

    Returns:
        解析后的配置字典。

    Raises:
        SystemExit: 配置文件缺失或解析失败时，给出友好中文错误后退出。
    """
    try:
        import yaml  # 延迟导入：仅在真正需要时依赖 pyyaml
    except ImportError:  # pragma: no cover - 缺依赖时的友好提示
        _fatal("缺少依赖 pyyaml，请先安装：pip install pyyaml")

    path = config_path if config_path.is_absolute() else (REPO_ROOT / config_path)
    if not path.is_file():
        _fatal(f"找不到配置文件：{path}")

    try:
        with path.open("r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001 - 统一转成友好错误
        _fatal(f"配置文件解析失败：{path}\n  原因：{exc}")

    if not isinstance(cfg, dict):
        _fatal(f"配置文件格式非法（应为映射/字典）：{path}")

    # 写回解析所用的绝对路径，便于下游模块复用而无需再次拼接。
    cfg.setdefault("_meta", {})["config_path"] = str(path)
    cfg["_meta"]["repo_root"] = str(REPO_ROOT)
    return cfg


def resolve_path(value: str) -> Path:
    """把配置里的相对路径解析为相对仓库根的绝对路径。"""
    p = Path(value)
    return p if p.is_absolute() else (REPO_ROOT / p)


def _get(cfg: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """安全地按层级取嵌套配置值，任一层缺失则返回 default。"""
    cur: Any = cfg
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def _fatal(msg: str) -> None:
    """打印友好中文错误并以非零码退出。"""
    print(f"[错误] {msg}", file=sys.stderr)
    raise SystemExit(1)


def _info(msg: str) -> None:
    """打印进度信息（统一带前缀，便于在日志中识别）。"""
    print(f"[流水线] {msg}", flush=True)


def _to_jsonable(obj: Any) -> Any:
    """把 dataclass / 嵌套结构转成可 json 序列化的纯数据（不就地修改原对象）。"""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


def load_comments(path: Path) -> List["Any"]:
    """从 jsonl 读取评论，转成 ``Comment`` 列表。

    每行一个 JSON 对象，至少含 ``id`` / ``source`` / ``text``，``ts`` 可选。
    非法行会被跳过并计数提示，绝不静默吞掉（呼应「不信任外部数据」原则）。
    """
    from slang_miner.schema import Comment  # 延迟导入，避免循环依赖

    if not path.is_file():
        _fatal(f"找不到评论数据文件：{path}")

    comments: List[Comment] = []
    skipped = 0
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                comments.append(
                    Comment(
                        id=str(obj["id"]),
                        source=str(obj.get("source", "")),
                        text=str(obj["text"]),
                        ts=str(obj.get("ts", "")),
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                skipped += 1
                print(
                    f"[警告] 跳过第 {lineno} 行非法评论：{exc}",
                    file=sys.stderr,
                )
    if skipped:
        print(f"[警告] 共跳过 {skipped} 行非法评论。", file=sys.stderr)
    return comments


def load_entities(path: Path) -> List[Dict[str, str]]:
    """从 csv 读取「精灵名/技能名/官方术语」过滤表为行字典列表。

    列约定：``term,type``。返回元素形如 ``{"term": ..., "type": ...}``。
    文件缺失时返回空表（不致命：仅意味着不做专有名词过滤）。
    """
    import csv  # 延迟导入

    if not path.is_file():
        print(f"[警告] 找不到 entities 过滤表，跳过专有名词过滤：{path}", file=sys.stderr)
        return []

    rows: List[Dict[str, str]] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            term = (raw.get("term") or "").strip()
            if term:
                rows.append({"term": term, "type": (raw.get("type") or "").strip()})
    return rows


# --------------------------------------------------------------------------- #
# 子命令实现
# --------------------------------------------------------------------------- #
def cmd_mine(args: argparse.Namespace) -> int:
    """子命令 ``mine``：读评论 → 自动挖掘 → 存候选 json。"""
    from slang_miner.mining import mine  # mine(comments, entities, config)

    t0 = time.perf_counter()
    cfg = load_config(Path(args.config))

    comments_path = resolve_path(_get(cfg, "paths", "comments", default=""))
    entities_path = resolve_path(_get(cfg, "paths", "entities", default=""))
    seeds_path = resolve_path(_get(cfg, "paths", "seeds", default=""))
    out_dir = resolve_path(_get(cfg, "paths", "out_dir", default="outputs"))
    out_dir.mkdir(parents=True, exist_ok=True)

    _info("第 1 步｜读取输入三源")
    comments = load_comments(comments_path)
    entities = load_entities(entities_path)
    _info(f"  评论数：{len(comments)}（来源文件 {comments_path.name}）")
    _info(f"  过滤表（精灵/技能/官方术语）：{entities_path.name}（{len(entities)} 条）")
    _info(f"  种子词典（已确认黑话）：{seeds_path.name}")

    _info("第 2 步｜自动挖掘（N-gram + PMI + 左右熵），并过滤专有名词")
    # SPEC：mine(comments, entities, config) -> List[Candidate]
    candidates = mine(comments, entities, cfg)
    _info(f"  候选数：{len(candidates)}")

    out_path = Path(args.out) if args.out else (out_dir / "candidates.json")
    out_path = out_path if out_path.is_absolute() else (REPO_ROOT / out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(_to_jsonable(candidates), fh, ensure_ascii=False, indent=2)

    elapsed = time.perf_counter() - t0
    _info(f"完成｜候选已写入：{out_path}")
    _info(f"耗时：{elapsed:.2f}s（评论 {len(comments)} → 候选 {len(candidates)}）")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """子命令 ``run``：端到端（挖掘 → 3 Agent 串行 → 导出 review xlsx）。"""
    from slang_miner.pipeline import run_pipeline  # 见模块 SPEC
    from slang_miner.review.exporter import export_review_xlsx  # 见模块 SPEC

    t0 = time.perf_counter()
    cfg = load_config(Path(args.config))

    comments_path = resolve_path(_get(cfg, "paths", "comments", default=""))
    out_dir = resolve_path(_get(cfg, "paths", "out_dir", default="outputs"))
    out_dir.mkdir(parents=True, exist_ok=True)

    provider = _get(cfg, "llm", "provider", default="mock")
    offline = _get(cfg, "llm", "offline", default=True)
    _info("端到端流水线启动")
    _info(f"  LLM 模式：provider={provider} offline={offline}")

    _info("第 1 步｜读取评论")
    comments = load_comments(comments_path)
    _info(f"  评论数：{len(comments)}")

    _info("第 2 步｜自动挖掘 + 第 3 步｜Agent1 分类 → Agent2 释义 → Agent3 终审")
    # SPEC：run_pipeline(comments, config) -> List[SlangEntry]
    # pipeline 内部串行执行 挖掘 + 三个 Agent，返回待人工审核的词条列表。
    entries = run_pipeline(comments, config=cfg)
    kept = [e for e in entries if getattr(e, "status", "pending") != "incorrect"]
    _info(f"  终审通过（待人工审核）词条数：{len(entries)}")

    _info("第 4 步｜导出周报 xlsx（供人工填写 correct/modified/incorrect）")
    ts_tag = time.strftime("%Y%m%d_%H%M%S")
    xlsx_path = Path(args.out) if args.out else (out_dir / f"review_{ts_tag}.xlsx")
    xlsx_path = xlsx_path if xlsx_path.is_absolute() else (REPO_ROOT / xlsx_path)
    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
    # SPEC：export_review_xlsx(entries, out_path) -> None
    export_review_xlsx(entries, xlsx_path)

    elapsed = time.perf_counter() - t0
    _info(f"完成｜review 周报已写入：{xlsx_path}")
    _info(
        "端到端汇总："
        f"评论 {len(comments)} → 待审词条 {len(entries)} "
        f"(非 incorrect {len(kept)}) ｜ 耗时 {elapsed:.2f}s"
    )
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    """子命令 ``feedback``：把人工审核过的 xlsx 回灌种子词典（闭环）。"""
    from slang_miner.feedback import ingest_reviewed  # 见模块 SPEC

    t0 = time.perf_counter()
    cfg = load_config(Path(args.config))

    xlsx_path = Path(args.xlsx)
    xlsx_path = xlsx_path if xlsx_path.is_absolute() else (REPO_ROOT / xlsx_path)
    if not xlsx_path.is_file():
        _fatal(f"找不到待回灌的审核 xlsx：{xlsx_path}")

    seeds_path = resolve_path(_get(cfg, "paths", "seeds", default=""))

    _info("闭环回灌启动")
    _info(f"  审核结果文件：{xlsx_path}")
    _info(f"  目标种子词典：{seeds_path}")

    # SPEC：ingest_reviewed(xlsx_or_entries, seeds_path) -> int（写入种子的确认词数）
    # 仅把状态为 correct / modified 的词条合并进种子词典；incorrect/pending 丢弃。
    affected = ingest_reviewed(xlsx_path, seeds_path)

    elapsed = time.perf_counter() - t0
    _info("完成｜种子词典已更新（下轮挖掘将自动过滤并提升 Agent 准确度）")
    _info(
        f"回灌汇总：回灌确认词（新增+更新，去重后）{affected} 条 "
        f"｜ 耗时 {elapsed:.2f}s"
    )
    return 0


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """构建 argparse 解析器（含三个子命令）。"""
    parser = argparse.ArgumentParser(
        prog="slang-miner",
        description=(
            "游戏玩家社区黑话自动挖掘流水线 v2：\n"
            "  输入三源 → 自动挖掘(N-gram+PMI+左右熵) → 3 Agent 串行审查 "
            "→ 输出 xlsx + 人工审核 → 闭环回灌"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        metavar="PATH",
        help=f"配置文件路径（默认：{DEFAULT_CONFIG}）",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True  # Python 3.9 下需显式置 required

    p_mine = sub.add_parser(
        "mine",
        help="读评论 → 自动挖掘 → 存候选(json)",
        description="读取评论并执行 N-gram+PMI+左右熵 候选挖掘，过滤专有名词后存候选。",
    )
    p_mine.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="候选输出路径（默认：<out_dir>/candidates.json）",
    )
    p_mine.set_defaults(func=cmd_mine)

    p_run = sub.add_parser(
        "run",
        help="端到端：挖掘 → 3 Agent 串行 → 导出 review xlsx",
        description="端到端执行流水线，导出供人工审核的周报 xlsx 到 outputs/。",
    )
    p_run.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="review xlsx 输出路径（默认：<out_dir>/review_<时间戳>.xlsx）",
    )
    p_run.set_defaults(func=cmd_run)

    p_fb = sub.add_parser(
        "feedback",
        help="把人工审核过的 xlsx 回灌种子词典（闭环）",
        description="读取人工填好的审核 xlsx，将 correct/modified 词条回灌已确认黑话词典。",
    )
    p_fb.add_argument(
        "xlsx",
        metavar="XLSX",
        help="人工审核完成的 review xlsx 路径",
    )
    p_fb.set_defaults(func=cmd_feedback)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI 主入口（供 console_scripts 调用）。

    Args:
        argv: 命令行参数列表（默认取 ``sys.argv[1:]``，便于测试注入）。

    Returns:
        进程退出码（0 表示成功）。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:  # pragma: no cover - 交互中断
        print("\n[中断] 用户取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
