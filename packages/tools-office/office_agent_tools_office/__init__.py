"""office-agent-tools-office：内置办公工具包独立 pip 包。

职责：注册办公域工具——
① office.report.generate：结构化中文日报/周报（读，不调大模型，每个数字带溯源标注）；
② office.minutes.generate：会议纪要（读，模板直出，缺板块留白）；
③ office.schedule.view：查询日程视图（读，带 source+fetched_at 溯源）；
④ office.todo.create：创建待办（写，scope=office:write + requires_approval=True + 幂等）；
⑤ kb.ask：知识库问答（读，内置条目 + KB_DIR 文件检索，空命中如实降级）；
⑥ ocr.image：图片OCR（读，Pillow 元数据 + Tesseract 可选引擎，缺失降级）。

链路：server lifespan → 本包 register_all() → 各模块 specs() → registry 注册 ToolSpec；
executor 按 spec 执行。
对齐：AGENTS.md §3（分层红线：工具实现纯函数）；
      智能办公Agent 产品需求文档.md §5.1（V1.0 必上线清单）。
"""

from office_agent_core.registry import register as _core_register

from . import kb, ocr, tools

__all__ = ["_core_register", "kb", "ocr", "register_all", "specs", "tools"]


def register_all() -> list[str]:
    """聚合注册全部内置办公工具（各模块自行决定依赖缺失时注册哪些）；返回工具名列表。"""
    names: list[str] = []
    for module in (tools, kb, ocr):
        names.extend(module.register_all())
    return names


def specs() -> tuple:
    """全部内置工具规格（供清单展示与测试断言）。"""
    all_specs: tuple = ()
    for module in (tools, kb, ocr):
        all_specs += module.specs()
    return all_specs
