"""评测 harness（数据集 → in-process 执行 → trajectory → 打分聚合）

链路：run_eval → 逐 case：临时 SQLite 库建表 → loader.agents_dir 指向自带
      plugins 目录 → runner.start_run（与 POST /runs、审批续跑同一执行入口）
      → 从 Task.checkpoint 提取 Trajectory（与断点回放同源，不查 run_steps 表）
      → scorer 链 → CaseReport → EvalReport。

关键决策：
- in-process 复用 execute_run：不起 HTTP、不打登录——评测的就是产品同一执行链，
  传输层差异（TestClient vs 直接调用）不进入评测面；
- 独立临时库：不碰全局引擎（settings.DATABASE_URL 指向的 dev/生产库），评测
  落库自持、跑完即弃（仿 tests/conftest.py 的临时库纪律）；
- Trajectory 只取成功步骤（status=ok）：失败步骤的事实由 status/error 承载，
  工具序列不与失败行混淆（对齐 loop_state._replay_results 的口径）；
- 单条用例失败（智能体不存在等）是评测事实：进报告不中断整批。
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from office_agent_core.contracts import AgentState
from office_agent_core.errors import BusinessError
from office_agent_runtime import loader
from office_agent_runtime.checkpoint import parse_checkpoint, step_entries
from office_agent_runtime.eval.dataset import EvalCase, load_dataset
from office_agent_runtime.eval.scorers import Scorer, ScoreResult, default_scorers
from office_agent_runtime.runner import start_run
from office_agent_server.db import Base
from office_agent_server.models import Task
from office_agent_server.rbac import CurrentUser

logger = logging.getLogger(__name__)

#: 评测机器人身份（roles=["*"] 全 Scope：评的是规划质量，不是权限收窄）
_EVAL_USER = CurrentUser(username="eval-bot", tenant="default", roles=["*"])


@dataclass(frozen=True)
class Trajectory:
    """一次 run 的评测面轨迹（从 Task.checkpoint 提取，scorer 的唯一输入）。

    tools 与 sorted(results) 的键按位对应（同一批 ok 步骤条目同序构建）；
    results 的值是工具出参的 result 面（对齐 validation.observations 形状）。
    """

    agent: str
    goal: str
    status: str
    tools: tuple[str, ...]
    results: dict[int, Any] = field(default_factory=dict)
    answer: str = ""
    validation: dict[str, Any] | None = None
    error: str = ""


@dataclass(frozen=True)
class CaseReport:
    """单条用例的报告（scores 含 skipped 条目；passed = 状态成功且全部非 skip scorer 通过）。"""

    case_id: str
    agent: str
    goal: str
    status: str
    scores: tuple[ScoreResult, ...]
    passed: bool
    error: str = ""


@dataclass(frozen=True)
class EvalReport:
    """整批评测的报告（pass_rate = passed / total；空集为 0.0）。"""

    total: int
    passed: int
    cases: tuple[CaseReport, ...]

    def pass_rate(self) -> float:
        """汇总通过率（0.0~1.0；total=0 时 0.0，不除零）。"""
        return self.passed / self.total if self.total else 0.0


async def run_eval(
    dataset_path: Path,
    *,
    plugins_dir: Path | None = None,
    db_path: Path | None = None,
    scorers: list[Scorer] | None = None,
    tool_registrar: Callable[[], None] | None = None,
) -> EvalReport:
    """跑完整批评测并返回报告（单条用例失败进报告不中断；数据集/智能体不存在才抛错）。

    tool_registrar：可选的工具注册钩子（如 office_agent_tools_office.register_all）——
    runtime 包不依赖 tools-office，示例数据集所需的工具由调用方显式注入，
    未注入则按 registry 现状执行（清单里没有的工具在运行期内如实失败）。
    """
    cases = load_dataset(dataset_path)
    active_scorers = scorers if scorers is not None else default_scorers()
    plugins = Path(plugins_dir) if plugins_dir else loader.agents_dir()
    tmp_dir: Path | None = None
    if db_path is None:
        tmp_dir = Path(tempfile.mkdtemp(prefix="office-agent-eval-"))
        db_path = tmp_dir / "eval.db"
    if tool_registrar is not None:
        tool_registrar()
    original_agents_dir = loader.agents_dir
    loader.agents_dir = lambda: plugins  # type: ignore[assignment]  # 临时指向评测 plugins（仿 conftest monkeypatch）
    try:
        engine, session_factory = await _open_db(db_path)
        try:
            reports = [await _run_case(case, session_factory, active_scorers) for case in cases]
        finally:
            await engine.dispose()
    finally:
        loader.agents_dir = original_agents_dir  # type: ignore[assignment]
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    passed = sum(1 for report in reports if report.passed)
    return EvalReport(total=len(reports), passed=passed, cases=tuple(reports))


async def _open_db(db_path: Path) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """开临时库并建表（Base metadata 全表 create_all，口径同 db.init_models）。"""
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    engine = create_async_engine(url, future=True, connect_args={"timeout": 10.0})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _run_case(
    case: EvalCase,
    session_factory: async_sessionmaker[AsyncSession],
    scorers: list[Scorer],
) -> CaseReport:
    """跑单条用例并打分（BusinessError 进报告：智能体不存在等是评测事实）。"""
    async with session_factory() as db:
        try:
            spec = loader.find_agent_spec(case.agent)
            summary = await start_run(
                db,
                spec=spec,
                goal=case.goal,
                user=_EVAL_USER,
                trace_id=f"eval-{case.id}",
            )
        except BusinessError as exc:
            return CaseReport(
                case_id=case.id,
                agent=case.agent,
                goal=case.goal,
                status="error",
                scores=(),
                passed=False,
                error=exc.msg,
            )
        task = await db.get(Task, summary["run_id"])
        trajectory = _trajectory_of(case, task)
        scores = tuple(scorer.score(case, trajectory) for scorer in scorers)
        applicable = [item["passed"] for item in scores if item["passed"] is not None]
        passed = trajectory.status == AgentState.DONE.value and all(applicable)
        return CaseReport(
            case_id=case.id,
            agent=case.agent,
            goal=case.goal,
            status=trajectory.status,
            scores=scores,
            passed=passed,
            error="" if passed else trajectory.error,
        )


def _trajectory_of(case: EvalCase, task: Task | None) -> Trajectory:
    """从 Task.checkpoint 提取评测面轨迹（与断点回放同源；task 缺失给空轨迹）。"""
    if task is None:
        return Trajectory(agent=case.agent, goal=case.goal, status="error", tools=())
    checkpoint = parse_checkpoint(task.checkpoint)
    entries = sorted(
        (entry for entry in step_entries(checkpoint) if entry.get("status") == "ok"),
        key=lambda entry: int(entry.get("index") or 0),
    )
    tools: list[str] = []
    results: dict[int, Any] = {}
    for entry in entries:
        tools.append(str(entry.get("tool") or ""))
        outcome = entry.get("outcome")
        if isinstance(outcome, dict):
            try:
                results[int(entry["index"])] = outcome.get("result")
            except (KeyError, TypeError, ValueError):
                continue
    validation = checkpoint.get("validation")
    return Trajectory(
        agent=case.agent,
        goal=case.goal,
        status=task.status,
        tools=tuple(tools),
        results=results,
        answer=str(checkpoint.get("answer") or ""),
        validation=validation if isinstance(validation, dict) else None,
        error=str(checkpoint.get("error") or ""),
    )
