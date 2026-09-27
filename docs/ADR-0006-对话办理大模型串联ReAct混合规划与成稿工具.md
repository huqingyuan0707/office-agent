# ADR-0006：对话办理重构——ReAct 混合规划与大模型成稿工具

- 日期：2026-09-26
- 状态：已落地
- 决策人：用户（方案确认）+ AI（实施）
- 关联文档：.trae/documents/对话办理重构-大模型串联三场景.md（实施计划 SSOT）、ADR-0005（LangGraph 图装配基础）、AGENTS.md §6（单轮规划拿不到上一步出参的边界登记）

## 1. 背景与约束

- 现状与问题：对话页文档卡明示「纯模板直出，未调用任何大模型」——用户要的是「一句话串起
  数据分析、文档生成、知识问答」，现状却是模板拼接；且 `LlmFunctionCallPlanner.plan()` 单轮
  一次性提议全部步骤，拿不到上一步真实出参，参数只能靠模型编（AGENTS §6 已登记）。
- 约束：
  - 数值一致率红线不破：数字不经模型手抄，一律占位符回填 / 素材原值 + 独立校验标注；
  - 降级绝不 500、绝不编造：LLM 不可用要有如实出路；
  - checkpoint 契约不漂移（`plan` 条目 `{tool, args}`、断点续跑、审批重放照常工作）；
  - 分层纪律：tools-office 不得反向 import runtime（ADR-0005 既定方向）；
  - 存量 agent.yaml 零回归：不写 `llm_mode` 的智能体行为与从前完全一致。

## 2. 候选方案

| 方案 | 优点 | 缺点 | 成本 |
|---|---|---|---|
| A：全切 ReAct（所有智能体逐步再规划） | 单轮规划缺陷根除 | 规则链智能体（daily-report 等）被拖慢且丢确定性；小模型逐步问更易跑偏 | 高 |
| B：混合（推荐采纳）——关键词命中走规则快路径（链内文档步切大模型成稿）；规则不中的自由目标走 ReAct 逐步再规划 | 快路径保留确定性；自由目标每步看真实出参再决定下一步；两模式声明式切换零回归 | 两条执行路径需各自治理（步数顶/降级分级） | 中 |
| C：维持单轮 plan + 加大 prompt 力度 | 零代码 | 模型仍拿不到执行期真实出参，编参数问题无法根治 | 低 |

## 3. 决策

- 选择：方案 B（混合）。
- 理由（对照约束逐条）：
  - 数字红线：链内数字仍走 `{steps[N].result.路径}` 单步取值；成稿素材走新增
    `{steps[*].result}` 整体注入（全部已完成步骤真实出参），成稿侧 `_check_numbers`
    独立标注失信数字（只标注不拦截，与终答数值校验同口径）；
  - 降级：`office.doc.compose` 在 LLM_PROVIDERS 未配置/出站失败/空文本时降级素材原文
    直出（`degraded=True + degrade_reason`）；react 中途失败按「已有真实产出则带产出
    收敛 DONE、零产出则 FAILED 中文报错」分级；
  - 契约：react 计划增量 append 进 `checkpoint["plan"]`，条目形状不变；断点续跑经
    `_plan_from_checkpoint` 回放，审批挂起重放照常；
  - 分层：成稿工具放 tools-office（`doc_compose.py`，自读 `os.environ` 的
    LLM_PROVIDERS + httpx，仿 retrieval.py 先例），runtime 不被反向依赖；
  - 零回归：`AgentSpec.llm_mode` 缺省 `plan`，plan 模式降级链原样保留。

## 4. 后果

- 正面：
  - 文档产物从模板直出升级为「真实数据 + 大模型行文」；自由目标获得逐步决策能力；
  - `llm_mode` 是声明式字段——任何智能体一行配置切换规划范式；
  - 前端文档卡带来源角标（大模型成稿·模型名 / 模型不可用·素材直出），产物来源透明。
