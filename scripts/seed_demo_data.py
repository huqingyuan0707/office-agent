"""演示数据种子脚本（幂等，可重复运行；把演示内容落到本仓 data/ 目录）。

职责：让系统各功能页/工具「开箱即有真实数据可查、可答」，一次性补齐四类落点——
- 知识库文档（settings.KB_DIR）：考勤/差旅/报销/请假/加班/保密/采购/会议/用车/信息安全
  等 20+ 高频主题，覆盖 md/txt/docx/xlsx/csv 五种格式，经 kb_admin.save_and_ingest 与
  「知识库后台」同一口径入库（同一来源清单、向量化如实标注），上传即刻可被 kb.ask 检索；
- 业务台账（DOCS_DIR/data/{dataset}.csv）：sales/project/work/attendance/budget 五类，
  多月份 × 多人 × 多项目密度，作为 office.data.query / office.budget.query 的 CSV 叠加层
  （追加到内置演示行之上）；
- 网盘文档（DOCS_DIR）：日报/周报/会议纪要/值班表/用车记录/培训课件/Q4 冲刺计划/合同样本，
  供 office.file.read / office.file.ask；
- 业务事务台账（DOCS_DIR/data/*.json）：待办/资源预订/行政工单/审批台账覆写为干净演示数据，
  顺带清掉历史冒烟测试留下的「写周报/冒烟评审」噪声行。

链路：python scripts/seed_demo_data.py → kb_admin.save_and_ingest（知识库）/
      直接写盘（台账 CSV、网盘文档与 JSON 事务台账）。
红线：纯本地、幂等（重复运行不产生重复台账行，知识库按 (origin) source text 去重；
      JSON 台账为整文件覆写，始终回到干净演示形态）；
      不触 ORM/FastAPI；演示账号由 office_agent_server.seed 在服务启动时另补，本脚本不管。
对齐：AGENTS.md §3（降级绝不 500/数据不出域）；
      智能办公Agent 产品需求文档.md §2.1（文档解析/问答）、§2.4（数据查询与分析）、
      §2.6（知识库检索）、§2.13（知识库后台）、§2.8（资源预订）、§2.11（通用工具）。
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import zipfile
from pathlib import Path

#: 确保无论从哪个目录调用，都能 import 仓库内的 office_agent 各包（脚本不依赖 pip 全局安装）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docx import Document
from openpyxl import Workbook

from office_agent_core.settings import settings
from office_agent_tools_office import kb_admin

#: 生成引擎可用性探测：PIL（图片/PDF/信息图）与 python-pptx（课件）缺失时降级跳过
#: 对应类型，不让整个种子脚本因可选依赖而中断（红线：降级绝不 500）
try:
    from PIL import Image, ImageDraw, ImageFont

    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    Image = ImageDraw = ImageFont = None
    _PIL_AVAILABLE = False

try:
    from pptx import Presentation as _PptxPresentation

    _PPTX_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PptxPresentation = None
    _PPTX_AVAILABLE = False

#: 中文字体候选路径（Windows/macOS/Linux 常见路径，按序尝试）
_CJK_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/simhei.ttf"),
    Path("C:/Windows/Fonts/msyh.ttf"),
    Path("/System/Library/Fonts/PingFang.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
)


def _wrap_text(text: str, limit: int) -> list[str]:
    """按字符宽度折行（中文按字符计，不按字节，避免绘制溢出页面）。"""
    if len(text) <= limit:
        return [text]
    return [text[index : index + limit] for index in range(0, len(text), limit)]


def _pick_cjk_font(size: int) -> ImageFont.ImageFont:
    """挑选可用的中文字体；全部缺失时退回 PIL 默认字体（西文可用）。"""
    for candidate in _CJK_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(str(candidate), size)
        except OSError:
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# 一、知识库文档（主题 × 格式；visibility：public 人人可见，hr 仅 hr/admin/* 可见）
# ---------------------------------------------------------------------------


#: 把多主题文档渲染进同一条 KB 上传请求的辅助函数（batch 一次入库，减少后台「单文件一条」
#: 的零散感；每篇正文仍作为独立 entry 分块检索）。
def _kb_md(title: str, paragraphs: list[str]) -> str:
    """渲染一条 markdown 知识库正文（标题 + 段落）。"""
    body = f"# {title}\n\n"
    body += "\n\n".join(paragraphs)
    return body


#: 直接放盘（不经后台）的 md：正文首行 `visibility: hr` 是 md/txt 的可见范围指令——
#: 走 kb.load_entries 回退口径而非清单（这也是「手工放盘 → 后台标未解析」三态的一例）
KB_MD_HR = (
    "visibility: hr\n"
    "# 薪酬构成与调薪细则\n\n"
    "月度薪酬由基本工资、绩效奖金、岗位津贴三部分构成，绩效奖金与季度考核结果挂钩。\n\n"
    "调薪统一安排在每年四月窗口，其余时间不单独调整；职级薪档仅本人与人力资源可见，"
    "任何形式的薪酬讨论与打听均属违规。\n\n"
    "转正、晋升、平调涉及薪档变更时，由人力资源在当月工资发放前完成核算并双人复核。"
)

KB_DOCS: list[tuple[str, str, str]] = [
    # (文件名, 可见范围, 正文)
    (
        "考勤管理制度.md",
        "public",
        _kb_md(
            "考勤管理制度",
            [
                "工作日标准时长为上午 9:00 至下午 18:00，午休 12:00-13:00；弹性到岗时段为 "
                "8:30-10:00，在时段内到岗即视为正常出勤。",
                "每月补卡不超过 3 次；连续迟到 3 次以上需向直属主管书面说明原因；无故缺勤按旷工处理，"
                "旷工累计超过 3 天将影响当月绩效。",
                "因公外出、出差、请假须提前在协同系统提交申请并完成审批。",
            ],
        ),
    ),
    (
        "差旅与招待管理办法.md",
        "public",
        _kb_md(
            "差旅与招待管理办法",
            [
                "出差须提前填写出差申请单，按权限审批后方可出行；出差结束 7 个自然日内提交行程与费用明细。",
                "交通标准：高铁二等座、经济舱为默认；乘坐一等座/头等舱须因公确有需要并经部门负责人事前批准。",
                "住宿标准：一线城市每晚不超过 500 元，其他城市不超过 350 元；客房类型以标准间为限，超标部分自理。",
                "市内交通实报实销但须附行程记录；招待客户按人均标准执行并于季度末接受费用抽查。",
                "费用发生后 30 天内提交报销并附原始发票；单笔超 1000 元需部门负责人审批，超 5000 元需分管副总审批。",
            ],
        ),
    ),
    (
        "费用报销标准.txt",
        "public",
        "费用报销标准\n"
        "差旅交通按本文档第一节标准报销；市内因公出行可报销公共交通与出租车费用，单日上限 200 元。\n\n"
        "办公用品采购金额低于 500 元可由行政统一采购，个人采购原则上不予报销；"
        "团建活动费用按每人每季度 200 元以内报销，需附活动签到记录。\n\n"
        "报销单须在费用发生后 30 天内提交并附原始发票；单笔超过 1000 元需部门负责人审批，"
        "超过 5000 元需分管副总审批；报销统一每周三打款。",
    ),
    (
        "请假与休假管理办法.md",
        "public",
        _kb_md(
            "请假与休假管理办法",
            [
                "年休假：员工入职满一年起享有 5 天，此后每满一年增加 1 天，最长 15 天；当年未休完可结转次年，最多保留 5 天。",
                "请假类别分为病假、事假、年假、婚假、产假、陪产假、丧假、调休；病假须提供医院证明，事假原则上不超过 5 个工作日。",
                "请假流程：员工在协同办公系统提交请假申请，1 天以内由直属主管批准，3 天以内由部门负责人批准，超过 3 天由分管副总审批。",
                "上下半年各安排一次调薪窗口，请假天数与考勤结果将纳入年度绩效评估的参考。",
            ],
        ),
    ),
    (
        "加班与调休管理办法.md",
        "public",
        _kb_md(
            "加班与调休管理办法",
            [
                "加班须事前在系统申请并经主管审批，事后补申请不予认定；工作日加班超过 2 小时起计，法定节假日加班按国家规定计酬。",
                "加班时长以小时为单位累计，1 小时内按 0.5 天记录，满 4 小时按 0.5 天，满 8 小时按 1 天计调休。",
                "调休当年有效，跨年作废；安排员工调休时优先于调薪，不得在未审批情况下自行离岗。",
            ],
        ),
    ),
    (
        "保密与信息安全制度（公开版）.md",
        "public",
        _kb_md(
            "保密与信息安全制度（公开版）",
            [
                "公司商业秘密、客户资料、未公开经营数据均属保密信息，未经授权不得复制、外发或带离办公场所。",
                "严禁通过私人邮箱、云盘、即时通讯工具传输涉密资料；对外提供数据一律经合规复核并留存审批记录。",
                "发现疑似泄密或异常访问应立即上报信息安全组，严禁私下传播或隐瞒。",
                "办公电脑须安装统一终端安全软件并保持系统更新；弱口令、共享账号均是红线行为。",
            ],
        ),
    ),
    (
        "供应商准入与采购制度.txt",
        "public",
        "供应商准入采用准入评审制：新供应商须通过资质审查与样品验证后方可进入合格供应名录，"
        "评审结论由采购部、需求部门、合规岗三方会签。\n\n"
        "单笔采购金额超过 20000 元须三家比价，超过 50000 元须招投标；紧急采购须事后补流程。\n\n"
        "采购合同签订前须经法务与财务双审；到货验收须由需求部门与采购岗共同完成并出具验收单。",
    ),
    (
        "会议与会议室管理办法.md",
        "public",
        _kb_md(
            "会议与会议室管理办法",
            [
                "会议室分为大会议室（容纳 20 人）与小会议室（容纳 6 人）两种，日常使用通过协同办公系统在线预订。",
                "预订人在会议开始前 30 分钟未到场，视为自动释放；临时取消预订应在系统内及时取消，以便资源复用。",
                "会议纪要由主持人所在角色负责整理，会后 1 个工作日内存档至网盘；决议事项需列明责任人、截止时间并追踪闭环。",
                "访客会议须由接待人提前在前台登记，会后访客由接待人负责带出办公区域。",
            ],
        ),
    ),
    (
        "公务用车管理规定.md",
        "public",
        _kb_md(
            "公务用车管理规定",
            [
                "公务车辆仅限因公出行使用，使用前须在系统预订，单次使用须注明起止时间与事由。",
                "驾驶员须持有效驾照并遵守交通法规；车辆油费、过路费、停车费凭票实报实销。",
                "车辆维护、年检、保险由行政部统一安排；非工作时间车辆须停回公司指定车库，未经审批不得公车私用。",
            ],
        ),
    ),
    (
        "移动终端与BYOD管理办法.txt",
        "public",
        "移动终端与BYOD管理办法\n"
        "员工自带设备接入公司网络/办公系统前须安装企业安全管理配置，未合规终端禁止访问内部服务。\n\n"
        "离职或调岗时须在离岗当日退还公司派发的移动终端；自带设备须由 IT 组执行数据擦除并出具回执。",
    ),
    (
        "访客与门禁管理规定.md",
        "public",
        _kb_md(
            "访客与门禁管理规定",
            [
                "来访人员须由接待人提前预约，在前台登记身份证件并领取访客卡，全程由接待人陪同。",
                "访客卡当日有效，离开时须归还前台并注销；无预约的临时访客须经部门负责人书面批准方可进入。",
                "门禁卡属个人专属，严禁转借；遗失须在 2 小时内报行政部挂失补办。",
            ],
        ),
    ),
    (
        "员工手册（大厅版）.md",
        "public",
        _kb_md(
            "员工手册（大厅版）",
            [
                "公司推行扁平化组织，实行弹性工作制，工作日 9:00-18:00 为对外办公时段。",
                "员工应爱护办公区域公共设施，保持工位整洁；禁止在工位食用有异味食品。",
                "内部沟通优先使用办公协同平台；敏感事项须面对面或加密渠道沟通，严禁在公开渠道讨论涉敏信息。",
            ],
        ),
    ),
    (
        "信息安全事件应急响应流程.txt",
        "public",
        "信息安全事件应急响应流程\n"
        "发现信息安全事件（如系统异常、疑似被入侵、数据泄露）后，第一时间隔离受影响终端/系统并保留现场证据。\n\n"
        "立即上报信息安全组，由组长牵头成立应急小组；根据事件定级决定是否上报管理层与法务。\n\n"
        "事件处理完毕后输出复盘报告，明确根因、整改措施与责任划分。",
    ),
    (
        "数据分级与权限管理规范.txt",
        "public",
        "数据分级与权限管理规范\n"
        "公司数据按敏感度分为公开、内部、敏感三级：公开数据可对内共享；内部数据仅限相关岗位访问；"
        "敏感数据（薪酬、客户资料、未公开财报等）须经权限审批方可访问。\n\n"
        "账号权限遵循最小授权原则，按需申请、定期复核；岗位调整时应同步回收或调整权限。",
    ),
    (
        "采购申请与合同审批流程.txt",
        "public",
        "采购申请与合同审批流程\n"
        "采购需求由业务部门发起《采购申请单》，按金额分档走不同审批路径并附比价/询价材料。\n\n"
        "合同版本须经法务审核后定稿，正式签署前须确认合同编号、金额、交付与付款条款无异议。\n\n"
        "已签署合同原件归档至合同台账，电子版存网盘合同目录；过期/终止合同按档案管理规定归档。",
    ),
    (
        "常用供应商名录.csv",
        "public",
        "",  # csv 内容由 _csv_text 生成
    ),
    (
        "新员工入职办理清单.xlsx",
        "public",
        "",  # xlsx 内容由 _xlsx_bytes 生成，文本列于此仅是占位
    ),
    (
        "固定资产与办公设备领用标准.md",
        "public",
        _kb_md(
            "固定资产与办公设备领用标准",
            [
                "笔记本电脑、显示器、工位家具等固定资产统一由行政部登记入册，使用人领用时签订《领用确认单》。",
                "办公设备损坏或故障时须提交 IT 报修工单，由 IT 组评估维修或更换；人为损坏由责任人承担维修费用。",
                "离职或岗位调整时须在 3 个工作日内归还设备并完成注销，未归还不得办理离职手续。",
            ],
        ),
    ),
    (
        "企业文化与行为准则.md",
        "public",
        _kb_md(
            "企业文化与行为准则",
            [
                "公司倡导「诚实守信、客户第一、拥抱变化、团队协作」四项价值观。",
                "员工应诚信对待客户与同事，不得虚报费用、伪造记录或隐瞒重大风险。",
                "禁止收受供应商、客户的现金、礼品或利益输送；无法回避的礼节性往来须报备。",
            ],
        ),
    ),
    (
        "客服知识库·常见问题 FAQ.md",
        "public",
        _kb_md(
            "客服知识库·常见问题 FAQ",
            [
                "Q1：发票抬头信息是什么？A：发票抬头为公司全称，税号看合同抬头；个人消费不予开具公司发票。",
                "Q2：如何修改收货地址？A：在订单页面点“修改收货信息”，已发货订单联系客服人工协助。",
                "Q3：退换货政策是什么？A：收货 7 天内支持无理由退换，保持商品及包装完好即可联系客服办理。",
                "Q4：什么时候能收到退款？A：退货包裹签收后 3 个工作日内原路退回。",
            ],
        ),
    ),
    (
        "项目交付与验收管理办法.md",
        "public",
        _kb_md(
            "项目交付与验收管理办法",
            [
                "项目立项须提交《项目立项书》，明确目标、范围、里程碑、干系人与验收标准。",
                "项目周会每周一次，产出周报并跟踪风险项；里程碑变更须走变更评审，禁止私自延期。",
                "验收分为内部 POC 验收与客户正式验收两个阶段；验收材料（测试报告、操作手册、演示脚本）齐备后方可提交。",
                "项目结项后 30 天内完成复盘，归档过程文档与产出物至网盘项目目录。",
            ],
        ),
    ),
    (
        "目标与绩效管理（OKR·KPI）.md",
        "public",
        _kb_md(
            "目标与绩效管理（OKR/KPI）",
            [
                "各团队按季度设定部门 OKR，员工对齐部门目标制定个人 KR；季度末进行月度复盘与季度对齐评审。",
                "绩效评估结果分 S/A/B/C 四档，与绩效奖金与调薪挂钩；连续两个季度 C 档将触发绩效改善计划。",
                "评估过程遵循双向沟通原则，直线上级须与员工面谈并就结果达成一致后提交人事备案。",
            ],
        ),
    ),
    (
        "招聘与入职管理办法.md",
        "public",
        _kb_md(
            "招聘与入职管理办法",
            [
                "新岗位编制须经部门负责人与 HR 联合确认，超出编制预算的招聘须经总经理审批。",
                "面试流程为：简历初筛 → 部门一面 → 综合二面 → HR 终面/背景调查 → 发放 Offer。",
                "入职当日由 HR 办理合同签订、社保公积金登记、工牌门禁与设备领取；试用期一般为 3-6 个月。",
            ],
        ),
    ),
    (
        "离职与交接管理办法.md",
        "public",
        _kb_md(
            "离职与交接管理办法",
            [
                "离职申请须提前 30 天书面向直属主管提出，转正员工试用期内提前 3 天即可。",
                "交接清单由离职人所在部门指定，包含工作文件、账号权限、设备与业务进度四类，交接双方签字确认。",
                "未完成交接的离职申请不批准；离职办结后可领取离职证明、结清工资与社保。",
            ],
        ),
    ),
]

#: 保密类文档走 manifest 可见范围（docx 正文写不了 md/txt 那种首行指令，可见范围全靠清单）
KB_DOCX_HR = (
    "保密与信息安全制度",
    [
        "公司商业秘密、客户资料、未公开经营数据均属保密信息，未经授权不得复制、外发或带离办公场所。",
        "严禁将客户资料发送至私人邮箱或外部网盘；对外提供数据须经合规复核并留存审批记录。",
        "发现疑似泄密或异常访问应立即上报信息安全组，严禁私下传播或隐瞒。",
    ],
)


def _docx_bytes(heading: str, paragraphs: list[str]) -> bytes:
    """生成 docx 文档字节（标题 + 段落；不落盘，交由调用方写入）。"""
    buf = io.BytesIO()
    doc = Document()
    doc.add_heading(heading, level=1)
    for text in paragraphs:
        doc.add_paragraph(text)
    doc.save(buf)
    return buf.getvalue()


def _xlsx_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    """生成 xlsx 工作簿字节（单表；不落盘）。"""
    buf = io.BytesIO()
    book = Workbook()
    sheet = book.active
    sheet.title = "清单"
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    book.save(buf)
    return buf.getvalue()


def _pdf_scan_bytes(title: str, lines: list[str]) -> bytes | None:
    """生成扫描件型 PDF（每页渲染成位图，无文本层，模拟归档合同/签署件）。

    返回 bytes；PIL 缺失时返回 None（由调用方跳过并提示）。这与规范中
    「PDF 定位：归档、发布、合同、制度、扫描件，需解析/OCR」保持一致。
    """
    if not _PIL_AVAILABLE:
        return None
    width, height = 1240, 1754  # A4 @150dpi
    pages: list[Image.Image] = []
    current: list[str] = []
    for raw in lines:
        current.extend(_wrap_text(raw, 52))
        if len(current) >= 26:
            pages.append(_render_text_page(current, title, (width, height)))
            current = []
    if current:
        pages.append(_render_text_page(current, title, (width, height)))
    if not pages:
        return None
    buf = io.BytesIO()
    pages[0].save(buf, format="PDF", save_all=True, append_images=pages[1:])
    return buf.getvalue()


def _render_text_page(
    lines: list[str],
    title: str,
    size: tuple[int, int],
) -> Image.Image:
    """把若干行文本渲染成一页白底位图（供扫描件 PDF 与信息图 PNG 复用）。"""
    _, height = size
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    y = 80
    if title:
        title_font = _pick_cjk_font(34)
        draw.text((70, 40), title, fill=(0, 0, 0), font=title_font)
        y = 150
    body_font = _pick_cjk_font(24)
    for line in lines:
        draw.text((70, y), line, fill=(30, 32, 38), font=body_font)
        y += 42
        if y > height - 70:
            break
    return image


def _pptx_bytes(title: str, slides: list[tuple[str, list[str]]]) -> bytes | None:
    """生成 PPTX 课件字节（首屏标题 + 各内容页要点）；python-pptx 缺失返回 None。"""
    if not _PPTX_AVAILABLE:
        return None
    prs = _PptxPresentation()
    first = prs.slides.add_slide(prs.slide_layouts[0])
    first.shapes.title.text = title
    for slide_title, bullets in slides:
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = slide_title
        body = slide.placeholders[1].text_frame
        for bullet in bullets:
            paragraph = body.add_paragraph()
            paragraph.text = bullet
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _png_bytes(title: str, lines: list[str]) -> bytes | None:
    """生成一张信息图 PNG（标题 + 要点），模拟汇报素材/可视化截图。"""
    if not _PIL_AVAILABLE:
        return None
    width, height = 1200, 800
    image = Image.new("RGB", (width, height), (245, 247, 250))
    draw = ImageDraw.Draw(image)
    title_font = _pick_cjk_font(44)
    body_font = _pick_cjk_font(28)
    draw.rectangle((0, 0, width, 120), fill=(18, 93, 201))
    draw.text((60, 28), title, fill="white", font=title_font)
    y = 210
    for index, line in enumerate(lines, start=1):
        for segment in _wrap_text(f"{index}. {line}", 44):
            draw.text((60, y), segment, fill=(40, 44, 52), font=body_font)
            y += 42
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    """把若干命名内容打包成 ZIP（模拟批量导入/导出文档包）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return buf.getvalue()


