"""office-agent-tools-office：内置办公工具包独立 pip 包。

职责：注册办公域工具——
① office.report.generate：结构化中文日报/周报（读，不调大模型，每个数字带溯源标注）；
② office.minutes.generate：会议纪要（读，模板直出，缺板块留白）；
③ office.schedule.view：查询日程视图（读，带 source+fetched_at 溯源）；
④ office.todo.create：创建待办（写，scope=office:write + requires_approval=True + 幂等）；
⑤ kb.ask：知识库问答（读，内置条目 + KB_DIR 文件检索，空命中如实降级）；
⑥ ocr.image：图片OCR（读，Pillow 元数据 + Tesseract 可选引擎，缺失降级）；
⑦ office.doc.compare：文档对比（读，两份 docx 段落级 diff + 变更摘要）；
⑧ office.task.decompose / office.task.commit：任务拆解（读）与批量建单（写，恒送审+幂等）；
⑨ office.template.save / office.template.apply：自定义模板（写送审落盘 / 读填充复用）；
⑩ office.data.query / analyze / export：数据自助分析（读，演示台账 + CSV 叠加 / 统计 + 趋势 + 异常 / 文本导出不写盘）；
⑪ office.meeting.agenda / book / risks：会议协作（读议程模板直出 / 写预约恒送审 / 读风险关键词预警，无命中不编造）。

链路：server lifespan → 本包 register_all() → 各模块 specs() → registry 注册 ToolSpec；
executor 按 spec 执行。
对齐：AGENTS.md §3（分层红线：工具实现纯函数）；
      智能办公Agent 产品需求文档.md §5.1（V1.0 必上线清单）、§5.2（V1.1 数据自助分析/会议协作）。
"""

from office_agent_core.registry import register as _core_register

from . import data_analysis, doc_compare, kb, meeting, ocr, task_planner, templates, tools

__all__ = [
    "_core_register",
    "data_analysis",
    "doc_compare",
    "kb",
    "meeting",
    "ocr",
    "register_all",
    "specs",
    "task_planner",
    "templates",
    "tools",
]

#: 工具模块清单（新增模块在此追加一行即可被聚合注册）
_MODULES = (tools, kb, ocr, doc_compare, task_planner, templates, data_analysis, meeting)


def register_all() -> list[str]:
    """聚合注册全部内置办公工具（各模块自行决定依赖缺失时注册哪些）；返回工具名列表。"""
    names: list[str] = []
    for module in _MODULES:
        names.extend(module.register_all())
    return names


def specs() -> tuple:
    """全部内置工具规格（供清单展示与测试断言）。"""
    all_specs: tuple = ()
    for module in _MODULES:
        all_specs += module.specs()
    return all_specs