- 负面/风险 + 缓解：
  - react 每步一次 LLM 往返，本地小模型延迟叠加 → max_steps 硬顶 + 观察截断（800 字/步）
    + 300s 冒烟轮询上限；受理期 profile 未配置即报错（不浪费执行轮）；
  - 小模型一次返回多条 tool_calls → 只告警取第一条（ReAct 节奏一步一问）；
  - LLM 成稿数字失信 → `numbers_check.unverified` 清单如实透出，前端可感知。
- 回滚方案：agent.yaml 删 `llm_mode: react` 行即回 plan 模式；规则链文档步改回
  `office.report.generate` 即回模板直出（两机制件互不依赖，可独立回退）。
- 可观测验证：`tests/smoke_compose_chain.py`（5 段 HTTP 冒烟）+ runtime 单测 12 例
  （占位符/llm_mode/react 全链路）+ tools-office 单测 6 例（成稿/降级/数字校验）。

## 5.1 执行链路追踪（对话调试面板，2026-09-27 追加）

- 决策：在 ReAct 混合规划之上加一层「执行链路」可观测——终答提示词允许模型输出
  `<<<inner>>>` 块（内写【Step 思考】/【Step 路由】/【Step 工具调用】，仅管理员可见），
  后端正则剥离 inner 与对外正文；规划走 tool_calls 结构化输出的步骤**不**要求模型自述
  过程——意图/路由/工具/耗时由执行期真实数据组装（比模型自述更可信，且规则快路径零 LLM 也能出链路）。
- 诚实约束：意图置信度与候选打分只写实测可得的——规则命中记 1.0 并注明「规则关键词命中，
  非模型打分」，LLM/react 直定不写分数；候选智能体只标选中/未选中；token 计数未透出不写字段。
- 存储与权限：trace 存 `Task.checkpoint["trace"]` 与 `Task.output["trace"]`（内嵌 JSON，
  不新增表列、无需迁移）；run 概要不带 trace；`GET /runs/{id}` 仅 admin/`*` 透出，
  普通用户响应无该键；超长按 `TRACE_INNER_MAX_CHARS`/`TRACE_RESULT_MAX_CHARS` 截断。
- 前端：对话气泡右下角「展开执行详情」（仅管理员渲染，默认收起）+ `RunTracePanel`
  浅灰折叠面板（意图/思考/路由/工具调用/工具返回/汇总 + 总耗时 + 收起按钮）；
  历史会话经 run_id 拉取，链路随会话一起加载。
- 回滚：前端隐藏按钮即回退（后端 trace 照常落库，不影响任何现有渲染）；
  提示词去掉 inner 说明即回纯正文（解析缺块降级空分段，不阻断收敛）。

## 5. 落地清单

- [x] 机制件 1：`{steps[*].result}` 整体注入占位符（planner/rule.py）
- [x] 机制件 2：`AgentSpec.llm_mode`（plan/react）声明式字段与校验（spec.py）
- [x] 机制件 3：`office.doc.compose` 大模型成稿工具（tools-office/doc_compose.py）
- [x] 机制件 4：`ReACTPlanner.next_step` 逐步再规划器（planner/react.py，继承出站链路）
- [x] 机制件 5：graph/loop_state 边界改造（react 增量计划 + 失败分级收敛）
- [x] 机制件 6：office-assistant / daily-report-assistant 切换 + 前端来源角标
- [x] 测试：单测 18 例 + HTTP 冒烟 smoke_compose_chain.py
- [x] 文档：本 ADR + AGENTS §5/§6 + README 中英 + .env.example 注释
- [x] 执行链路追踪：`trace.py`（inner 解析 + 链路组装 + 管理门控）+ `run_finish.py`
  （收尾逻辑拆分，loop_state 回 400 行内）+ `GET /runs/{id}.trace` 管理透出
  + 前端 `RunTracePanel` 调试面板（§5.1）
- [x] 测试：runtime 单测 10 例（`test_trace.py`）+ HTTP 冒烟 `tests/smoke_trace.py`（16 项断言）
- [x] 文档：本 ADR §5.1 + README 中英对话表执行链路行