#: 新员工入职清单（xlsx 内容）
_JOIN_HEADER = ["序号", "事项", "责任方", "完成时限"]
_JOIN_ROWS = [
    ["1", "提交身份证与学历证明、办理员工卡", "人力资源", "入职当日"],
    ["2", "开通协同系统与邮箱账号、领取办公设备", "IT 组", "入职当日"],
    ["3", "参加入职培训与信息安全宣导", "人力资源", "入职一周内"],
    ["4", "直属主管面谈并确认试用期目标", "用人部门", "入职两周内"],
    ["5", "试用期考核并决定转正", "用人部门+人力资源", "试用期届满前"],
]

#: 供应商名录（csv 内容）
_SUPPLIER_HEADER = ["供应商", "品类", "准入状态", "评审日期"]
_SUPPLIER_ROWS = [
    ["云创办公用品", "办公用品", "合格", "2026-03"],
    ["恒信物流", "物流配送", "合格", "2026-05"],
    ["启航云计算", "云资源", "试用", "2026-08"],
    ["天悦财税咨询", "财税服务", "合格", "2026-01"],
    ["瑞德广告传媒", "广告投放", "合格", "2026-04"],
    ["同创网络科技", "软件开发", "合格", "2026-06"],
    ["绿源保洁服务", "保洁绿化", "合格", "2026-02"],
    ["安途汽车租赁", "车辆租赁", "试用", "2026-09"],
]


