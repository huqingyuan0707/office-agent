"""跨智能体路由（对话主入口：员工一句话 → 自动挑智能体）

链路：POST /runs（agent 省略）→ route_agent_spec(goal) → 命中 AgentSpec → start_run 照常受理。
三段收口，纯寒暄最先：
  0) 纯寒暄（「你好」等问候语剥除后无剩余）：不进规则、不进 LLM，直接 1001 中文引导
     （附可用说法示例 + 已装载智能体清单）——寒暄无业务目标，进 LLM 兜底只会空转
     十几秒再 FAILED（本地 8B 实测约 17s），不如零网络秒回；
  1) 规则智能体（有 rules）：goal 命中任一规则关键词即选中（RulePlanner.plan 非空，
     与执行期规划同一匹配口径，路由选中的智能体执行期规划必然成功，不出现「路由说行、执行说不行」）；
  2) LLM 智能体（llm profile 非空）：规则全不中时兜底——由 LLM 决定工具调用，
     LLM 不可用时执行期降级链自行收敛（规划失败在时间线里如实呈现，路由不预判）。
全不中：1001 中文可操作报错，列出已装载智能体及其能力描述，绝不瞎猜一个。

红线：路由只「挑智能体」不执行任何工具；纯函数零 IO（specs 由调用方所在模块注入），
     不触及 ORM / FastAPI；与内核「planner 只提议不执行」同纪律。
对齐：AGENTS.md §3（分层红线）；.trae/documents/智能体编排层实现方案.md §4
     （POST /runs 契约：agent 可选，省略即自动路由）；PRD §4.3（纯自然语言交互零门槛）。
"""

from __future__ import annotations

from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_runtime.loader import load_agent_specs
from office_agent_runtime.planner.llm import profile_configured
from office_agent_runtime.planner.rule import RulePlanner
from office_agent_runtime.spec import AgentSpec

#: 纯寒暄词表（问候/致谢/告别，大小写不敏感；命中后还要看剥除后有无剩余业务文本）。
_GREETING_WORDS = (
    "您好",
    "你好",
    "早上好",
    "中午好",
    "下午好",
    "晚上好",
    "哈喽",
    "hello",
    "hey",
    "hi",
    "嗨",
    "喂",
    "在吗",
    "在么",
    "谢谢",
    "多谢",
    "再见",
    "拜拜",
    "辛苦了",
)

#: 问候词按长度倒排（「早上好」先于「好」类短词匹配，短路即停）。
_GREETING_WORDS_BY_LEN = tuple(sorted(_GREETING_WORDS, key=len, reverse=True))

#: 剥除问候前后缀时的标点与空白（含全角）。
_GREETING_TRIM = " \t\r\n，。、；;：:！!？?～~·.…—-「」『』\"'()（）【】"

#: 问候语气尾字（「你好呀」→ 剥「你好」后再剥一字「呀」，余空即纯寒暄）。
_GREETING_TRAILERS = ("呀", "啊", "呢", "吧", "哦", "哟", "呐")

#: 纯寒暄引导文案唯一出处（路由 1001 与即时 DONE run 的终答共用，改说法只改这里）。
GREETING_GUIDE = (
    "请用一句话说明要办的事，例如：「生成今天的工作日报」「查一下今天日程」"
    "「记一下明天要跟进的事」「做一次经营数据分析」。"
)


def _strip_leading_greetings(text: str) -> str:
    """剥除开头的问候词（含标点与一字语气尾）：返回剩余业务文本（余空即纯寒暄）。"""
    rest = text
    while True:
        head = rest.lstrip(_GREETING_TRIM)
        hit = ""
        for word in _GREETING_WORDS_BY_LEN:
            if head.startswith(word):
                hit = word
                break
        if not hit:
            return head.strip(_GREETING_TRIM)
        rest = head[len(hit) :]
        for trailer in _GREETING_TRAILERS:
            if rest.startswith(trailer):
                rest = rest[len(trailer) :]
                break


def is_greeting_only(goal: str) -> bool:
    """整句是否纯寒暄：问候词剥除后无剩余（「你好，帮我出日报」余下业务词，不算）。

    公开供受理层复用：POST /runs 省略 agent 时纯寒暄直接走即时 DONE run（200 秒回），
    不经本模块的 1001——对话页走正常 run 路径渲染终答，不依赖前端错误分支。
    """
    text = goal.strip().lower()
    if not text:
        return False
    return _strip_leading_greetings(text) == ""


def route_agent_spec(goal: str) -> AgentSpec:
    """按目标为一句话挑智能体：纯寒暄秒回引导，规则命中优先，LLM 智能体兜底，全不中 1001。

    specs 每次实扫 plugins/*/agent.yaml（与清单/受理同口径，配置热更新零重启）；
    扫描顺序稳定（loader 按目录名排序），命中多个规则时取声明序第一个，可预期。
    LLM 兜底只挑 profile 确已配置的智能体（零网络查 LLM_PROVIDERS）——没配就不接，
    让用户立刻看到「没人能办」而不是等一轮执行期失败。
    """
    specs = load_agent_specs()
    known = "；".join(f"{spec.name}（{spec.description}）" for spec in specs) or "暂无已装载智能体"
    if is_greeting_only(goal):
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            f"你好！我是智能办公助手，{GREETING_GUIDE}已装载智能体：{known}，换个说法即可办理",
        )
    for spec in specs:
        if spec.rules and RulePlanner(spec).plan(goal):
            return spec
    for spec in specs:
        if spec.llm and profile_configured(spec.llm):
            return spec
    raise BusinessError(
        ErrorCode.PARAM_INVALID,
        f"没有智能体能处理这件事：「{goal[:60]}」。已装载智能体：{known}，"
        "请换个说法或联系管理员补充智能体规则",
    )
