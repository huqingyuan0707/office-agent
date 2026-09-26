"""企业知识库问答工具（kb.ask：双通道检索 + 条目级权限过滤，对齐 PRD §2.6）。

职责：
- kb.ask（office:read）：对「内置演示条目 + KB_DIR 本地文件」做检索，返回 top_k 命中片段，
  带 source + fetched_at 溯源；库为空时 degraded=True 留白不编造答案。
- 本地文件支持 pdf/docx/xlsx/csv/txt/md（与 office.file.read 同一提取口径）——知识库后台
  office_agent_tools_office.kb_admin 入盘的文件即刻可被检索，无需额外注册步骤。
- 检索双通道：**向量语义检索**优先（Settings 的 EMBEDDING_* 配置本地 OpenAI 兼容
  /embeddings，按段落余弦相似度排序）→ 未配置或调用失败时回退**字符 bigram 检索**。
  降级只在结果里如实标注（retrieval_mode / retrieval_fallback_reason），绝不 500。
- 权限适配（PRD §2.6）：每条目带 `visibility`（`public` 或角色名如 `hr`）——
  检索前按 ToolContext.roles 过滤（`*`/`admin` 可见全部，其余需精确命中角色名），
  被滤条目只计 `permission_filtered` 个数，标题与正文一律不外泄；pdf/docx/xlsx 的可见范围
  取来源清单（后台上传时选定），md/txt 另支持首非空行 `visibility: hr` 指令（该行不计入正文）。
- 检索原语（切块 / 双通道 / 片段口径）已抽到同包 retrieval.py，与 office.file.ask 共用。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler。
红线：纯本地实现，不触及 ORM / FastAPI；答案只摘录命中条目原文（不生成、不编造）。
      向量检索走本地私有化部署（数据不出域）；外部依赖不可用走降级路径，绝不 500。
对齐：AGENTS.md §3（分层红线 / 降级绝不 500 / 数据不出域）；
      智能办公Agent 产品需求文档.md §5.1（V1.0 知识库问答）、
      §2.6（制度答疑/资料检索/权限适配——Scope 闸门管入口，visibility 管条目）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register
from office_agent_core.settings import settings
from office_agent_tools_office import file_read, kb_admin
from office_agent_tools_office.retrieval import (
    chunks_with_meta,
    first_snippet,
    retrieve,
)

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"

#: 可见性与正文指令只对纯文本资料生效（pdf/docx/xlsx 的首行是正文，不该被当指令吃掉）
_DIRECTIVE_SUFFIXES = (".md", ".txt")

#: 可见性口径：public 人人可见；其余值为角色名，需调用方 roles 精确命中；
#: 通配 `*` 与 `admin` 可见全部（含受限条目，管理口径）。
VISIBILITY_PUBLIC = "public"
_PRIVILEGED_ROLES = ("*", "admin")

#: 内置演示条目（source=builtin-demo；真实部署用 KB_DIR 目录替换/追加）
#: 新增人事/行政/合规三条覆盖 PRD §2.6 制度域；薪酬条目 visibility=hr 演示权限过滤。
_BUILTIN_ENTRIES: list[dict[str, str]] = [
    {
        "title": "考勤制度（演示条目）",
        "content": "工作日 9:00-18:00，弹性上班 8:30-10:00 之间到岗即视为正常；"
        "每月补卡不超过 3 次；连续迟到 3 次以上需向直属主管说明原因。",
        "visibility": "public",
    },
    {
        "title": "报销制度（演示条目）",
        "content": "报销单需在费用发生后 30 天内提交，附发票原件；"
        "单笔超过 1000 元需部门负责人审批，超过 5000 元需分管副总审批；"
        "报销周期为每周三统一打款。",
        "visibility": "public",
    },
    {
        "title": "请假流程（演示条目）",
        "content": "1 天以内请假由直属主管审批；3 天以内需提前 1 天申请；"
        "3 天以上需提前 3 个工作日申请并做好工作交接；病假需补交医院证明。",
        "visibility": "public",
    },
    {
        "title": "差旅标准（演示条目）",
        "content": "高铁二等座、经济舱为默认标准；住宿一线城市每晚上限 500 元，"
        "其他城市上限 350 元；市内交通实报实销，需保留行程凭证。",
        "visibility": "public",
    },
    {
        "title": "人事制度（演示条目）",
        "content": "试用期三个月，转正需提交总结与主管评价；"
        "入职携带身份证与学历证明办理手续；调岗需经双方主管确认。",
        "visibility": "public",
    },
    {
        "title": "行政制度（演示条目）",
        "content": "场地经日历预约先到先得；用印经线上提交后至前台盖章；"
        "办公用品每月初申领，紧急需求可单独申请。",
        "visibility": "public",
    },
    {
        "title": "合规红线（演示条目）",
        "content": "客户资料不得外发私人邮箱；对外提供数据须经合规复核；"
        "发现疑似泄密立即上报安全组，严禁私下传播。",
        "visibility": "public",
    },
    {
        "title": "薪酬保密制度（演示条目）",
        "content": "职级薪档仅本人与人力资源可见；不得打听他人薪酬；"
        "调薪每年四月统一窗口，其余时间不单独调整。",
        "visibility": "hr",
    },
]


def can_view(visibility: str, roles: list[str]) -> bool:
    """条目可见性判定（权限适配唯一出处，kb.ask 与跨源检索共用）。

    public 人人可见；`*`/`admin` 可见全部；其余需 roles 精确命中角色名。
    未知 visibility 值按受限处理（未知即从严，不默认放行）。
    """
    if visibility == VISIBILITY_PUBLIC:
        return True
    if any(role in _PRIVILEGED_ROLES for role in roles):
        return True
    return visibility in roles


def parse_visibility_block(text: str) -> tuple[str, str]:
    """解析 KB 文件首行 visibility 指令，返回 (visibility, 剩余正文)。

    首个非空行形如 `visibility: hr`（大小写不敏感，支持中文冒号）即为指令行，
    该行从正文剔除；无指令行一律 public。指令值取首个空白前 token 并小写化。
    """
    lines = text.splitlines()
    head = 0
    while head < len(lines) and not lines[head].strip():
        head += 1
    if head >= len(lines):
        return VISIBILITY_PUBLIC, text
    first = lines[head].strip()
    lowered = first.lower().replace("：", ":")
    if not lowered.startswith("visibility:"):
        return VISIBILITY_PUBLIC, text
    tail = lowered.split("visibility:", 1)[1].strip()
    value = tail.split()[0] if tail else VISIBILITY_PUBLIC
    rest = "\n".join(lines[:head] + lines[head + 1 :]).strip()
    return value or VISIBILITY_PUBLIC, rest


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def load_entries() -> tuple[list[dict[str, str]], bool]:
    """合并内置条目与 KB_DIR 本地文件；返回 (条目列表, 目录是否缺失)。

    文件侧支持 pdf/docx/xlsx/csv/txt/md，统一走 file_read.extract_document 提取口径——
    后台上传（kb_admin.save_and_ingest）与手工放盘的文件被同一装载口径读取，检索行为一致。
    可见范围优先取来源清单（后台上传时选定，pdf/docx 等无法在正文里写指令的格式全靠它）；
    md/txt 另支持首非空行 `visibility: xxx` 指令。解析引擎缺失/损坏的文件跳过并告警。
    公开给跨源联合检索复用同一装载口径（含 visibility 解析，不重复实现）。
    """
    entries = [
        {
            "title": item["title"],
            "content": item["content"],
            "source": "builtin-demo",
            "visibility": item.get("visibility", VISIBILITY_PUBLIC),
        }
        for item in _BUILTIN_ENTRIES
    ]
    root = Path(settings.KB_DIR)
    if not root.is_dir():
        return entries, True
    try:
        files = sorted(
            path
            for path in root.iterdir()
            if path.is_file() and path.suffix.lower() in kb_admin.KB_SUFFIXES
        )
    except OSError as exc:
        logger.warning("知识库目录不可读（降级为内置条目）：%s", str(exc)[:120])
        return entries, True
    declared = kb_admin.manifest_visibility()
    for path in files:
        try:
            if path.stat().st_size > file_read.MAX_FILE_BYTES:
                logger.warning("知识文件过大已跳过：%s", path.name)
                continue
        except OSError as exc:
            logger.warning("知识文件读取失败已跳过 %s：%s", path.name, str(exc)[:120])
            continue
        try:
            extracted = file_read.extract_document(path)
        except (OSError, ValueError) as exc:
            logger.warning("知识文件解析失败已跳过 %s：%s", path.name, str(exc)[:120])
            continue
        body = str(extracted.get("text") or "").strip()
        if not body or extracted.get("degraded"):
            continue
        file_visibility = VISIBILITY_PUBLIC
        if path.suffix.lower() in _DIRECTIVE_SUFFIXES:
            file_visibility, body = parse_visibility_block(body)
        visibility = declared.get(path.name) or file_visibility
        first_line = next((ln.strip("# \t") for ln in body.splitlines() if ln.strip()), "")
        entries.append(
            {
                "title": first_line or path.stem,
                "content": body,
                "source": f"local-kb:{os.path.basename(path.name)}",
                "visibility": visibility,
            }
        )
    return entries, False


def _load_entries() -> tuple[list[dict[str, str]], bool]:
    """历史私有入口保留（单测与旧引用兼容，行为与 load_entries 一致）。"""
    return load_entries()


async def _kb_ask(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """kb.ask：权限过滤后检索命中条目并摘录原文片段（不生成、不编造）。"""
    query = str(args.get("query") or "").strip()
    if not query:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 query 不能为空：请输入要咨询的问题")
    top_k = args.get("top_k", 3)
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 10:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 top_k 必须是 1-10 的整数")

    entries, dir_missing = await asyncio.to_thread(load_entries)
    visible = [
        entry
        for entry in entries
        if can_view(str(entry.get("visibility", VISIBILITY_PUBLIC)), list(ctx.roles))
    ]
    permission_filtered = len(entries) - len(visible)
    chunks = chunks_with_meta(visible)
    # 三级降级链统一入口（milvus → embedding 全量重算 → bigram），口径收口在 retrieval.retrieve
    scored, mode, fallback_reason = await retrieve(query, chunks, top_k, origin="knowledge")

    results = [
        {
            "title": chunk["title"],
            "snippet": first_snippet(chunk["text"], query, mode),
            "score": round(score, 4),
            "source": chunk["source"],
            "visibility": chunk.get("visibility", VISIBILITY_PUBLIC),
        }
        for score, chunk in scored
    ]
    return {
        "query": query,
        "results": results,
        "count": len(results),
        "retrieval_mode": mode,
        "embedding_model": settings.EMBEDDING_MODEL if mode in ("embedding", "milvus") else "",
        "retrieval_fallback_reason": fallback_reason,
        "permission_filtered": permission_filtered,
        "degraded": not results,
        "degraded_reason": (
            "知识库中没有命中内容：请换个说法，或让管理员往知识目录补充资料" if not results else ""
        ),
        "kb_dir": settings.KB_DIR,
        "kb_dir_missing": dir_missing,
        "source": "local-kb",
        "fetched_at": _now_text(),
    }


def specs() -> tuple[ToolSpec, ...]:
    """kb.ask 的 ToolSpec。"""
    return (
        ToolSpec(
            name="kb.ask",
            scope=SCOPE_READ,
            description="企业知识库制度问答：检索内置条目与知识目录（KB_DIR，pdf/docx/xlsx/csv/txt/md，与文件解析同一提取口径），按段落返回命中原文片段与来源溯源；配 EMBEDDING_* 时走向量语义检索（改说法也能命中），未配置或调用失败自动回退字符检索并在 retrieval_mode 如实标注；条目级权限过滤（visibility=public 或角色名，后台入盘时选定或 KB_DIR 纯文本首行 visibility: xxx 声明，* /admin 可见全部，被滤只计 permission_filtered 不外泄标题正文）；无命中时如实告知 degraded，绝不编造答案",
            params={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "要咨询的问题（如：报销超 1000 元找谁审批）",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "top_k": {"type": "integer", "description": "返回命中条数（1-10，默认 3）"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=_kb_ask,
        ),
    )


def register_all() -> list[str]:
    """注册知识库工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