def _csv_text(header: list[str], rows: list[list[str]]) -> str:
    """把行列渲染成 UTF-8 CSV 文本（极简转义，字段不含逗号/引号/换行）。"""
    lines = [",".join(header)]
    lines.extend(",".join(row) for row in rows)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 二、业务台账（CSV 叠加层：追加到 data_analysis/budget 内置演示行之上）
# ---------------------------------------------------------------------------

LEDGER_CSVS: dict[str, str] = {
    # 销售台账：3 个月 × 6 名销售（8 月为财年冲刺月，金额更高）
    "sales": _csv_text(
        ["person", "month", "amount"],
        [
            ["赵六", "2026-07", "52000"],
            ["赵六", "2026-08", "86000"],
            ["赵六", "2026-09", "94000"],
            ["孙七", "2026-07", "48000"],
            ["孙七", "2026-08", "72000"],
            ["孙七", "2026-09", "91000"],
            ["周八", "2026-07", "61000"],
            ["周八", "2026-08", "98000"],
            ["周八", "2026-09", "105000"],
            ["吴九", "2026-07", "33000"],
            ["吴九", "2026-08", "56000"],
            ["吴九", "2026-09", "68000"],
            ["郑十", "2026-07", "71000"],
            ["郑十", "2026-08", "88000"],
            ["郑十", "2026-09", "76000"],
            ["王十一", "2026-07", "27000"],
            ["王十一", "2026-08", "39000"],
            ["王十一", "2026-09", "51000"],
        ],
    ),
    # 项目台账：覆盖各状态与负责人，支撑「有哪些项目 / 进行到哪一步」问答
    "project": _csv_text(
        ["name", "owner", "status", "progress", "deadline"],
        [
            ["官网改版二期", "产品经理", "进行中", "60", "2026-11-15"],
            ["移动端适配", "前端工程师", "进行中", "40", "2026-12-01"],
            ["数据看板验收", "后端工程师", "已完成", "100", "2026-09-10"],
            ["客服知识库上线", "产品经理", "进行中", "25", "2026-11-30"],
            ["品牌物料设计", "市场专员", "未开始", "0", "2026-10-31"],
            ["安全审计整改", "后端工程师", "未开始", "0", "2026-12-20"],
            ["年会策划", "行政主管", "进行中", "55", "2027-01-10"],
        ],
    ),
    # 工时台账：8 个工作日 × 6 人，支撑「最近一周谁在做什么 / 工时汇总」
    "work": _csv_text(
        ["person", "date", "hours", "task"],
        [
            ["赵六", "2026-09-14", "8", "市场活动方案"],
            ["赵六", "2026-09-15", "7", "官网文案"],
            ["赵六", "2026-09-16", "8", "发布会材料"],
            ["赵六", "2026-09-17", "6", "客户拜访"],
            ["赵六", "2026-09-18", "8", "活动复盘"],
            ["孙七", "2026-09-14", "8", "接口压测"],
            ["孙七", "2026-09-15", "8", "压测报告"],
            ["孙七", "2026-09-16", "7", "故障演练"],
            ["孙七", "2026-09-17", "8", "安全审计整改"],
            ["孙七", "2026-09-18", "4", "代码评审"],
            ["周八", "2026-09-14", "8", "首页开发"],
            ["周八", "2026-09-15", "8", "导航开发"],
            ["周八", "2026-09-16", "8", "视觉走查"],
            ["周八", "2026-09-17", "7", "移动端适配"],
            ["周八", "2026-09-18", "8", "性能优化"],
            ["吴九", "2026-09-14", "6", "客户培训"],
            ["吴九", "2026-09-15", "8", "客服知识库清单"],
            ["吴九", "2026-09-16", "8", "FAQ 编写"],
            ["吴九", "2026-09-17", "7", "内测评审"],
            ["吴九", "2026-09-18", "6", "培训课件"],
            ["郑十", "2026-09-14", "8", "看板设计"],
            ["郑十", "2026-09-15", "8", "看板开发"],
            ["郑十", "2026-09-16", "6", "数据口径核对"],
            ["郑十", "2026-09-17", "8", "验收演示"],
            ["郑十", "2026-09-18", "8", "文档归档"],
            ["王十一", "2026-09-14", "8", "需求访谈"],
            ["王十一", "2026-09-15", "8", "原型设计"],
            ["王十一", "2026-09-16", "7", "用户测试"],
            ["王十一", "2026-09-17", "8", "评审会"],
            ["王十一", "2026-09-18", "7", "需求文档"],
        ],
    ),
    # 考勤台账：两周 × 6 人（含迟到/出差/事假，支撑「本月迟到几次 / 谁出差了」问答）
    "attendance": _csv_text(
        ["person", "date", "status"],
        [
            ["赵六", "2026-09-14", "正常"],
            ["赵六", "2026-09-15", "正常"],
            ["赵六", "2026-09-16", "正常"],
            ["赵六", "2026-09-17", "迟到"],
            ["赵六", "2026-09-18", "正常"],
            ["孙七", "2026-09-14", "出差"],
            ["孙七", "2026-09-15", "出差"],
            ["孙七", "2026-09-16", "正常"],
            ["孙七", "2026-09-17", "正常"],
            ["孙七", "2026-09-18", "正常"],
            ["周八", "2026-09-14", "正常"],
            ["周八", "2026-09-15", "正常"],
            ["周八", "2026-09-16", "迟到"],
            ["周八", "2026-09-17", "正常"],
            ["周八", "2026-09-18", "正常"],
            ["吴九", "2026-09-14", "正常"],
            ["吴九", "2026-09-15", "事假"],
            ["吴九", "2026-09-16", "正常"],
            ["吴九", "2026-09-17", "正常"],
            ["吴九", "2026-09-18", "正常"],
            ["郑十", "2026-09-14", "出差"],
            ["郑十", "2026-09-15", "出差"],
            ["郑十", "2026-09-16", "出差"],
            ["郑十", "2026-09-17", "正常"],
            ["郑十", "2026-09-18", "正常"],
            ["王十一", "2026-09-14", "正常"],
            ["王十一", "2026-09-15", "正常"],
            ["王十一", "2026-09-16", "迟到"],
            ["王十一", "2026-09-17", "迟到"],
            ["王十一", "2026-09-18", "正常"],
        ],
    ),
    # 预算台账：覆盖研发/市场/行政/销售 4 个部门，支撑「哪个项目超支了」问答
    "budget": _csv_text(
        ["project", "department", "year", "budget", "used"],
        [
            ["安全审计整改", "研发部", "2026", "60000", "24000"],
            ["移动端适配", "研发部", "2026", "150000", "90000"],
            ["官网改版二期", "市场部", "2026", "200000", "175000"],
            ["品牌物料设计", "市场部", "2026", "50000", "12000"],
            ["年会物料", "行政部", "2026", "40000", "39000"],
            ["客服知识库上线", "市场部", "2026", "80000", "30000"],
            ["渠道推广费", "销售部", "2026", "120000", "118000"],
        ],
    ),
}

