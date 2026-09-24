"""Task.checkpoint 读写与内核状态推进助手（runner 主循环与审批闭环共用）

链路：runner 主循环 / approvals 送审挂起与内联裁决 → 本模块统一 checkpoint JSON
      读写、RunStep 步骤条目维护与内核状态机推进——两个模块共享同一套口径，
      避免各写一份 JSON/状态推进逻辑产生第二真相源。

口径：
- JSON 一律文本落库（SQLite 无 JSONB）；非可序列化对象降级 str，绝不因落库丢留痕；
- 损坏的 checkpoint 给安全缺省不抛错（读取路径绝不因断点脏数据拖垮）；
- 状态推进只走内核 TRANSITIONS（非法流转 4009），编排层不私改状态。
对齐：.trae/documents/智能体编排层实现方案.md §3（运行实体与断点）。
"""

from __future__ import annotations

import json
from typing import Any

from office_agent_core.contracts import AgentState, ensure_transition, state_label
from office_agent_server.models import Task


def parse_checkpoint(raw: str) -> dict[str, Any]:
    """Task.checkpoint 列的 JSON 文本 → 字典（脏数据给空对象，不因断点坏拖垮读取）。"""
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def dumps(value: Any) -> str:
    """JSON 文本落库（非可序列化对象降级为 str，绝不因落库失败丢留痕）。"""
    return json.dumps(value, ensure_ascii=False, default=str)


def step_entries(checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    """检查点里的步骤条目（损坏时给空列表，读取路径绝不抛错）。"""
    entries = checkpoint.get("steps")
    return (
        [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []
    )


def record_entry(checkpoint: dict[str, Any], entry: dict[str, Any]) -> None:
    """按步号替换或追加步骤条目（重试成功覆盖失败条目），保持步号有序。"""
    entries = checkpoint.setdefault("steps", [])
    entries[:] = [item for item in entries if item.get("index") != entry["index"]]
    entries.append(entry)
    entries.sort(key=lambda item: int(item.get("index") or 0))


def next_step_of(checkpoint: dict[str, Any]) -> int:
    """断点步号（脏数据给 0，与「从头重跑」同口径）。"""
    try:
        return int(checkpoint.get("next_step") or 0)
    except (TypeError, ValueError):
        return 0


def move_state(task: Task, dst: AgentState) -> None:
    """沿内核状态机推进并把新状态落到 Task 行（非法流转 4009，编排层不私改状态）。"""
    task.status = str(ensure_transition(task.status, dst))


def approved_steps(checkpoint: dict[str, Any]) -> list[int]:
    """已获批可重放的步号（R2：批准后续跑重放该步，不再重复送审）。"""
    raw = checkpoint.get("approved_steps")
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for item in raw:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def summarize_run(task: Task, checkpoint: dict[str, Any]) -> dict[str, Any]:
    """run 概要（受理 / 续跑 / 审批裁决统一形状；agent 与 goal 从 checkpoint 取）。"""
    steps_done = sum(1 for entry in step_entries(checkpoint) if entry.get("status") == "ok")
    return {
        "run_id": task.id,
        "agent": str(checkpoint.get("agent") or ""),
        "goal": str(checkpoint.get("goal") or ""),
        "status": task.status,
        "status_label": state_label(task.status),
        "steps_done": steps_done,
        "error": str(checkpoint.get("error") or ""),
    }
