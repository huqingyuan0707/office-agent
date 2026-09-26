"""Run 主循环入口（编排层唯一执行位：提议 → executor.call → 观察 → 检查点 → 重复）

链路：api 受理 → start_run 建 Task 行（type="agent.run"）→ execute_run 薄装配 →
      graph.run_graph（LangGraph StateGraph：plan → act 自环）驱动 loop_state.LoopState
      逐步 executor.call（Scope 硬拦 / Schema 校验 /
      熔断 / 审批闸门 / 审计全在内核，编排层绕不过）→ 每步写 RunStep →
      Task.checkpoint 随步推进 → 收敛。

R2 增量（对齐 .trae/documents/智能体编排层实现方案.md §R2）：
- 送审分流：步骤命中 requires_approval → 不落 executor，经 approvals.suspend_for_approval
  预检（Scope+Schema，与 invoke 端点同一分流口径）并落审批单，run 挂起
  WAITING_APPROVAL（checkpoint 存 pending_approval）；批准后由 approvals.resolve_pending
  内联裁决 lazy resume——该步按幂等口径在主循环重放（approved_steps 标记跳过二次
  送审），重放仍走 executor.call，时间线留完整出参与审计。
- 回答数值校验：收敛 DONE 后经 validation.finalize_answer 合成 LLM 终答并校验数字
  溯源，标记随 checkpoint / run 概要透出（降级不 500，绝不阻断已完成步骤）。

关键决策（都有出处）：
- 受理与执行分离：单步失败（白名单越权 / 模板缺值 / executor 业务拒绝）把 run 收敛到
  FAILED 终态，但本循环不向外抛错——POST /runs 仍返回 code 0 的 run 概要，错误细节
  落在 RunStep 时间线与检查点里，由 GET /runs/{id} 轮询展示（前端轮询友好）。
- max_steps 硬顶：计划步数超出即 FAILED（1005），已执行步骤留痕，checkpoint 记录
  断点，续跑可从断点继续（防 LLM 规划器循环烧 token 的地基）。
- 计划持久化：首次规划结果写进 checkpoint，续跑回放原计划（agent.yaml 中途变更
  也不会让步号与历史结果错位）；白名单仍按当前 spec 在执行前硬拦。
- 状态机：全程走内核 TRANSITIONS（ensure_transition），编排层不私改状态；FAILED 可经
  resume 重新进入 PLANNING（内核口径），DONE 是唯一成功终态；审批解除的重入从
  WAITING_APPROVAL 直进 ACTING（内核口径），计划回放照常、不重复送审。
- 白名单：planner 提议的工具必须命中 AgentSpec 白名单（声明层已校验 rules ⊆ 白名单，
  这里是运行时最后防线，防 LLM 规划器越权提议）；Scope 与角色可见性由内核 policy
  在 executor.call 里硬拦。
- 结构拆分：循环骨架在 graph.py（LangGraph StateGraph 装配），执行态与全部私有
  助手在 loop_state.py（LoopState 类），本文件只留受理入口与薄装配——execute_run
  签名是 approvals.resolve_pending 的注入契约，不得变更（AGENTS §6 C901 豁免的
  拆分偿还，2026-09-24；ADR-0005 阶段一由 RunLoop 循环改图为条件边自环）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_core.contracts import AgentState
from office_agent_runtime.checkpoint import dumps, summarize_run
from office_agent_runtime.graph import run_graph
from office_agent_runtime.loop_state import LoopState
from office_agent_runtime.router import GREETING_GUIDE
from office_agent_runtime.spec import AgentSpec
from office_agent_server.models import Task
from office_agent_server.rbac import CurrentUser

#: Run 复用 Task 表的类型标记（Task 无新增列，agent/goal/断点全在 checkpoint 里）
RUN_TASK_TYPE = "agent.run"


async def start_run(
    db: AsyncSession,
    *,
    spec: AgentSpec,
    goal: str,
    user: CurrentUser,
    trace_id: str,
) -> dict[str, Any]:
    """发起一次运行：建 Task 行（type="agent.run"）并同步执行主循环。

    Task 表没有的列（agent / goal / 断点）全存 checkpoint 与 input，不硬塞；
    发起人就在 Task.username 既有列上。
    """
    task = Task(
        tenant=user.tenant,
        username=user.username,
        type=RUN_TASK_TYPE,
        status=AgentState.IDLE.value,
        input=dumps({"agent": spec.name, "goal": goal}),
        checkpoint=dumps(
            {"agent": spec.name, "goal": goal, "next_step": 0, "steps": [], "error": ""}
        ),
    )
    db.add(task)
    await db.flush()  # 先落行拿 id（后续 RunStep.run_id / 异步轮询都要用）
    # 立刻提交释放写锁：execute_run 里的 LLM 规划是 30~60s 级网络 I/O，
    # 若让事务持有到 run 结束，SQLite 单写者会把并发写全撞成 "database is locked"。
    # 轮询方也因此能尽早看到 IDLE 行（expire_on_commit=False，task 属性不失效）。
    await db.commit()
    return await execute_run(db, task=task, spec=spec, goal=goal, user=user, trace_id=trace_id)


async def instant_greeting_run(
    db: AsyncSession,
    *,
    goal: str,
    user: CurrentUser,
) -> dict[str, Any]:
    """纯寒暄即时回复：零工具调用，直接建 DONE 的 Task 行（answer=使用引导终答）。

    链路：POST /runs 省略 agent 且目标纯寒暄 → 本函数 → code 0 即时返回 →
    对话页走正常 run 路径渲染终答、轮询一次即停（与 1001 错误分支无关，气泡必有内容）。
    只在自动路由前拦截：显式指定 agent 的直达调用不受影响（由所选智能体如实规划）。
    """
    answer = f"你好！我是智能办公助手。{GREETING_GUIDE}"
    payload = {
        "agent": "",
        "goal": goal,
        "status": AgentState.DONE.value,
        "steps_done": 0,
        "error": "",
        "answer": answer,
        "validation": None,
    }
    checkpoint = {
        "agent": "",
        "goal": goal,
        "next_step": 0,
        "steps": [],
        "error": "",
        "answer": answer,
    }
    task = Task(
        tenant=user.tenant,
        username=user.username,
        type=RUN_TASK_TYPE,
        status=AgentState.DONE.value,
        progress=1.0,
        input=dumps({"agent": "", "goal": goal}),
        output=dumps(payload),
        checkpoint=dumps(checkpoint),
    )
    db.add(task)
    await db.flush()  # 落行拿 id（概要与后续 GET /runs/{id} 都要用）
    summary = summarize_run(task, checkpoint)
    summary["answer"] = answer
    return summary


async def execute_run(
    db: AsyncSession,
    *,
    task: Task,
    spec: AgentSpec,
    goal: str,
    user: CurrentUser,
    trace_id: str,
) -> dict[str, Any]:
    """执行一次 run 主循环，返回 run 概要（不抛错，失败也走概要——见模块 docstring）。

    薄装配：循环骨架在 graph.run_graph（LangGraph StateGraph，plan → act 自环），
    执行态与收尾在 LoopState（prepare_plan / run_step / finish）；本签名是
    approvals.resolve_pending 的注入契约，不得变更。
    """
    loop = LoopState(db, task, spec, goal, user, trace_id)
    await run_graph(loop)
    return await loop.finish()
