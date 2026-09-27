"""执行链路追踪（对话调试面板的数据组装 + inner 块解析，对齐 PRD §4.3）

链路：loop_state.finish 收敛 → build_run_trace() 用本次 run 的真实执行数据
  （目标/路由结论/各步出参与实测耗时/终答 inner 块）组装 trace → 存进
  Task.checkpoint["trace"] 与 Task.output["trace"]（内嵌 JSON，不新增表列，
  无需 alembic 迁移）→ GET /runs/{id} 仅对管理员透出 trace → 前端
  RunTracePanel 按执行时序渲染；历史会话经 run_id 拉取，链路随会话一起加载。

红线：
- 零编造：意图置信度/候选打分只写实测可得的——规则命中记 1.0 并注明「规则关键词
  命中，非模型打分」，LLM/react 直定不写分数；候选智能体只标选中/未选中；
  token 计数 Ollama 未透出就不写字段，前端缺失不渲染；
- inner 块是可选的：终答不带 <<<inner>>> 即只有确定性分段，解析失败降级空分段，
  绝不因解析异常阻断收敛；
- 权限：透出前一律 is_admin_roles() 判定（admin 或 *），普通用户响应里无 trace 键；
- 截断：inner 原文与工具结果摘要分别按 Settings 截断并打标记，防 checkpoint 膨胀。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from office_agent_core.settings import settings

logger = logging.getLogger(__name__)

#: inner 块起止标记（ReAct 提示词模板约定：内部思考链路与对外回答的隔离线）
INNER_OPEN = "<<<inner>>>"
INNER_CLOSE = "<<</inner>>>"
_INNER_RE = re.compile(r"<<<inner>>>(.*?)<<</inner>>>", re.S)

#: inner 块内分段标记 → trace step_type（模板有三段；未知【Step …】归 thought 不丢弃）
_SECTION_KIND = (
    ("【Step 思考】", "thought"),
    ("【Step 路由】", "route"),
    ("【Step 工具调用】", "tool_call"),
)
_SECTION_MARKS = tuple(mark for mark, _ in _SECTION_KIND)

#: trace 步骤类型 → 前端中文label（同一映射前后端同字，后端是唯一出处）
STEP_LABELS = {
    "intent": "意图识别",
    "thought": "Agent思考 Thought",
    "route": "智能体路由匹配",
    "tool_call": "工具调用",
    "tool_result": "工具返回结果",
    "summary": "大模型汇总生成对外回复",
}


def is_admin_roles(roles: list[str]) -> bool:
    """是否有调试面板可见权限（admin 或通配 *；展示门控，数据门控在 api 层同口径复用）。

    公开供 api._run_view 复用：同一行为只允许一个实现，不在端点里另写一份判定。
    """
    return "*" in roles or "admin" in roles


def split_inner(text: str) -> tuple[str, str]:
    """终答文本 → (inner 原文, 对外正文)：无标记返回 ("", 全文)，多块按序拼接。

    对外正文去掉 inner 块后 strip；inner 块只做原文透出，不做语义改写。
    """
    chunks = _INNER_RE.findall(text or "")
    inner = "\n".join(part.strip() for part in chunks if part.strip())
    public = _INNER_RE.sub("", text or "").strip()
    return inner, public


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    """超长截断（带「已截断」标记，绝不伪装完整；limit<=0 视为不截断）。"""
    if limit > 0 and len(text) > limit:
        return text[:limit] + "…（已截断）", True
    return text, False


def parse_inner_sections(inner: str) -> list[dict[str, Any]]:
    """inner 原文 → 分段列表（按 【Step 思考】/【Step 路由】/【Step 工具调用】标记切分）。

    无标记的整块视为一段 thought（含模型自由发挥的思考不断链）；单段超长按
    TRACE_INNER_MAX_CHARS 截断并标记。纯函数，可单测、可回放。
    """
    body = (inner or "").strip()
    if not body:
        return []
    hits = [(body.find(mark), mark) for mark in _SECTION_MARKS if body.find(mark) >= 0]
    if not hits:
        text, truncated = truncate_text(body, settings.TRACE_INNER_MAX_CHARS)
        return [_section("thought", text, truncated)]
    hits.sort()
    out: list[dict[str, Any]] = []
    for pos, (at, mark) in enumerate(hits):
        start = at + len(mark)
        end = hits[pos + 1][0] if pos + 1 < len(hits) else len(body)
        text, truncated = truncate_text(body[start:end].strip(), settings.TRACE_INNER_MAX_CHARS)
        out.append(_section(_kind_of(mark), text, truncated))
    return out


def _kind_of(mark: str) -> str:
    """分段标记 → step_type（未知标记归 thought，宁收不丢）。"""
    for name, kind in _SECTION_KIND:
        if name == mark:
            return kind
    return "thought"


def _section(kind: str, content: str, truncated: bool) -> dict[str, Any]:
    """inner 分段条目（content 为空的段不收，防 LLM 空标记刷屏）。"""
    return {
        "step_type": kind,
        "step_label": STEP_LABELS.get(kind, kind),
        "content": content,
        "truncated": truncated,
    }


@dataclass
class TraceInput:
    """组装一次 run 执行链路所需的真实执行数据（全部来自执行期实测，无模型自述）。

    steps: loop_state.results 全量出参 {步号: outcome}（outcome 含 tool/status/
      args/result/latency_ms）；plan: checkpoint 持久化计划（步数与工具名核对用）；
    candidates: 已装载智能体名（选中标 ✅，其余标未选中，不写打分）；
    route_note: 路由结论（一句话，如「规则关键词命中」/「ReAct 逐步编排」）。
    """

    run_id: str = ""
    session_id: str = ""
    goal: str = ""
    agent: str = ""
    planner_source: str = "rule"
    route_note: str = ""
    candidates: list[str] = field(default_factory=list)
    steps: dict[int, dict[str, Any]] = field(default_factory=dict)
    plan: list[dict[str, Any]] = field(default_factory=list)
    inner_sections: list[dict[str, Any]] = field(default_factory=list)
    public_answer: str = ""
    error: str = ""
    total_ms: int = 0


def build_run_trace(data: TraceInput) -> dict[str, Any]:
    """执行链路组装（确定性：意图 → 思考 → 路由 → 每步工具调用/返回 → 汇总 → 性能指标）。

    思考段优先用终答 inner 解析结果（模型亲笔）；无 inner 时用路由结论兜底一句，
    并注明「规则/执行期组装，非模型自述」——两种来源在来源字段里写清楚。
    """
    entries: list[dict[str, Any]] = []
    entries.append(_intent_entry(data))
    entries.extend(_thought_entries(data))
    entries.append(_route_entry(data))
    for index in sorted(data.steps):
        entries.extend(_tool_entries(data, index))
    entries.append(_summary_entry(data))
    return {
        "session_id": data.session_id,
        "run_id": data.run_id,
        "agent": data.agent,
        "goal": data.goal,
        "planner_source": data.planner_source,
        "steps": entries,
        "total_ms": data.total_ms,
    }


def _intent_entry(data: TraceInput) -> dict[str, Any]:
    """Step1 意图识别：用户原话 + 命中智能体；置信度只写诚实的（规则=1.0 注明非打分）。"""
    if data.planner_source == "rule":
        content = f"用户输入：{data.goal}\n识别意图：{data.agent}｜置信度：1.0（规则关键词命中，非模型打分）"
    else:
        content = f"用户输入：{data.goal}\n识别意图：{data.agent}（模型直定，无打分）"
    return {
        "step_no": 1,
        "step_type": "intent",
        "step_label": STEP_LABELS["intent"],
        "content": content,
    }


def _thought_entries(data: TraceInput) -> list[dict[str, Any]]:
    """Step2 思考：有 inner 用模型原文（逐段编号），无 inner 用执行期一句话兜底。"""
    if data.inner_sections:
        return [
            {
                "step_no": 2,
                "step_type": item["step_type"],
                "step_label": f"{STEP_LABELS['thought']}（模型原文{pos}）",
                "content": str(item.get("content") or ""),
                "truncated": bool(item.get("truncated")),
            }
            for pos, item in enumerate(data.inner_sections, start=1)
        ]
    return [
        {
            "step_no": 2,
            "step_type": "thought",
            "step_label": STEP_LABELS["thought"],
            "content": f"{data.route_note}（执行期组装，非模型自述）",
        }
    ]


def _route_entry(data: TraceInput) -> dict[str, Any]:
    """Step3 路由：候选清单只标选中/未选中，不编造匹配得分。"""
    lines = ["候选智能体列表："]
    for name in data.candidates:
        mark = "✅（选中）" if name == data.agent else "（未选中）"
        lines.append(f"- {name}{mark}")
    if not data.candidates:
        lines.append(f"- {data.agent}✅（选中）")
    return {
        "step_no": 3,
        "step_type": "route",
        "step_label": STEP_LABELS["route"],
        "content": "\n".join(lines),
    }


def _tool_entries(data: TraceInput, index: int) -> list[dict[str, Any]]:
    """单步工具调用 + 工具返回：入参/出参/耗时全部取真实出参，失败步如实标状态。"""
    outcome = data.steps.get(index) or {}
    tool = str(outcome.get("tool") or "")
    status = str(outcome.get("status") or "")
    cost = _to_int(outcome.get("latency_ms"))
    call = {
        "step_no": 4 + index * 2,
        "step_type": "tool_call",
        "step_label": STEP_LABELS["tool_call"],
        "content": f"工具名称：{tool}\n入参：{outcome.get('args')}",
        "tool_name": tool,
        "params": outcome.get("args"),
        "time_cost_ms": cost,
    }
    result_text, truncated = truncate_text(
        str(outcome.get("result")), settings.TRACE_RESULT_MAX_CHARS
    )
    result = {
        "step_no": 5 + index * 2,
        "step_type": "tool_result",
        "step_label": STEP_LABELS["tool_result"],
        "content": f"工具原始返回（{status}）：{result_text}",
        "time_cost_ms": 0,
        "truncated": truncated,
    }
    return [call, result]


def _summary_entry(data: TraceInput) -> dict[str, Any]:
    """Step6 汇总 + 性能指标：对外正文（截断）与本次总耗时；token 未透出不写。"""
    answer, truncated = truncate_text(
        data.public_answer or data.error, settings.TRACE_RESULT_MAX_CHARS
    )
    content = f"对外输出文本：{answer}\n本次总耗时：{data.total_ms}ms"
    if truncated:
        content += "（正文已截断）"
    return {
        "step_no": 1000,
        "step_type": "summary",
        "step_label": STEP_LABELS["summary"],
        "content": content,
        "time_cost_ms": data.total_ms,
        "truncated": truncated,
    }


def _to_int(value: Any) -> int:
    """耗时转 int（脏数据给 0，读取路径绝不抛错）。"""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


#: 路由结论一句话（trace 思考兜底与路由段共用，来源如实不编造打分）
_ROUTE_NOTES = {
    "rule": "规则关键词命中，直达确定性链",
    "llm": "规则未中，模型单轮规划直定",
    "react": "规则未中，ReAct 看真实出参逐步编排",
}


def input_from_state(state: Any, validation_block: dict[str, Any]) -> TraceInput:
    """从 LoopState 执行态组装 trace 输入（组装失败返回空输入→空链路，绝不拖垮收敛）。

    state 取鸭子类型（task/spec/goal/planner_source/results/checkpoint 属性），
    避免 trace 反向依赖 loop_state 形成导入环；loader 实扫失败即空候选不阻断。
    """
    try:
        return _build_trace_input(state, validation_block)
    except Exception:  # 防御：trace 是调试旁路，任何意外不得阻断 run 收敛
        logger.warning("执行链路组装失败，已降级空链路", exc_info=True)
        return TraceInput()


def _build_trace_input(state: Any, validation_block: dict[str, Any]) -> TraceInput:
    """执行态 → TraceInput（字段缺失按空值处理，脏数据不抛错）。"""
    from office_agent_runtime import loader

    try:
        candidates = [spec.name for spec in loader.load_agent_specs()]
    except Exception:
        candidates = []
    block = validation_block if isinstance(validation_block, dict) else {}
    inner = block.get("inner")
    sections = inner.get("sections") if isinstance(inner, dict) else None
    checkpoint = getattr(state, "checkpoint", {}) or {}
    return TraceInput(
        run_id=str(getattr(getattr(state, "task", None), "id", "")),
        goal=str(getattr(state, "goal", "")),
        agent=str(getattr(getattr(state, "spec", None), "name", "")),
        planner_source=str(getattr(state, "planner_source", "") or "rule"),
        route_note=_ROUTE_NOTES.get(str(getattr(state, "planner_source", "")), "未知来源"),
        candidates=candidates,
        steps=dict(getattr(state, "results", {}) or {}),
        plan=list(checkpoint.get("plan") or []),
        inner_sections=list(sections) if isinstance(sections, list) else [],
        public_answer=str(block.get("answer") or ""),
        error=str(checkpoint.get("error") or ""),
        total_ms=_run_total_ms(getattr(state, "task", None)),
    )


def _run_total_ms(task: Any) -> int:
    """本次 run 总耗时（建行到收敛的墙钟差；脏时间给 0 不抛错）。"""
    try:
        created = getattr(task, "created_at", None)
        if created is None:
            return 0
        if created.tzinfo is not None:
            created = created.replace(tzinfo=None)
        delta = (datetime.now(UTC).replace(tzinfo=None) - created).total_seconds()
        return max(0, int(delta * 1000))
    except Exception:
        return 0
