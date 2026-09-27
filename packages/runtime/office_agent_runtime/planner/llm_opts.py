"""LLM 出站调优参数（Ollama 本地 2-3s 快路径配合，对齐 ADR-0006）

链路：planner/llm.py 的 plan/answer 与 planner/react.py 的 next_step 共用
  build_chat_payload() 组装 chat.completions 请求体——temperature=0 保规划确定性、
  num_predict 封顶防长尾、num_ctx=4096 够装裁剪后工具集、keep_alive 常驻显存免冷启、
  think=False 关 qwen3 思考链（实测热机 20s 里思考占大头）。
红线：OpenAI 兼容端忽略未知字段；本模块只组装字典，不发网络、不读凭据。
"""

from __future__ import annotations

from typing import Any

#: 出站 options（deterministic + 封顶 + 小上下文）
LLM_OPTIONS: dict[str, Any] = {"temperature": 0, "num_predict": 1024, "num_ctx": 4096}
#: 模型常驻显存（免按需加载数秒冷启动）
LLM_KEEP_ALIVE = "30m"


def build_chat_payload(
    model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """组装 chat.completions 请求体（含调优字段；终答合成传 tools=None）。"""
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "keep_alive": LLM_KEEP_ALIVE,
        "options": dict(LLM_OPTIONS),
    }
    if tools is not None:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return payload