# ---------------------------------------------------------------------------
# 三、网盘文档（DOCS_DIR：供 office.file.read / office.file.ask 解析与问答）
# ---------------------------------------------------------------------------

NETDISK_DOCS: dict[str, str] = {
    "2026-09-25-工作日报.md": (
        "# 工作日报（2026-09-25）\n\n"
        "今日完成：完成知识库后台的上传/解析/向量化功能联调；修复来源清单三态统计口径。\n\n"
        "明日计划：补充批量转 PDF 与整句多语种机翻能力评估；跟进会议纪要落实项的闭环对账。\n\n"
        "风险：本地向量库未配置，语义检索暂走按需计算，待环境就绪后切换常驻灌库。"
    ),
    "2026-09-25-项目周报.md": (
        "# 项目周报（W4）\n\n"
        "本周进展：官网改版完成 60%（首页与导航）；移动端适配完成 40%；数据看板已完成验收。\n\n"
        "下周计划：官网改版进入视觉走查；移动端适配推进到 60%；启动客服知识库上线培训。\n\n"
        "待协调：品牌物料的视觉素材需市场部本周确认，否则影响官网改版里程碑。"
    ),
    "产品评审会议纪要.txt": (
        "产品评审会议纪要（2026-09-25）\n"
        "决议：通过官网改版第二阶段方案，视觉稿下周三前定稿；\n"
        "行动项：前端工程师负责移动端适配的性能基线；产品经理负责客服知识库内容清单；\n"
        "风险：品牌物料素材延期可能拖累官网改版里程碑，市场部需本周内确认。"
    ),
    "会议室设备采购合同样本.docx": _docx_bytes(
        "会议室设备采购合同（样本）",
        [
            "甲方向乙方采购会议室音视频设备一批，含投影、音响与视频会议终端，合同金额以订单为准。",
            "交付时间：合同签订后 15 个工作日内完成安装调试；质保期为验收合格之日起 12 个月。",
            "付款方式：签订后预付 30%，验收合格后支付 60%，质保期满支付剩余 10%。",
        ],
    ),
    "2026-09-值班表.md": (
        "# 2026 年 9 月值班表\n\n"
        "值班人员按周轮换，工作日 18:00-20:00 与周末 09:00-12:00 需有人值守，处理紧急事务。\n\n"
        "第 4 周（09-21 至 09-27）：赵六、孙七；第 5 周（09-28 至 10-04）：周八、吴九。\n\n"
        "值班期间如遇重大事项，须按事件等级逐级上报；值班记录次日提交行政部。"
    ),
    "2026-09-用车记录.txt": (
        "2026 年 9 月公务用车记录\n"
        "09-01 京A·001 客户拜访（北京朝阳） 09:00-15:00 赵六\n"
        "09-03 京A·001 财务取件（税务局） 09:30-12:00 郑十\n"
        "09-08 京A·001 供应商访厂（通州） 08:30-17:30 孙七\n"
        "09-12 京A·002 员工通勤应急（亦庄） 18:30-20:00 行政部\n"
        "09-16 京A·001 项目验收材料领取（海淀） 10:00-14:00 周八\n"
        "09-19 京A·001 活动现场物料运输 08:00-13:00 赵六"
    ),
    "2026-09-客服培训课件.md": (
        "# 客服知识库培训课件（2026-09）\n\n"
        "目标：让一线客服在一周内掌握订单查询、退换货、发票开具与投诉升级四类常用流程。\n\n"
        "重点：订单查询需先确认订单号后 4 位；退换货须引导客户保留商品完好配件；\n"
        "投诉升级标准为超过 2 次未解决或涉及安全问题。\n\n"
        "考核：培训结束后进行情景演练，通过率低于 80% 的学员安排一对一带教。"
    ),
    "2026-Q4-项目冲刺计划.md": (
        "# 2026 Q4 项目冲刺计划\n\n"
        "冲刺目标：Q4 完成官网改版二期、移动端适配、客服知识库上线三个主线项目验收。\n\n"
        "里程碑：10 月底官网视觉走查；11 月中移动端 beta 开放；11 月底客服知识库培训完成。\n\n"
        "资源：品牌物料设计由市场部负责，后端工程师承担安全审计整改，行政部负责年会筹备。"
    ),
    "2026-09-18-周报.md": (
        "# 项目周报（W3，2026-09-18）\n\n"
        "本周进展：官网改版完成 45%，移动端适配完成 25%，数据看板验收完成 90%。\n\n"
        "下周计划：产品评审会定稿官网视觉方案；接口压测收尾；客服知识库清单评审。\n\n"
        "风险：移动端性能基线尚未确定，需产品与技术共同制定。"
    ),
    "2026-09-21-工作日报.md": (
        "# 工作日报（2026-09-21）\n\n"
        "今日完成：移动端首页性能基线代码走查；修复看板接口超时；整理客服 FAQ 初稿。\n\n"
        "明日计划：移动端适配视觉走查；官网智能问答接口联调；参加产品周会。\n\n"
        "风险：官网接口联调依赖云资源环境，待基础设施恢复。"
    ),
    "2026-09-16-产品周会纪要.txt": (
        "产品周会纪要（2026-09-16）\n"
        "出席：产品、研发、设计、市场\n"
        "结论：移动端适配按计划推进，性能基线先按 2 秒首屏口径；客服知识库内容清单本周五提交。\n"
        "行动项：郑十完成看板验收演示；孙七在 09-18 前完成接口压测。"
    ),
    # ---- 多类型文档：PDF（扫描归档件）、PPTX（课件）、PNG（信息图）、HTML（邮件模板）、
    #      JSON/YAML（配置）、ZIP（文档包）——按规范命名「业务域_文档名_版本_日期」并附 meta.json
    "proc_会议室设备采购合同_v1.0_20260927.pdf": _pdf_scan_bytes(
        "会议室设备采购合同（扫描件）",
        [
            "合同编号：CT-2026-0912    签订日期：2026-09-27",
            "甲方：艾梦尔（北京）科技有限公司",
            "乙方：同创网络科技（北京）科技有限公司",
            "一、标的与金额：投影、音响、视频会议终端各一批，总价 128,000 元（含税）。",
            "二、交付：合同签订后 15 个工作日内完成安装调试，逾期按日 0.5% 计违约金。",
            "三、付款：预付款 30%；验收合格后 60%；质保期满（12 个月）后 10%。",
            "四、质保与售后：整机质保 12 个月，核心部件质保 24 个月，乙方免费上门。",
            "五、争议解决：协商不成提交甲方所在地人民法院诉讼解决。",
            "本页为扫描归档件，无文本层，正式文本以另存 Word 版为准。",
        ],
    ),
    "hr_产品培训课纲_v1.0_20260927.pptx": _pptx_bytes(
        "2026 年产品能力培训课纲",
        [
            (
                "为什么需要办公 Agent",
                ["文档与数据分散", "人工翻找与汇总低效", "希望自然语言直达工作项"],
            ),
            ("核心能力地图", ["文档解析与问答", "业务台账数据查询", "待办/日程/审批闭环"]),
            ("落地节奏", ["W39 功能联调", "W40 试运行", "W41 全员培训"]),
        ],
    ),
    "office_Q4项目看板_v1.0_20260927.png": _png_bytes(
        "2026 Q4 主线项目看板",
        [
            "官网改版二期：进度 60%【进行中】",
            "移动端适配：进度 40%【进行中】",
            "数据看板：进度 100%【已完成】",
            "客服知识库：进度 25%【进行中】",
            "安全审计整改：进度 0%【未开始】",
        ],
    ),
    "office_月度服务通知模板_v1.1_20260927.html": (
        """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>服务月报订阅通知</title></head>
<body style="font-family:-apple-system,'Microsoft YaHei',sans-serif;max-width:600px;margin:0 auto">
<h2>9 月服务月报已生成</h2>
<p>尊敬的客户：贵司 9 月服务月报已生成，可在控制台「服务月报」模块查看。</p>
<ul>
  <li>合同编号：SV-2026-0012</li>
  <li>报告周期：2026-09-01 至 2026-09-30</li>
  <li>本期 SLA：99.5%（达标）</li>
  <li>工单处理：42 张，平均 4.2 小时结案</li>
</ul>
<p>如需纸质发票或调整订阅邮箱，请联系您的客户成功经理。</p>
</body>
</html>"""
    ),
    "agent_会议默认配置_v1.0_20260927.json": (
        json.dumps(
            {
                "meeting": {
                    "default_room": "小会议室",
                    "duration_minutes": 60,
                    "notify_attendees": True,
                    "auto_summary": True,
                    "summary_archives_to": "data/docs",
                },
                "ttl": {"minutes": 60},
            },
            ensure_ascii=False,
            indent=2,
        )
    ),
    "agent_工具权限矩阵_v2.1_20260927.yaml": (
        "# 办公 Agent 工具权限矩阵（v2.1）\n"
        "tools:\n"
        "  office.file.read:\n"
        "    scope: office:read\n"
        "    require_approval: false\n"
        "  office.docx.write:\n"
        "    scope: office:write\n"
        "    require_approval: true\n"
        "  office.approval.decide:\n"
        "    scope: office:write\n"
        "    require_approval: true\n"
        "  office.budget.query:\n"
        "    scope: office:read\n"
        "    require_approval: false\n"
    ),
    "project_Q4文档包_v1.0_20260927.zip": _zip_bytes(
        {
            "README.txt": "这是一个演示文档包：含官网改版二期清单与客服知识库 FAQ 摘录。\n",
            "www_改版清单.xlsx": _xlsx_bytes(
                ["模块", "负责人", "进度"],
                [
                    ["首页", "周八", "60%"],
                    ["导航", "周八", "80%"],
                    ["资讯", "孙七", "40%"],
                ],
            ),
            "客服FAQ摘录.txt": "订单查询需确认订单号后 4 位；退款 3 个工作日内原路退回。\n",
        }
    ),
}


