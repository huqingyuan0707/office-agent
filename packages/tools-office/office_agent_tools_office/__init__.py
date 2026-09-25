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
⑪ office.meeting.agenda / book / risks：会议协作（读议程模板直出 / 写预约恒送审 / 读风险关键词预警，无命中不编造）；
⑫ office.approval.draft / check / invoice.extract：审批智能辅助（读草稿必填校验 / 读合规自查与高危二次确认 / 读发票要素提取）；
⑬ office.pptx.generate：PPT 生成（写，大纲直出 .pptx，恒送审+幂等键，python-pptx 缺失不注册）；
⑭ office.compliance.scan：合规风险检测（读，隐私/违规用语/泄密凭据三类规则，只摘录不推断）；
⑮ office.budget.query：预算查询（读，演示台账 + CSV 叠加，剩余额度确定性计算带溯源）；
⑯ office.memo.compose：通用文案起草（读，通知/邮件/方案/总结/汇报五类模板直出，缺板块留白）；
⑰ office.text.summarize / normalize：文本处理（读，抽取式摘要单篇+多篇整合/格式统一只动空白，润色改写后置）；
⑱ office.file.read / docs.rename / file.ask：文件解析、批量重命名与文件内容问答（读 docx 表格文本图片清单/xlsx 行列与前 N 行/纯文本直读/PDF pypdf 逐页文本；ask 按段落检索摘录原文作答，向量优先失败回退字符检索；写重命名恒送审禁覆盖）；
⑲ office.todo.list / update / delete、office.schedule.create / freebusy、office.worklog.generate：个人事务管理（PRD §2.2——本地事务存储，读免审；写恒送审审批通过才落盘；到期/会前/节点预警由 server 通知扫描链共用同一存储生成；实现拆两文件：affairs.py 待办域+存储原语，affairs_schedule.py 日程域）。

链路：server lifespan → 本包 register_all() → 各模块 specs() → registry 注册 ToolSpec；
executor 按 spec 执行。
对齐：AGENTS.md §3（分层红线：工具实现纯函数）；
      智能办公Agent 产品需求文档.md §5.1（V1.0 必上线清单）、§5.2（V1.1 数据分析/会议协作/审批辅助）、
      §5.3（V1.2 PPT 生成/合规风险检测/预算查询）。
"""

from office_agent_core.registry import register as _core_register

from . import (
    affairs,
    affairs_schedule,
    approval,
    budget,
    compliance,
    compose,
    data_analysis,
    doc_compare,
    file_ask,
    file_read,
    kb,
    meeting,
    ocr,
    pptx_gen,
    summarize,
    task_planner,
    templates,
    terms,
    tools,
)

__all__ = [
    "_core_register",
    "affairs",
    "affairs_schedule",
    "approval",
    "budget",
    "compliance",
    "compose",
    "data_analysis",
    "doc_compare",
    "file_ask",
    "file_read",
    "kb",
    "meeting",
    "ocr",
    "pptx_gen",
    "register_all",
    "specs",
    "summarize",
    "task_planner",
    "templates",
    "terms",
    "tools",
]

#: 工具模块清单（新增模块在此追加一行即可被聚合注册）
_MODULES = (
    tools,
    kb,
    ocr,
    doc_compare,
    task_planner,
    templates,
    data_analysis,
    meeting,
    approval,
    pptx_gen,
    compliance,
    budget,
    compose,
    summarize,
    file_read,
    file_ask,
    terms,
    affairs,
    affairs_schedule,
)


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
