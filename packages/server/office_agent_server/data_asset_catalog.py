"""数据资产目录入口（唯一清单存同目录 data_asset_catalog.json）

职责：数据资产/数据文档目录的唯一代码入口，供 data_assets 表、种子脚本、
      文档生成器和 API 共用。清单数据存于同包 `data_asset_catalog.json`，
      import 时加载为 DATA_ASSET_CATALOG；资产形态/摘要等纯函数也在此文件。

口径：
  - 新增/改名资产只改 `data_asset_catalog.json`，禁止在 API/文档/测试另写一份。
  - `asset_kind()` 按名称后缀推断「数据表/模板/规则配置/文档/资产」。
  - `asset_summary()` 生成登记摘要（分类职责 + 资产形态），不编造字段级细节。
  - 本文件不依赖 ORM / FastAPI，可被普通脚本直接 import。
对齐：AGENTS.md §3（零硬编码/数据集中一处）；README 数据资产目录一节。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

_DATA_FILE: Final[Path] = Path(__file__).with_name("data_asset_catalog.json")

DATA_ASSET_CATALOG: Final[dict[str, tuple[str, ...]]] = {
    category: tuple(assets)
    for category, assets in json.loads(_DATA_FILE.read_text(encoding="utf-8")).items()
}


def catalog_items() -> list[dict[str, str]]:
    """拍平为行数据（category/asset_name），供库表与文档共用。"""
    return [
        {"category": category, "asset_name": name}
        for category, names in DATA_ASSET_CATALOG.items()
        for name in names
    ]


#: 按资产名后缀推断的形态（表 / 模板 / 文档 / 规则 / 其他）
_KIND_RULES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("数据表", ("表", "台账")),
    ("模板", ("模板",)),
    ("规则/配置", ("规则", "配置", "策略", "参数", "矩阵", "规范")),
    (
        "文档",
        ("文档", "手册", "说明", "指南", "协议", "预案", "材料", "清单", "记录", "报告", "计划"),
    ),
)


def asset_kind(asset_name: str) -> str:
    """按名称后缀推断资产形态，供目录文档与种子 description 展示。"""
    for kind, suffixes in _KIND_RULES:
        if any(asset_name.endswith(suffix) for suffix in suffixes):
            return kind
    return "资产"


#: 各分类一句话口径（文档生成器/种子 description 共用，禁止在别处另写一份）
_CATEGORY_INTRO: Final[dict[str, str]] = {
    "产品需求类": "产品全生命周期需求与交付文档，覆盖从需求收集、版本规划到上线回滚和培训手册的端到端记录。",
    "用户权限与安全类": "用户、组织、角色权限、租户隔离、审计日志与安全合规资产，支撑最小授权、可追溯与数据分级保护。",
    "办公内容与模板类": "高频办公内容的标准模板和字段化模板配置，统一周报、会议、审批、资源预订等文档结构。",
    "文档处理类": "文档解析、摘要、润色、格式转换、批量处理与多文档整合的过程记录和规则资产。",
    "术语与翻译类": "企业术语、多语种对照与翻译质量资产，统一内部语言并沉淀可复用词库。",
    "待办日程类": "待办、日程、会议预约、简报与主动推送相关数据资产，覆盖任务从创建到提醒留痕的闭环。",
    "审批流程类": "审批单、流程配置、审批记录、校验规则、台账与风险词库，支撑写动作恒送审与过程留痕。",
    "数据查询与分析类": "数据源、指标、查询复用、图表、导出与快照资产，覆盖自助分析从取数到结论的链路。",
    "会议协作类": "会议、议程、资料包、纪要、行动项、风险与五态对账资产，沉淀会议决策到落实的追踪数据。",
    "知识库与RAG类": "知识库、切片、向量配置、召回策略、问答记录与评测集，支撑可治理的知识检索问答链路。",
    "邮件与IM类": "邮件、IM 消息、归类语料、回复模板与预审规则，覆盖对外沟通和群消息处理的规则化。",
    "人事行政资源类": "员工、考勤、资源预订与行政工单资产，沉淀人力资源和行政服务的数据基础。",
    "项目管理类": "项目、里程碑、任务拆解、依赖、交付物与复盘资产，覆盖项目从立项到结项的追踪。",
    "财务辅助类": "预算、费用、发票、财务权限与接口资产，辅助财务查询分析和审批规则落地。",
    "高阶自动化类": "定时任务、工作流调度、RPA、工具注册、会话与幂等资产，支撑自动化编排和治理链路。",
    "Agent工程类": "系统架构、接口、Prompt、标注集、配置与路由资产，沉淀 Agent 工程化所需的数据和规则。",
    "系统管理与运营类": "平台统计、配置、字典、错误码、运营看板与用户反馈资产，支撑平台运营与持续观测。",
    "测试与评测类": "测试计划、用例、自动化脚本、评测集与验收资产，覆盖功能、安全和模型质量验证。",
    "集成与接口类": "外部系统对接、鉴权、映射、同步与接口治理资产，支撑多系统集成和接口稳定性。",
    "运维与高可用类": "部署、环境、监控、备份、降级、重试与链路资产，保障服务可用性和可恢复性。",
    "项目交付与落地类": "项目立项、交付、验收、落地记录、效果评估与复盘资产，沉淀项目价值与实施经验。",
}


def category_intro(category: str) -> str:
    """读取分类一句话口径（文档/API 展示用，缺失时给兜底文案）。"""
    return _CATEGORY_INTRO.get(category, "数据资产目录，按业务域登记数据文档/数据资产。")


def asset_summary(category: str, asset_name: str) -> str:
    """生成目录登记的用途摘要（不编造字段级细节）。"""
    kind = asset_kind(asset_name)
    return f"{category_intro(category)} 本项按「{kind}」登记，具体字段/内容由实现阶段细化。"