def _write_text(root: Path, content: str) -> None:
    """UFT-8 写文本文件（父目录不存在则创建）。"""
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_text(content, encoding="utf-8")


def _write_bytes(root: Path, data: bytes) -> None:
    """写二进制文件（父目录不存在则创建）。"""
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_bytes(data)


async def _seed_kb() -> None:
    """知识库文档经 kb_admin.save_and_ingest 入库（与后台同一口径 → 清单/向量化如实）。"""
    for filename, visibility, body in KB_DOCS:
        if filename.endswith(".xlsx"):
            data = _xlsx_bytes(_JOIN_HEADER, _JOIN_ROWS)
        elif filename.endswith(".csv"):
            data = _csv_text(_SUPPLIER_HEADER, _SUPPLIER_ROWS).encode("utf-8")
        else:
            data = body.encode("utf-8")
        result = await kb_admin.save_and_ingest(
            filename, data, uploaded_by="seed", visibility=visibility
        )
        print(
            f"  知识库 {result['filename']}  →  {result['title']} · "
            f"{result['chunks']} 块 · {result['vector_mode']}"
            + (" · 已覆盖同名来源" if result.get("reuploaded") else "")
        )
    # 保密制度走 manifest visibility=hr（docx 无法写正文指令）
    result = await kb_admin.save_and_ingest(
        "保密与信息安全制度.docx",
        _docx_bytes(KB_DOCX_HR[0], KB_DOCX_HR[1]),
        uploaded_by="seed",
        visibility="hr",
    )
    print(
        f"  知识库 {result['filename']}  →  {result['title']} · "
        f"{result['chunks']} 块 · {result['vector_mode']}"
    )
    # 薪酬细则直接放盘：可见范围由正文首行 visibility: hr 指令决定（不经清单/后台）
    kb_dir = Path(settings.KB_DIR)
    _write_text(kb_dir / "薪酬构成与调薪细则.md", KB_MD_HR)
    print("  知识库 薪酬构成与调薪细则.md → 直接放盘 · 可见范围=正文首行指令 hr（后台标未解析）")


