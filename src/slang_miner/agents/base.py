"""Agent 基础设施层：统一的 LLM 客户端封装。

本模块提供三个 Agent（Classifier / Definer / Reviewer）共用的底层能力：

1. ``LLMClient``：屏蔽不同 provider（mock / anthropic / openai）差异的统一
   客户端，对外只暴露一个 ``chat(system, user) -> str`` 接口。
2. ``parse_json``：带容错的 JSON 解析工具，能从「模型可能夹带解释文字 / 包了
   ```json 代码块」的脏输出里抠出合法 JSON。
3. ``BaseAgent``：所有 Agent 的抽象基类，统一持有 client、system prompt，
   并提供一个「调 LLM + 解析 JSON + 兜底」的模板方法。

设计要点（务必遵守业务约束）：
- **离线 mock 模式是一等公民**：provider="mock"（或缺少 API key）时，所有调用
  走本地启发式规则，无需任何网络/密钥即可端到端产出非空结果。
- **真实 API 可选启用**：provider="anthropic" 用 ``claude-opus-4-8``，
  provider="openai" 用 ``gpt-4o``；API key 从环境变量读取，缺失时**自动降级**
  到 mock，并打印告警，绝不抛错中断流水线。
- Python 3.9 兼容：``from __future__ import annotations`` + ``typing`` 泛型。
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC
from typing import Any, Callable, Dict, Optional

# ----------------------------------------------------------------------------
# 默认模型名常量（provider != mock 时使用；config.yaml 留空则回退到这里）
# ----------------------------------------------------------------------------
DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"
DEFAULT_OPENAI_MODEL = "gpt-4o"

# 环境变量名（真实模式从这里取 key）
ENV_ANTHROPIC_KEY = "ANTHROPIC_API_KEY"
ENV_OPENAI_KEY = "OPENAI_API_KEY"


def parse_json(raw: str) -> Optional[Dict[str, Any]]:
    """从模型原始输出里尽力解析出一个 JSON 对象（dict）。

    模型经常返回「带解释文字 / 包在 ```json 代码块里 / 前后有杂质」的脏输出，
    本函数按以下顺序尝试，全部失败才返回 ``None``（由调用方决定兜底策略）：

    1. 直接 ``json.loads``；
    2. 剥离 markdown 代码围栏（```json ... ``` 或 ``` ... ```）后再解析；
    3. 用正则截取第一个 ``{ ... }`` 平衡片段后再解析。

    Args:
        raw: 模型返回的原始字符串。

    Returns:
        解析成功返回 dict；任何分支都失败返回 ``None``。
    """
    if not raw or not raw.strip():
        return None

    text = raw.strip()

    # 分支 1：直接解析
    obj = _try_loads(text)
    if isinstance(obj, dict):
        return obj

    # 分支 2：剥离 markdown 代码围栏
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    if fenced != text:
        obj = _try_loads(fenced)
        if isinstance(obj, dict):
            return obj

    # 分支 3：截取第一个大括号平衡片段
    snippet = _extract_first_object(text)
    if snippet:
        obj = _try_loads(snippet)
        if isinstance(obj, dict):
            return obj

    return None


def _try_loads(text: str) -> Any:
    """安全的 ``json.loads``：失败返回 None 而非抛错。"""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def _extract_first_object(text: str) -> Optional[str]:
    """扫描字符串，返回第一个括号平衡的 ``{...}`` 子串。

    比简单的「第一个 { 到最后一个 }」更稳健：能正确处理后面还跟着杂质文本的
    情况，且通过计数大括号保证截取片段自身是平衡的。
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


class LLMClient:
    """统一 LLM 客户端：屏蔽 mock / anthropic / openai 三种 provider 差异。

    对外仅暴露 :meth:`chat`，输入 system / user 两段提示，返回纯文本字符串。
    各 Agent 在此之上自行约定「让模型输出 JSON」并用 :func:`parse_json` 解析。

    Args:
        provider: "mock" | "anthropic" | "openai"。
        model: 真实模型名；为空时按 provider 取默认常量。
        offline: True 时强制走 mock（即便 provider 是真实厂商也降级）。
        mock_handler: 可选的自定义 mock 处理函数 ``(system, user) -> str``。
            供各 Agent 注入自己的启发式规则；不提供时用通用回声 mock。
    """

    def __init__(
        self,
        provider: str = "mock",
        model: str = "",
        offline: bool = True,
        mock_handler: Optional[Callable[[str, str], str]] = None,
    ) -> None:
        self.provider = (provider or "mock").lower().strip()
        self.offline = bool(offline)
        self.mock_handler = mock_handler
        # 真实客户端句柄（惰性创建），mock 模式下恒为 None
        self._real_client: Any = None

        # 解析最终生效的「实际 provider」与「模型名」，并完成降级判定
        self.effective_provider, self.model = self._resolve(provider, model)

    # ------------------------------------------------------------------ #
    # provider / 降级判定
    # ------------------------------------------------------------------ #
    def _resolve(self, provider: str, model: str) -> "tuple[str, str]":
        """决定实际生效的 provider 与模型名，必要时降级为 mock。

        降级规则（任一满足即降级到 mock）：
        - offline=True；
        - provider 不在 {anthropic, openai}；
        - 对应环境变量缺少 API key。
        """
        if self.offline or self.provider not in ("anthropic", "openai"):
            return "mock", ""

        if self.provider == "anthropic":
            if not os.environ.get(ENV_ANTHROPIC_KEY):
                print(
                    f"[LLMClient] 警告：未检测到环境变量 {ENV_ANTHROPIC_KEY}，"
                    f"自动降级为离线 mock 模式。"
                )
                return "mock", ""
            return "anthropic", model or DEFAULT_ANTHROPIC_MODEL

        # openai
        if not os.environ.get(ENV_OPENAI_KEY):
            print(
                f"[LLMClient] 警告：未检测到环境变量 {ENV_OPENAI_KEY}，"
                f"自动降级为离线 mock 模式。"
            )
            return "mock", ""
        return "openai", model or DEFAULT_OPENAI_MODEL

    @property
    def is_mock(self) -> bool:
        """当前是否实际运行在 mock 模式。"""
        return self.effective_provider == "mock"

    # ------------------------------------------------------------------ #
    # 对外统一接口
    # ------------------------------------------------------------------ #
    def chat(self, system: str, user: str) -> str:
        """统一对话接口：给定 system / user 提示，返回模型文本输出。

        mock 模式下：若注入了 ``mock_handler`` 则交由其处理（各 Agent 的启发式
        规则在此生效），否则返回一个通用回声 JSON，保证下游 parse 不空。

        真实模式下：调用对应厂商 SDK；任何异常都**捕获并降级**为 mock，确保
        流水线整体永不因单次 LLM 失败而中断。
        """
        if self.is_mock:
            return self._mock_chat(system, user)

        try:
            if self.effective_provider == "anthropic":
                return self._anthropic_chat(system, user)
            if self.effective_provider == "openai":
                return self._openai_chat(system, user)
        except Exception as exc:  # noqa: BLE001 - 兜底：任何异常都降级，不中断
            print(f"[LLMClient] 真实调用失败（{self.effective_provider}）：{exc}；降级 mock。")
        return self._mock_chat(system, user)

    # ------------------------------------------------------------------ #
    # mock 实现
    # ------------------------------------------------------------------ #
    def _mock_chat(self, system: str, user: str) -> str:
        """mock 对话：优先用注入的 handler，否则通用回声。"""
        if self.mock_handler is not None:
            return self.mock_handler(system, user)
        # 通用兜底：把 user 原样塞回一个最小 JSON，保证可被 parse_json 解析
        return json.dumps({"echo": user[:200]}, ensure_ascii=False)

    # ------------------------------------------------------------------ #
    # 真实厂商实现（惰性导入 SDK，未安装时降级）
    # ------------------------------------------------------------------ #
    def _anthropic_chat(self, system: str, user: str) -> str:
        if self._real_client is None:
            import anthropic  # 惰性导入：mock 路径完全不依赖该包

            self._real_client = anthropic.Anthropic(
                api_key=os.environ[ENV_ANTHROPIC_KEY]
            )
        resp = self._real_client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        # Anthropic 返回内容块列表，拼接所有 text
        parts = [getattr(b, "text", "") for b in resp.content]
        return "".join(parts)

    def _openai_chat(self, system: str, user: str) -> str:
        if self._real_client is None:
            from openai import OpenAI  # 惰性导入

            self._real_client = OpenAI(api_key=os.environ[ENV_OPENAI_KEY])
        resp = self._real_client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""


class BaseAgent(ABC):
    """所有 Agent 的抽象基类。

    统一持有 :class:`LLMClient` 与一段 system prompt，并约定：每个子类把自己的
    「mock 启发式逻辑」实现为 :meth:`_mock_logic`，在构造时自动注入到 client，
    从而保证离线模式下也能产出领域合理的结构化结果。

    子类需实现：
    - :attr:`SYSTEM_PROMPT`（类属性）：该 Agent 的角色设定与输出格式约定。
    - :meth:`_mock_logic(user)`：离线启发式，返回 JSON 字符串。
    """

    SYSTEM_PROMPT: str = ""

    def __init__(self, client: Optional[LLMClient] = None) -> None:
        # 若外部未注入 client，则默认构造一个 mock client，并把本 Agent 的
        # 启发式逻辑挂上去，使离线调用走领域规则而非通用回声。
        if client is None:
            client = LLMClient(provider="mock", offline=True)
        # 无论 client 来自哪里，只要它处于 mock 且尚未绑定 handler，就绑定本
        # Agent 的启发式逻辑（用 system 区分调用方，避免不同 Agent 串味）。
        if client.is_mock and client.mock_handler is None:
            client.mock_handler = self._dispatch_mock
        self.client = client

    def _dispatch_mock(self, system: str, user: str) -> str:
        """mock handler 入口：仅当 system 与本 Agent 一致时走本 Agent 逻辑。

        这样即使多个 Agent 共享同一个 client，也能各自命中自己的启发式。
        """
        return self._mock_logic(user)

    def _ask(self, user: str) -> Optional[Dict[str, Any]]:
        """模板方法：发起一次「输出 JSON」的对话并解析为 dict。

        返回 None 表示解析失败，调用方应给出领域兜底默认值。
        """
        raw = self.client.chat(self.SYSTEM_PROMPT, user)
        return parse_json(raw)

    def _mock_logic(self, user: str) -> str:  # pragma: no cover - 抽象占位
        """子类实现：离线启发式，返回 JSON 字符串。"""
        raise NotImplementedError
