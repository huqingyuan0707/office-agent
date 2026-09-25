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
⑩ office.data.query / analyze / export：数据自助分析（读，演示台账 + CSV 叠加 / 统计 + 趋势 + 异常 / markdown-csv 文本与 xlsx-base64 导出不写盘，渲染收口 data_export）；
⑪ office.meeting.agenda / book / risks：会议协作（读议程模板直出 / 写预约恒送审 / 读风险关键词预警，无命中不编造）；
⑫ office.approval.draft / check / invoice.extract / opinion / submit：审批智能辅助（读草稿必填校验 / 读合规自查与高危二次确认 / 读发票要素提取 / 读审批说明与意见确定性成稿 / 写一键提交恒送审+幂等键批准后落台账）；
⑬ office.pptx.generate：PPT 生成（写，大纲直出 .pptx，恒送审+幂等键，python-pptx 缺失不注册）；
⑭ office.compliance.scan：合规风险检测（读，隐私/违规用语/泄密凭据三类规则，只摘录不推断）；
⑮ office.budget.query：预算查询（读，演示台账 + CSV 叠加，剩余额度确定性计算带溯源）；
⑯ office.memo.compose：通用文案起草（读，通知/邮件/方案/总结/汇报五类模板直出，缺板块留白）；
⑰ office.text.summarize / normalize：文本处理（读，抽取式摘要单篇+多篇整合/格式统一只动空白，润色改写后置）；
⑱ office.file.read / docs.rename / file.ask：文件解析、批量重命名与文件内容问答（读 docx 表格文本图片清单/xlsx 行列与前 N 行/纯文本直读/PDF pypdf 逐页文本；ask 按段落检索摘录原文作答，向量优先失败回退字符检索；写重命名恒送审禁覆盖）；
⑲ office.todo.list / update / delete、office.schedule.create / freebusy、office.worklog.generate：个人事务管理（PRD §2.2——本地事务存储，读免审；写恒送审审批通过才落盘；到期/会前/节点预警由 server 通知扫描链共用同一存储生成；实现拆两文件：affairs.py 待办域+存储原语，affairs_schedule.py 日程域）。
⑳ office.meeting.materials / digest / followup：会议全流程补充三件（PRD §2.5 缺口——会前资料包逐文件真实抽取汇编、会中速记按关键词归类决议/行动/风险要点原句摘录、会后行动项×待办台账五态对账跟进；实时语音转录需音频基建如实后置；meeting_flow.py，全读免审）。
㉑ office.mail.classify / reply_draft / action_items / precheck：邮件智能处理（PRD §2.7——入参驱动全读免审，不接真实邮箱杜绝假数据源；归类四类关键词口径 / 回复草稿缺项留占位不代编 / 行动项三字段正则提不出置 null / 预审复用 compliance 规则+语气词表命中即二次确认；群消息摘要与自动发信如实后置，见 mail.py docstring）。
㉒ office.data.query.save / list / run / delete + office.data.chart_insight：常用查询保存与一句话复用、图表自动解读（PRD §2.4 新增——save/delete 写恒送审落盘 data_queries/、list/run 读免审按租户隔离实时重查；insight 读免审，统计+最高/最低/2σ 异常标注+中文简报+同数据 SVG 条形图；见 data_saved.py / data_insight.py）。
㉓ office.im.digest：群消息摘要（PRD §2.7——入参驱动读免审：总数/分人计数+@本人摘录+任务候选+问句/决议/风险摘录+简报；动作词与日期正则复用 mail；定时调度待 §2.11）。
㉔ office.hr.attendance / checklist + office.resource.query / book：人事行政（PRD §2.8——考勤加班按人汇总确定性计算，草稿走 approval.draft；入离职清单模板直出缺项留白；资源台账内置+CSV 叠加，query 读免审给已订区间，book 写恒送审冲突 1001；见 hr.py / resources.py）。
㉕ office.project.query：项目台账（PRD §2.9——里程碑/风险/责任人/进度，内置 + projects.csv 叠加，按名/责任人/状态/有无风险过滤，附中文简报；任务拆解见 task_planner）。
㉖ office.finance.reimburse / expense：财务简易辅助（PRD §2.10——个人报销进度与在途金额、部门费用总额/类目/笔数 + 预算联带；预算剩余额度见 budget）。
㉗ office.desk.ticket / tickets：行政后勤通用工具（PRD §2.11——IT 报修/资产申领/工单三类，ticket 写恒送审落本地台账，tickets 读免审；外部系统同步由 linkage 承担）。
㉘ kb.ask 权限适配 + office.kb.search_unified + office.image.ask：企业知识库增强检索
（PRD §2.6——kb.ask 条目级 visibility（public/角色名，KB_DIR 首行 visibility 指令，
`*`/admin 可见全部，被滤只计 permission_filtered）；search_unified 一句话联查知识/
文档/本人待办日程/审批单据/数据台账五源并合并排序，远端聊天/OA 经联动接入后按
origin 追加；image.ask 图片 OCR 取文本后段落检索问答，引擎缺失如实降级；见 kb.py /
kb_unified.py / image_ask.py，全读免审）。
㉙ office.docx.render：Word 直出（PRD §2.1——标题 + markdown 子集渲染 .docx 二进制
base64 直出**不写盘**（与 data.export excel 同口径），供对话页「下载 Word」；
只做格式转换不改写文字；python-docx 缺失不注册；见 docx_render.py，读免审）。

链路：server lifespan → 本包 register_all() → 各模块 specs() → registry 注册 ToolSpec；
executor 按 spec 执行。
对齐：AGENTS.md §3（分层红线：工具实现纯函数）；
      智能办公Agent 产品需求文档.md §5.1（V1.0 必上线清单）、§5.2（V1.1 数据分析/会议协作/审批辅助）、
      §5.3（V1.2 PPT 生成/合规风险检测/预算查询）、§2.6（知识库增强检索）。
"""

from office_agent_core.registry import register as _core_register

from . import (
    affairs,
    affairs_schedule,
    approval,
    approval_opinion,
    approval_submit,
    budget,
    compliance,
    compose,
    data_analysis,
    data_insight,
    data_saved,
    desk,
    doc_compare,
    docx_render,
    file_ask,
    file_read,
    finance,
    hr,
    im_digest,
    image_ask,
    kb,
    kb_unified,
    mail,
    meeting,
    meeting_flow,
    ocr,
    pptx_gen,
    projects,
    resources,
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
    "approval_opinion",
    "approval_submit",
    "budget",
    "compliance",
    "compose",
    "data_analysis",
    "data_insight",
    "data_saved",
    "desk",
    "doc_compare",
    "docx_render",
    "file_ask",
    "file_read",
    "finance",
    "hr",
    "im_digest",
    "image_ask",
    "kb",
    "kb_unified",
    "mail",
    "meeting",
    "meeting_flow",
    "ocr",
    "pptx_gen",
    "projects",
    "register_all",
    "resources",
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
    docx_render,
    task_planner,
    templates,
    data_analysis,
    data_insight,
    data_saved,
    projects,
    finance,
    desk,
    meeting,
    meeting_flow,
    mail,
    approval,
    approval_submit,
    approval_opinion,
    pptx_gen,
    compliance,
    budget,
    compose,
    summarize,
    file_read,
    file_ask,
    image_ask,
    kb_unified,
    terms,
    im_digest,
    hr,
    resources,
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