def _seed_ledgers() -> None:
    """台账 CSV 叠加层写到 DOCS_DIR/data/（缺目录则建，幂等覆写）。"""
    data_dir = Path(settings.DOCS_DIR) / "data"
    for dataset, text in LEDGER_CSVS.items():
        _write_text(data_dir / f"{dataset}.csv", text + "\n")
        print(f"  台账 {dataset}.csv → {len(LEDGER_CSVS[dataset].splitlines()) - 1} 行叠加")


#: 网盘文档元数据（仅给多类型文档补齐；单条对应规范「xxx.meta.json」建议字段）
_NETDISK_META: dict[str, dict[str, object]] = {
    "proc_会议室设备采购合同_v1.0_20260927.pdf": {
        "tenant_id": "tenantA",
        "dept_id": "proc",
        "doc_type": "合同",
        "biz_domain": "采购",
        "security_level": "内部",
        "version": "v1.0",
        "effective_date": "2026-09-27",
        "expire_date": "",
        "owner": "admin",
        "source_system": "OA",
        "acl": ["admin", "proc"],
        "is_rag": True,
        "is_writable": False,
    },
    "hr_产品培训课纲_v1.0_20260927.pptx": {
        "tenant_id": "tenantA",
        "dept_id": "hr",
        "doc_type": "培训课件",
        "biz_domain": "人事",
        "security_level": "公开",
        "version": "v1.0",
        "effective_date": "2026-09-27",
        "expire_date": "",
        "owner": "admin",
        "source_system": "OA",
        "acl": ["admin", "hr"],
        "is_rag": True,
        "is_writable": True,
    },
    "office_Q4项目看板_v1.0_20260927.png": {
        "tenant_id": "tenantA",
        "dept_id": "office",
        "doc_type": "报表图",
        "biz_domain": "行政",
        "security_level": "公开",
        "version": "v1.0",
        "effective_date": "2026-09-27",
        "expire_date": "",
        "owner": "admin",
        "source_system": "内部系统",
        "acl": ["admin"],
        "is_rag": True,
        "is_writable": False,
    },
    "agent_工具权限矩阵_v2.1_20260927.yaml": {
        "tenant_id": "tenantA",
        "dept_id": "agent",
        "doc_type": "配置",
        "biz_domain": "平台",
        "security_level": "内部",
        "version": "v2.1",
        "effective_date": "2026-09-27",
        "expire_date": "",
        "owner": "admin",
        "source_system": "Agent 平台",
        "acl": ["admin"],
        "is_rag": False,
        "is_writable": True,
    },
}


