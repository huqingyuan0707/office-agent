"""LLM 函数调用规划器（R1：OpenAI 兼容 chat.completions，含降级链入口）

链路：Runner 检查 AgentSpec.llm → LlmFunctionCallPlanner.from_spec(spec)
      → plan(goal, tools) 用 registry 白名单构造 tools 参数 → httpx 出站
      → tool_calls 逐轮收集提议 → 返回 list[PlannerStep]。
      R2 增 answer(goal, observations)：把工具返回 JSON 交给 LLM 合成中文终答
      （validation.finalize_answer 的输入源，出站链路与 plan 共用 _post_chat）。
      出站失败（未配置 / 401 / 超时 / 网络错）由调用方决定降级，
      本层只抛 ``LlmPlanError``（marker，Runner 识别后转 RulePlanner）。

Provider 配置：环境变量 ``LLM_PROVIDERS``（JSON，与 LINKAGE_PROVIDERS 同构）：
  {"default":{"base_url":"https://api.openai.com/v1","model":"gpt-4o-mini","api_key":"sk-..."}}
  Ollama 即 base_url=http://127.0.0.1:11434/v1，api_key 可为空。

红线：
- 不改 packages/core；凭据只走环境变量，绝不入库入码；
- planner 只「提议」tool+args，永远不绕过 executor.call；
- 工具白名单取 registry 与 AgentSpec.tools 的交集，越权名直接过滤，
  不存在的工具抛 LlmPlanError（由 Runner 决定降级）。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from office_agent_core import registry as tool_registry
from office_agent_runtime.spec import AgentSpec, PlannerStep

logger = logging.getLogger(__name__)

#: LLM 调用的独立超时（秒；默认 30s，Ollama 本地可放宽）
_DEFAULT_TIMEOUT_SECONDS = 30.0

#: LLM_PROVIDERS 环境变量名（与 LINKAGE_PROVIDERS 同构但互不影响）
_LLM_ENV_KEY = "LLM_PROVIDERS"


class LlmPlanError(Exception):
    """LLM 规划阶段的可降级错误（Runner 识别该异常后转 RulePlanner；
    若无 rules 则由 Runner 上抛可操作中文错误给 create_run 链路）。"""

    def __init__(self, msg: str) -> None:
        super().__init__(msg)
        self.msg = msg


@dataclass(frozen=True)
class _ProviderCfg:
    """LLM profile 的最小配置（含凭据，仅内存；测试可注入 client 做 MockTransport）。"""

    base_url: str
    model: str
    api_key: str
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS

    @property
    def endpoint(self) -> str:
        """chat.completions 的完整 URL（base_url 以 /v1 结尾约定）。"""
        base = self.base_url.rstrip("/")
        return f"{base}/chat/completions"


def _parse_providers() -> dict[str, _ProviderCfg]:
    """解析 ``LLM_PROVIDERS`` 环境变量（非法 JSON / 非对象 / 缺字段抛错，绝不静默）。"""
    raw = os.environ.get(_LLM_ENV_KEY, "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LlmPlanError(f"环境变量 LLM_PROVIDERS 不是合法 JSON：{exc.msg}") from exc
    if not isinstance(payload, dict):
        raise LlmPlanError("LLM_PROVIDERS 必须是 {profile_name: {...}} 形式的 JSON 对象")
    out: dict[str, _ProviderCfg] = {}
    for name, value in payload.items():
        if not isinstance(value, dict):
            raise LlmPlanError(f"LLM_PROVIDERS 的 profile「{name}」值必须是键值对对象")
        base_url = str(value.get("base_url") or "").strip()
        model = str(value.get("model") or "").strip()
        if not base_url:
            raise LlmPlanError(f"LLM profile「{name}」缺少 base_url")
        if not model:
            raise LlmPlanError(f"LLM profile「{name}」缺少 model")
        out[str(name)] = _ProviderCfg(
            base_url=base_url,
            model=model,
            api_key=str(value.get("api_key") or ""),
            timeout_seconds=float(value.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS),
        )
    return out


def _provider_of(name: str) -> _ProviderCfg:
    """取命名 profile；找不到抛 LlmPlanError（Runner 决定降级）。"""
    providers = _parse_providers()
    if name not in providers:
        known = "/".join(sorted(providers)) or "（未配置）"
        raise LlmPlanError(f"LLM profile「{name}」未在 {_LLM_ENV_KEY} 中配置，可用：{known}")
    return providers[name]


def _tool_schemas(spec: AgentSpec) -> list[dict[str, Any]]:
    """构造 OpenAI tools 参数：取 registry 与 AgentSpec.tools 白名单的交集。

    不在 registry 里的名字直接过滤掉，planner 拿不到越权选项——
    最终 executor.call 仍会按白名单硬拦，这里是前置裁剪。
    """
    schemas: list[dict[str, Any]] = []
    for tool_name in spec.tools:
        reg = tool_registry.maybe_get(tool_name)
        if reg is None:
            logger.warning("LLM 规划器忽略白名单里未注册的工具 %s（registry 找不到）", tool_name)
            continue
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": reg.name,
                    "description": reg.description,
                    "parameters": reg.params,
                },
            }
        )
    return schemas


class LlmFunctionCallPlanner:
    """OpenAI 兼容 chat.completions 函数调用规划器。

    用法（Runner 里）：
        planner = LlmFunctionCallPlanner.from_spec(spec)
        steps = await planner.plan(goal)

    出站失败一律抛 LlmPlanError，由调用方决定降级。
    """

    def __init__(
        self,
        spec: AgentSpec,
        *,
        profile_name: str = "default",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._spec = spec
        self._profile_name = (spec.llm or profile_name).strip() or "default"
        self._client = client
        self._owns_client = client is None

    @classmethod
    def from_spec(
        cls,
        spec: AgentSpec,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> LlmFunctionCallPlanner:
        """按 AgentSpec 构造（spec.llm 空字符串 → 用 "default" profile 名）。"""
        return cls(spec, profile_name=spec.llm or "default", client=client)

    # ---------------- 出站调用 ----------------

    def _ensure_client(self, cfg: _ProviderCfg) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=cfg.timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        """关闭自有客户端（注入的由调用方负责）。"""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def plan(self, goal: str) -> list[PlannerStep]:
        """调 LLM 产出 tool_calls → 转 PlannerStep 列表。

        LLM 返回文本（无 tool_calls）→ 抛 LlmPlanError（Runner 降级）；
        单轮可返回多条 tool_calls（并发提议），一律逐条展开。
        """
        cfg = _provider_of(self._profile_name)
        tools = _tool_schemas(self._spec)
        if not tools:
            raise LlmPlanError(
                f"智能体 {self._spec.name} 的工具白名单里没有已注册的工具，"
                f"LLM 规划器无法构造 tools 参数"
            )

        messages: list[dict[str, Any]] = []
        if self._spec.system_prompt:
            messages.append({"role": "system", "content": self._spec.system_prompt})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"请为目标规划所需的工具调用步骤。你只能使用提供的 tools，"
                    f"每一步通过 tool_calls 返回 tool 名与 JSON args。\n目标：{goal}"
                ),
            }
        )

        payload = {
            "model": cfg.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }

        headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"

        data = await self._post_chat(cfg, payload, headers)
        return _extract_tool_calls(data, allowed_tools=frozenset(self._spec.tools))

    async def answer(self, goal: str, observations: list[dict[str, Any]]) -> str:
        """R2 终答合成：把本次 run 的工具返回 JSON 交给 LLM 生成中文回答。

        这是回答数值校验（validation.validate_answer_numbers）的输入源——prompt 明确
        要求回答中的每个数字必须直接来自工具返回 JSON、禁止推算与编造；但 LLM 不可信，
        最终以独立校验函数的结论为准（未通过则标注失信，不删回答）。
        出站失败一律抛 LlmPlanError，由调用方（validation.finalize_answer）降级。
        """
        cfg = _provider_of(self._profile_name)
        messages: list[dict[str, Any]] = []
        if self._spec.system_prompt:
            messages.append({"role": "system", "content": self._spec.system_prompt})
        messages.append({"role": "user", "content": f"目标：{goal}"})
        messages.append(
            {
                "role": "user",
                "content": (
                    "以下是本次运行各步骤工具返回的 JSON（回答的唯一数据来源）：\n"
                    f"{json.dumps(observations, ensure_ascii=False)}\n"
                    "请据此用中文回答目标。回答中出现的每个数字必须直接来自上述 JSON 字段，"
                    "禁止推算、汇总或编造任何数字。"
                ),
            }
        )
        headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        # 终答合成不带 tools：要的是文本回答，不是继续提议工具调用
        data = await self._post_chat(cfg, {"model": cfg.model, "messages": messages}, headers)
        content = _extract_content(data).strip()
        if not content:
            raise LlmPlanError("LLM 未返回文本回答，无法生成最终答复")
        return content

    async def _post_chat(
        self, cfg: _ProviderCfg, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        """出站 chat.completions（plan / answer 共用）：状态码分级 + JSON 解析。"""
        client = self._ensure_client(cfg)
        try:
            resp = await client.post(cfg.endpoint, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LlmPlanError(
                f"LLM profile「{self._profile_name}」调用超时（>{cfg.timeout_seconds}s）"
            ) from exc
        except (httpx.HTTPError, OSError) as exc:
            raise LlmPlanError(
                f"LLM profile「{self._profile_name}」不可达（{type(exc).__name__}）"
            ) from exc

        if resp.status_code == 401:
            raise LlmPlanError(
                f"LLM profile「{self._profile_name}」返回 401（凭据错误或未授权），"
                f"请检查 api_key 或 Ollama 是否开启鉴权"
            )
        if resp.status_code >= 500:
            raise LlmPlanError(
                f"LLM profile「{self._profile_name}」返回 HTTP {resp.status_code}（上游故障）"
            )
        if resp.status_code >= 400:
            raise LlmPlanError(
                f"LLM profile「{self._profile_name}」返回 HTTP {resp.status_code}（请求非法）"
            )

        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LlmPlanError("LLM 返回非 JSON 响应") from exc
        if not isinstance(data, dict):
            raise LlmPlanError("LLM 响应结构非对象")
        return data


def _extract_tool_calls(
    data: dict[str, Any], *, allowed_tools: frozenset[str]
) -> list[PlannerStep]:
    """从 chat.completions 响应里抽 tool_calls 并校验白名单。

    - 没有 choices / 没有 message / message 无 tool_calls → 抛 LlmPlanError；
    - tool_calls 里非法 JSON → 抛 LlmPlanError；
    - tool 名不在白名单 → 抛 LlmPlanError（让 Runner 决定降级，
      而不是静默跳过——跳过会导致后续步骤上下文错位）。
    """
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlmPlanError("LLM 响应缺少 choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise LlmPlanError("LLM choices[0] 结构非法")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise LlmPlanError("LLM 响应缺少 message")
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        content = str(message.get("content") or "").strip()
        hint = f"（LLM 返回了纯文本：{content[:80]}）" if content else ""
        raise LlmPlanError(f"LLM 没有返回任何 tool_calls，无法规划{hint}")

    steps: list[PlannerStep] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            raise LlmPlanError("LLM tool_calls 项结构非法")
        func = call.get("function")
        if not isinstance(func, dict):
            raise LlmPlanError("LLM tool_calls[].function 结构非法")
        tool_name = str(func.get("name") or "").strip()
        raw_args = func.get("arguments")
        if not tool_name:
            raise LlmPlanError("LLM tool_calls[].function.name 为空")
        if not isinstance(raw_args, str):
            raise LlmPlanError(f"LLM tool_calls[{tool_name}].arguments 不是字符串")
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError as exc:
            raise LlmPlanError(
                f"LLM tool_calls[{tool_name}].arguments 不是合法 JSON：{exc.msg}"
            ) from exc
        if not isinstance(args, dict):
            raise LlmPlanError(f"LLM tool_calls[{tool_name}].arguments 解析后不是对象")

        if tool_name not in allowed_tools:
            raise LlmPlanError(
                f"LLM 提议了白名单外的工具「{tool_name}」，"
                f"白名单是 {'、'.join(sorted(allowed_tools))}"
            )
        steps.append(PlannerStep(tool=tool_name, args=args))
    return steps


def _extract_content(data: dict[str, Any]) -> str:
    """抽 chat.completions 的文本回答（无 choices / message 给空串，由调用方收口）。"""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, dict):
        return ""
    message = choice.get("message")
    if not isinstance(message, dict):
        return ""
    return str(message.get("content") or "")
