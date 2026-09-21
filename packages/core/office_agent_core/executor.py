"""工具执行器（超时 + 幂等重试 + 熔断 + 全量审计）

链路：policy.ensure_allowed 放行 → 参数 Schema 校验 → 熔断闸门
      → asyncio.wait_for(实现, 超时) → 失败按幂等性重试 → 审计留痕 → 返回结果。

失败分级（关键口径，避免把正常业务结果当故障熔断）：
- BusinessError（对象不存在/状态非法/无据等）：业务拒绝，**不重试、不计熔断**，原码上抛；
- UpstreamError（跨系统上游不可达/超时/上游 5xx）：**计熔断失败**，幂等工具线性退避重试，
  尝试耗尽后以 5001 收口——上游故障与「本地工具调用失败」（4008）在错误码上不糊成一个。

红线：
- 非幂等工具恒只调一次（重复资金/资损动作的口子）；熔断开闸直接 4007 快失败；
- 好/坏两条路径都必须写审计（工具调用透明可回放）；
- 远程工具的出站头与溯源由 linkage 层负责，本层不重复拼装，也不认识对端是谁。
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from office_agent_core import audit, linkage, policy, registry
from office_agent_core.contracts import Provenance, ToolContext, ToolSpec, validate_args
from office_agent_core.errors import BusinessError, ErrorCode, UpstreamError
from office_agent_core.observability import record
from office_agent_core.settings import settings


@dataclass
class _Breaker:
    """单工具熔断状态（进程内，按工具名隔离）。"""

    failures: int = 0
    opened_at: float = 0.0
    last_error: str = ""
    history: list[int] = field(default_factory=list)  # 最近若干次耗时，供前端画趋势


_breakers: dict[str, _Breaker] = {}
_breaker_lock = threading.Lock()
_HISTORY_MAX = 10


# ---------------- 熔断闸门 ----------------


def _breaker_of(name: str) -> _Breaker:
    """惰性建熔断位（无锁读优先，双检写入）。"""
    state = _breakers.get(name)
    if state is None:
        with _breaker_lock:
            state = _breakers.setdefault(name, _Breaker())
    return state


def is_open(name: str) -> bool:
    """是否处于开闸（含半开复位：冷却到点自动清闸放行一次试探）。"""
    state = _breakers.get(name)
    if state is None or not state.opened_at:
        return False
    cooldown = float(settings.AGENT_TOOL_CIRCUIT_COOLDOWN_SECONDS)
    if time.monotonic() - state.opened_at >= cooldown:
        with _breaker_lock:
            state.opened_at = 0.0
            state.failures = 0
        return False
    return True


def _record_success(name: str, latency_ms: int) -> None:
    """成功：清失败计数并追加耗时历史。"""
    state = _breaker_of(name)
    with _breaker_lock:
        state.failures = 0
        state.opened_at = 0.0
        state.last_error = ""
        state.history.append(latency_ms)
        del state.history[:-_HISTORY_MAX]


def _record_failure(name: str, error: str) -> None:
    """失败：累加计数，达阈值即开闸（记录开闸时刻用于冷却计时）。"""
    state = _breaker_of(name)
    with _breaker_lock:
        state.failures += 1
        state.last_error = error[:200]
        if state.failures >= int(settings.AGENT_TOOL_CIRCUIT_THRESHOLD) and not state.opened_at:
            state.opened_at = time.monotonic()


def breaker_snapshot(name: str) -> dict[str, Any]:
    """单工具熔断快照（注册中心端点透出，运维可观测）。"""
    state = _breakers.get(name)
    if state is None:
        return {"failures": 0, "open": False, "last_error": "", "recent_latency_ms": []}
    return {
        "failures": state.failures,
        "open": is_open(name),
        "last_error": state.last_error,
        "recent_latency_ms": list(state.history),
    }


def reset_breakers(name: str = "") -> None:
    """复位熔断（运维手动恢复 / 测试隔离）；name 空=全复位。"""
    with _breaker_lock:
        if name:
            _breakers.pop(name, None)
            return
        _breakers.clear()


# ---------------- 审计 ----------------


async def _emit_audit(
    ctx: ToolContext,
    *,
    name: str,
    args: dict[str, Any],
    result: dict[str, Any],
    latency_ms: int,
    trace_id: str,
    ok: bool,
) -> None:
    """投递审计事件（落库由宿主的 sink 负责；内核只保证好坏两条路径都发）。"""
    await audit.emit(
        audit.AuditRecord(
            trace_id=trace_id or ctx.trace_id,
            tenant=ctx.tenant,
            username=ctx.username,
            name=name,
            args=args,
            result=result,
            latency_ms=latency_ms,
            ok=ok,
            db=ctx.db,
            on_behalf_of=ctx.username,
        )
    )


async def _audit_failure(
    ctx: ToolContext, *, name: str, args: dict[str, Any], message: str, trace_id: str
) -> None:
    """失败同样留痕（被拒/超时/熔断都要能在审计里查到）。"""
    await _emit_audit(
        ctx,
        name=name,
        args=args,
        result={"status": "failed", "message": message},
        latency_ms=0,
        trace_id=trace_id,
        ok=False,
    )


# ---------------- 调用主链 ----------------


def _outcome(
    spec: ToolSpec,
    *,
    result: dict[str, Any],
    args: dict[str, Any],
    attempts: int,
    latency_ms: int,
    trace_id: str,
) -> dict[str, Any]:
    """成功出参（端点与展示层共用同一形状）。"""
    approval_id = str(result.get("approval_id") or "")
    return {
        "tool": spec.name,
        "status": "ok",
        "scope": spec.scope,
        "idempotent": spec.idempotent,
        "requires_approval": spec.requires_approval,
        "approval_required": bool(spec.requires_approval),
        "approval_id": approval_id,
        "args": args,
        "result": result,
        "attempts": attempts,
        "latency_ms": latency_ms,
        "timeout_seconds": registry.timeout_of(spec),
        "trace_id": trace_id,
        # 数据出处：本地工具为空；远程工具由 linkage 层实测填写
        "provider_id": spec.remote.provider_id if spec.remote else "",
    }


async def _invoke_once(
    ctx: ToolContext, spec: ToolSpec, args: dict[str, Any], *, trace_id: str
) -> tuple[dict[str, Any], Provenance | None]:
    """单次调用：本地 handler 或远程提供方（超时由 Settings 控制）。"""
    timeout = registry.timeout_of(spec)
    if spec.remote is not None:
        provider = linkage.provider_of(spec.remote.provider_id)
        return await asyncio.wait_for(
            provider.invoke(
                spec.remote, args, trace_id=trace_id, on_behalf_of=ctx.username
            ),
            timeout=timeout,
        )
    handler = spec.handler
    if handler is None:  # registry.register 已拦，此处防御性兜底
        raise BusinessError(
            ErrorCode.TOOL_CALL_FAILED, f"工具 {spec.name} 没有可执行的实现", 500
        )
    return await asyncio.wait_for(handler(ctx, args), timeout=timeout), None


async def _invoke_with_retry(
    ctx: ToolContext, spec: ToolSpec, args: dict[str, Any], started: float, *, trace_id: str
) -> tuple[dict[str, Any] | None, Provenance | None, int, ErrorCode | None]:
    """重试主循环：返回 (结果, 溯源, 尝试次数, 失败码)。

    成功时失败码为 None；尝试耗尽返回 None + 失败码（超时给 4002、上游故障给 5001、
    其余依赖失败给 4008），调用方据此抛错并落审计。
    """
    max_attempts = registry.retries_of(spec) + 1
    attempts = 0
    code: ErrorCode | None = None
    while attempts < max_attempts:
        attempts += 1
        try:
            result, provenance = await _invoke_once(ctx, spec, args, trace_id=trace_id)
            _record_success(spec.name, int((time.perf_counter() - started) * 1000))
            return result, provenance, attempts, None
        except BusinessError:
            # 业务拒绝：不是依赖故障，原码上抛（重试与熔断都不该介入）
            raise
        except TimeoutError:
            reason = f"工具 {spec.name} 超时（>{registry.timeout_of(spec)}s）"
            code = ErrorCode.TASK_TIMEOUT
        except UpstreamError as exc:
            reason = f"工具 {spec.name} 的上游故障：{exc.msg[:120]}"
            code = ErrorCode.UPSTREAM_FAILED
        except Exception as exc:  # 依赖异常统一收敛为可重试失败（业务拒绝已在上分支上抛）
            reason = f"工具 {spec.name} 调用失败：{str(exc)[:120]}"
            code = ErrorCode.TOOL_CALL_FAILED
        _record_failure(spec.name, reason)
        if attempts >= max_attempts:
            return None, None, attempts, code
        await asyncio.sleep(float(settings.AGENT_TOOL_RETRY_BACKOFF_SECONDS) * attempts)
    return None, None, attempts, code


async def call(
    ctx: ToolContext, *, name: str, args: dict[str, Any] | None = None, trace_id: str = ""
) -> dict[str, Any]:
    """工具调用唯一入口：策略 → 校验 → 熔断 → 执行 → 审计。

    失败一律抛 BusinessError（由宿主的异常处理器收口成 fail() 信封），
    调用方不再写 try/except。
    """
    spec = registry.get(name)
    payload = dict(args or {})
    trace = trace_id or ctx.trace_id
    # 策略先行：Scope 不命中直接 403，绝不「没权限也执行」
    policy.ensure_allowed(roles=ctx.roles, spec=spec, args=payload)
    errors = validate_args(spec.params, payload)
    if errors:
        message = "；".join(errors)
        await _audit_failure(ctx, name=spec.name, args=payload, message=message, trace_id=trace)
        raise BusinessError(ErrorCode.PARAM_INVALID, message)
    if is_open(spec.name):
        message = f"工具 {spec.name} 连续失败已熔断，请稍后重试或转人工跟进"
        await _audit_failure(ctx, name=spec.name, args=payload, message=message, trace_id=trace)
        raise BusinessError(ErrorCode.TOOL_CIRCUIT_OPEN, message, 503)

    started = time.perf_counter()
    result, provenance, attempts, code = await _invoke_with_retry(
        ctx, spec, payload, started, trace_id=trace
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    if result is None:
        reason = _breakers.get(spec.name, _Breaker()).last_error or f"工具 {spec.name} 调用失败"
        await _audit_failure(ctx, name=spec.name, args=payload, message=reason, trace_id=trace)
        record("agent.tool", {"tool": spec.name, "ok": False, "latency_ms": latency_ms})
        raise BusinessError(code or ErrorCode.TOOL_CALL_FAILED, f"{reason}；已尝试 {attempts} 次")

    data = _outcome(
        spec, result=result, args=payload, attempts=attempts, latency_ms=latency_ms, trace_id=trace
    )
    if provenance is not None:
        # 数据出处与结果同级：前端与审计都能直接看到「这份数据从哪来、什么时候取的」
        data["provenance"] = provenance
    await _emit_audit(
        ctx,
        name=spec.name,
        args=payload,
        result=data,
        latency_ms=latency_ms,
        trace_id=trace,
        ok=True,
    )
    record(
        "agent.tool",
        {
            "tool": spec.name,
            "ok": True,
            "attempts": attempts,
            "latency_ms": latency_ms,
            "approval": data["approval_required"],
            "trace_id": trace,
        },
    )
    return data