def _seed_netdisk() -> None:
    """网盘文档写到 DOCS_DIR 根（文本/二进制全类型），多类型文档另配 meta.json。"""
    docs_dir = Path(settings.DOCS_DIR)
    for filename, payload in NETDISK_DOCS.items():
        if isinstance(payload, bytes):
            _write_bytes(docs_dir / filename, payload)
        else:
            _write_text(docs_dir / filename, payload)
        print(f"  网盘 {filename}")
    for filename, meta in _NETDISK_META.items():
        _write_text(
            docs_dir / f"{filename}.meta.json",
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        )
        print(f"  网盘 {filename}.meta.json（元数据）")


# ---------------------------------------------------------------------------
# 四、业务事务台账（DOCS_DIR/data/*.json：待办/日程、资源预订、行政工单、审批台账）
#     ——直接覆写为干净演示数据，同时清掉历史冒烟测试留下的「写周报/冒烟评审」噪声行
# ---------------------------------------------------------------------------

#: 待办与日程（office.todo.list 按 owner 过滤；留一批与网盘文档呼应的真实待办）
_TODOS: list[dict] = [
    {
        "id": "todo-0001",
        "tenant": "demo-tenant",
        "owner": "admin",
        "title": "官网改版视觉稿定稿",
        "description": "与设计评审 Q4 官网改版二期视觉稿，输出定稿版本",
        "priority": "high",
        "due_date": "2026-10-15",
        "status": "open",
        "created_at": "2026-09-24 08:00:00 UTC",
        "updated_at": "2026-09-24 08:00:00 UTC",
        "completed_at": "",
    },
    {
        "id": "todo-0002",
        "tenant": "demo-tenant",
        "owner": "admin",
        "title": "客服知识库内容清单评审",
        "description": "评审 FAQ 清单与培训课件初稿，周五前闭环",
        "priority": "medium",
        "due_date": "2026-10-02",
        "status": "open",
        "created_at": "2026-09-24 08:05:00 UTC",
        "updated_at": "2026-09-24 08:05:00 UTC",
        "completed_at": "",
    },
    {
        "id": "todo-0003",
        "tenant": "demo-tenant",
        "owner": "admin",
        "title": "移动端性能基线走查",
        "description": "按 2 秒首屏口径核对移动端适配性能基线",
        "priority": "high",
        "due_date": "2026-10-20",
        "status": "open",
        "created_at": "2026-09-24 08:10:00 UTC",
        "updated_at": "2026-09-24 08:10:00 UTC",
        "completed_at": "",
    },
    {
        "id": "todo-0004",
        "tenant": "demo-tenant",
        "owner": "admin",
        "title": "Q4 项目冲刺计划同步",
        "description": "向团队同步 Q4 三个主线项目的里程碑与资源安排",
        "priority": "medium",
        "due_date": "2026-09-30",
        "status": "open",
        "created_at": "2026-09-24 08:15:00 UTC",
        "updated_at": "2026-09-24 08:15:00 UTC",
        "completed_at": "",
    },
    {
        "id": "todo-0005",
        "tenant": "demo-tenant",
        "owner": "admin",
        "title": "产品周会纪要归档",
        "description": "把 09-16 产品周会决议与行动项归档至网盘",
        "priority": "low",
        "due_date": "",
        "status": "done",
        "created_at": "2026-09-16 16:00:00 UTC",
        "updated_at": "2026-09-17 10:00:00 UTC",
        "completed_at": "2026-09-17 10:00:00 UTC",
    },
]

