"""错误码分段（新增必须落号段）

号段：1xxx 通用 / 2xxx 模型 / 3xxx **预留给领域插件** / 4xxx 任务与工具 / 5xxx 系统。

领域码红线：某个业务域的专属码（如「对象不存在」的域内细分）不落内核，由插件按 3xxx 号段
自定；内核允许 BusinessError 携带任意原始码（含上游透传码），不做白名单裁剪——
否则跨系统联动时上游的域内码会被内核「翻译」成另一套语义，制造第二真相源。
"""

from __future__ import annotations

from enum import IntEnum


class ErrorCode(IntEnum):
    """内核错误码（3xxx 段留空，供领域插件占用）。"""

    OK = 0
    PARAM_INVALID = 1001
    UNAUTHORIZED = 1002
    FORBIDDEN = 1003
    NOT_FOUND = 1004
    QUOTA_EXCEEDED = 1005
    RATE_LIMITED = 1006
    LLM_FAILED = 2000
    NO_EVIDENCE = 2001
    CONVERSATION_LIMITED = 2002
    UNSAFE_CONTENT = 2003
    IMAGE_TOO_LARGE = 2004
    # ---- 3xxx：预留给领域插件，内核不占用 ----
    TASK_NOT_FOUND = 4001
    TASK_TIMEOUT = 4002
    APPROVAL_REQUIRED = 4003
    APPROVAL_DENIED = 4004
    TOOL_NOT_FOUND = 4005
    TOOL_SCOPE_DENIED = 4006
    TOOL_CIRCUIT_OPEN = 4007
    TOOL_CALL_FAILED = 4008
    AGENT_STATE_ILLEGAL = 4009
    INTERNAL = 5000
    UPSTREAM_FAILED = 5001
    MODEL_UNAVAILABLE = 5002


class BusinessError(Exception):
    """业务失败：号段码 + 中文可操作提示，由宿主异常处理器统一转 fail() 信封。

    code 类型是 ``ErrorCode | int``：上游透传的域内码（3xxx）不是内核枚举成员，
    但必须原样带给前端做分支，故允许 int。
    """

    def __init__(self, code: ErrorCode | int, msg: str, http_status: int = 400) -> None:
        super().__init__(msg)
        self.code = int(code)
        self.msg = msg
        self.http_status = http_status


class UpstreamError(Exception):
    """上游依赖故障（连不通 / 超时 / 上游 5xx 信封）。

    与 BusinessError 严格区分：BusinessError 是「业务拒绝」（确定性结果，不重试、不计熔断），
    UpstreamError 是「依赖抖动」（幂等工具应退避重试、并计入熔断），
    混用会让熔断统计把正常业务结果算成故障。
    """

    def __init__(self, msg: str) -> None:
        super().__init__(msg)
        self.msg = msg