#: 资源预订（office.resource.query 按 date 判占用；留几条约 2 周内的真实预订）
_BOOKINGS: list[dict] = [
    {
        "resource_id": "room-big",
        "resource_name": "大会议室",
        "date": "2026-10-01",
        "start": "14:00",
        "end": "16:00",
        "purpose": "官网改版二期评审会",
        "booked_by": "admin",
        "tenant": "demo-tenant",
        "booked_at": "2026-09-24 09:00:00 UTC",
        "idem_key": "seed-booking-0001",
    },
    {
        "resource_id": "room-small",
        "resource_name": "小会议室",
        "date": "2026-10-02",
        "start": "10:00",
        "end": "11:00",
        "purpose": "客服知识库 FAQ 评审",
        "booked_by": "admin",
        "tenant": "demo-tenant",
        "booked_at": "2026-09-24 09:10:00 UTC",
        "idem_key": "seed-booking-0002",
    },
    {
        "resource_id": "car-01",
        "resource_name": "公务车京A·001",
        "date": "2026-10-08",
        "start": "09:00",
        "end": "17:00",
        "purpose": "供应商访厂（通州）",
        "booked_by": "admin",
        "tenant": "demo-tenant",
        "booked_at": "2026-09-24 09:20:00 UTC",
        "idem_key": "seed-booking-0003",
    },
]

#: 行政工单（office.desk.tickets 读端：kind/status 过滤）
_DESK_TICKETS: list[dict] = [
    {
        "id": "desk-0001",
        "tenant": "demo-tenant",
        "kind": "it_repair",
        "kind_label": "IT 报修",
        "title": "工位显示器闪烁",
        "detail": "A 区工位 01 的显示器间歇性闪烁，需检测",
        "status": "open",
        "created_by": "admin",
        "created_at": "2026-09-24 09:30:00 UTC",
        "idem_key": "seed-desk-0001",
    },
    {
        "id": "desk-0002",
        "tenant": "demo-tenant",
        "kind": "asset_claim",
        "kind_label": "资产申领",
        "title": "申领无线鼠标",
        "detail": "新员工入职设备清单补充无线鼠标 1 个",
        "status": "open",
        "created_by": "admin",
        "created_at": "2026-09-24 09:40:00 UTC",
        "idem_key": "seed-desk-0002",
    },
    {
        "id": "desk-0003",
        "tenant": "demo-tenant",
        "kind": "work_order",
        "kind_label": "工单创建",
        "title": "会议室设备巡检",
        "detail": "十月中旬对全部会议室音视频设备做季度巡检",
        "status": "open",
        "created_by": "admin",
        "created_at": "2026-09-24 09:50:00 UTC",
        "idem_key": "seed-desk-0003",
    },
]

#: 审批台账（office.approval.* 落账展示：已批准生效单据）
_APPROVAL_TICKETS: list[dict] = [
    {
        "ticket_id": "tkt-20260924-01",
        "tenant": "demo-tenant",
        "applicant": "admin",
        "kind": "expense",
        "kind_label": "报销申请",
        "fields": {
            "amount": "6800",
            "expense_date": "2026-09-20",
            "reason": "客户拜访打车与餐费",
        },
        "idem_key": "seed-expense-0001",
        "status": "approved",
        "submitted_at": "2026-09-24 10:00:00 UTC",
    },
    {
        "ticket_id": "tkt-20260924-02",
        "tenant": "demo-tenant",
        "applicant": "admin",
        "kind": "leave",
        "kind_label": "请假申请",
        "fields": {
            "days": "1",
            "reason": "个人事务（家事）",
        },
        "idem_key": "seed-leave-0001",
        "status": "approved",
        "submitted_at": "2026-09-24 10:20:00 UTC",
    },
    {
        "ticket_id": "tkt-20260924-03",
        "tenant": "demo-tenant",
        "applicant": "admin",
        "kind": "purchase",
        "kind_label": "采购申请",
        "fields": {
            "item": "办公用纸与墨盒一批",
            "amount": "3200",
        },
        "idem_key": "seed-purchase-0001",
        "status": "approved",
        "submitted_at": "2026-09-24 10:40:00 UTC",
    },
]


def _seed_json_ledgers() -> None:
    """把四类业务事务台账覆写为干净演示数据（顺带清掉冒烟测试噪声行）。"""
    data_dir = Path(settings.DOCS_DIR) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    stores = {
        "affairs.json": {"todos": _TODOS, "schedules": []},
        "resource_bookings.json": {"bookings": _BOOKINGS},
        "desk_tickets.json": {"tickets": _DESK_TICKETS},
        "approvals_ledger.json": {"tickets": _APPROVAL_TICKETS},
    }
    for filename, payload in stores.items():
        (data_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        count = len(payload[next(iter(payload))])
        print(f"  台账 {filename} → {count} 条干净演示数据（已清冒烟噪声）")


def _clean_smoke_artifacts() -> None:
    """清理 DOCS_DIR 根冒烟测试残留（smoke-v*.pptx），保持演示网盘内容可控。"""
    docs_dir = Path(settings.DOCS_DIR)
    for path in docs_dir.glob("smoke-v*"):
        if path.is_file():
            path.unlink()
            print(f"  清理 {path.name}")


async def main() -> None:
    """按知识库 → 台账 → 网盘 → 业务事务台账顺序填充种子内容并打印回执。"""
    print(f"知识库目录：{settings.KB_DIR}")
    await _seed_kb()
    print(f"台账/网盘目录：{settings.DOCS_DIR}")
    _seed_ledgers()
    _clean_smoke_artifacts()
    _seed_netdisk()
    _seed_json_ledgers()
    print("演示数据已补齐（幂等，可重复运行）。")


if __name__ == "__main__":
    asyncio.run(main())
