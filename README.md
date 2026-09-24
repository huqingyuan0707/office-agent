# office-agent —— 领域无关的智能办公 Agent 平台

> 把 Agent 落地的四道硬问题——**权限、审批、审计、数据语义**——做成领域无关的治理内核：
> 工具注册中心 → Scope 鉴权执行器（超时 / 熔断 / 幂等）→ 审批闸门（写动作恒送审）→ 全程审计 + `trace_id` 贯穿。
> 内核之上是一层**智能体编排运行时**：用户给一句自然语言目标，planner 规划成一串工具调用逐步执行，
> 产出带溯源的结果。编排层只能「提议」，每一步都走执行器——治理口径绕不过。
> 办公场景与外部系统一律插件化接入，主包零业务域语义。

设计依据：`docs/office-agent仓库骨架与内核提取方案.md`、`docs/ADR-0003-办公Agent拆分独立开源项目.md`；
编排层方案：`.trae/documents/智能体编排层实现方案.md`；AI 协作纪律：`AGENTS.md`。

## 架构总览

```
用户目标（自然语言）
   │
   ▼
packages/runtime ── 智能体编排层（可选装配，RUNTIME_ENABLED=false 即纯地基模式）
   ├─ spec.py + loader.py   AgentSpec 声明式配置（plugins/*/agent.yaml，零代码定制）
   ├─ planner/              LlmFunctionCallPlanner（OpenAI 兼容）→ 降级 RulePlanner（零 LLM 可演示）
   ├─ runner.py             主循环：提议 → 执行 → 观察 → checkpoint；max_steps 硬顶、审批挂起 lazy resume
   └─ validation.py         回答数值校验：数字必须能溯源到工具返回，否则标注失信
   │
   ▼  每步提议 = {tool, args}，只能从（角色可见 ∩ 白名单）里选
packages/core ── 治理内核（冻结，只消费公开 API）
   ├─ registry.py   工具注册中心（ToolSpec + JSON Schema + Scope，重名拒绝）
   ├─ policy.py     Scope 鉴权 / 审批分流 / 幂等键
   ├─ executor.py   超时硬拦 / 熔断 / 全程审计 / trace_id
   ├─ linkage/      跨系统 HTTP 桥接（提供方注册表，凭据只走环境变量）
   └─ audit.py      审计事件缝（壳侧落库，只追加不改）
   │
   ▼
packages/server ── FastAPI 壳（auth / rbac / tasks / approvals / governance）
   + packages/mcp-bridge ── 标准 MCP 协议桥（可选装配：transport="mcp" 的提供方自动发现注册）
   + packages/tools-office ── 内置办公工具（日程/周报/纪要/知识库/OCR/文档对比/任务拆解/模板）
   + plugins/tools-ecommerce ── 外部系统只读桥接工具（可选，未配置提供方即不注册）
   + apps/web ── Vue3 管理台（登录 / 工具 / 任务 / 审批 / 治理 / 智能体运行面板）
```

## 仓库结构

```
office-agent/
├── packages/
│   ├── core/            office_agent_core：契约 / 注册中心 / 策略 / 执行器 / 联动 / 审计
│   ├── server/          office_agent_server：FastAPI 壳（/api/v1，统一信封，可选静态托管）
│   ├── runtime/         office_agent_runtime：智能体编排运行时（mount(app) 可选装配）
│   ├── mcp-bridge/      office_agent_mcp_bridge：标准 MCP 协议桥（本侧为 Client，JSON-RPC over HTTP）
│   └── tools-office/    office_agent_tools_office：内置办公工具包
├── plugins/
│   ├── tools-ecommerce/     电商只读桥接工具（order/logistics/stock/coupon/kb，远程工具示例）
│   ├── daily-report-assistant/agent.yaml   办公域示例智能体（规则规划 + 送审挂起演示）
│   └── ecommerce-assistant/agent.yaml      外部工具接入示例（白名单接远程工具，零代码）
├── apps/web/            Vue3 + TS + Vite 管理台（六视图路由化）
├── alembic/             数据库迁移（持久库 schema 演进唯一入口）
├── office_agent/        早期 M0/M1 单包演示（tests/smoke_*.py 对其 8201 端口做 HTTP 冒烟）
├── tests/               HTTP 级冒烟脚本
├── docs/                ADR 与设计方案
└── skills/ githooks/    质量门禁脚本与提交钩子
```

## 五分钟启动

要求 Python 3.11+（core 使用 `StrEnum`，3.10 跑不了 packages 测试）。

```powershell
# 1. 依赖 + 五个包（可编辑安装）
python -m venv .venv
.venv\Scripts\pip.exe install -r requirements.txt
.venv\Scripts\pip.exe install -e packages/core -e packages/server -e packages/runtime -e packages/tools-office -e packages/mcp-bridge

# 2. 配置（全部零硬编码，键的唯一出处 packages/core/office_agent_core/settings.py）
copy .env.example .env

# 3. 建库 / 迁移（存量库 schema 演进唯一入口；新库也可直接由启动建表）
.venv\Scripts\alembic.exe upgrade head

# 4. 启动后端（默认 127.0.0.1:8200；自动挂载 /api/v1/agents 与 /api/v1/runs）
.venv\Scripts\python.exe -m office_agent_server
```

前端管理台：

```powershell
cd apps\web
npm install
npm run dev        # http://127.0.0.1:5173，登录页可改后端地址
```

演示账号（`SEED_ON_START=true` 时自动种子，生产必关）：

| 账号 | 口令 | 角色 | 用途 |
|---|---|---|---|
| admin | admin123 | `*`（全 Scope） | 调工具、发起运行 |
| reviewer | reviewer123 | admin + approver | 复核审批（审批人 ≠ 提交人红线） |

## 五分钟体验（无 LLM 即可端到端）

```powershell
# 1. 登录拿 token
curl.exe -s -X POST http://127.0.0.1:8200/api/v1/auth/login -H "Content-Type: application/json" -d '{\"username\":\"admin\",\"password\":\"admin123\"}'

# 2. 发起一次运行：日报助手按 agent.yaml 规则规划两步（schedule.view → report.generate）
curl.exe -s -X POST http://127.0.0.1:8200/api/v1/runs -H "Authorization: Bearer <token>" -H "Content-Type: application/json" -d '{\"agent\":\"daily-report-assistant\",\"goal\":\"生成今天的工作日报\"}'

# 3. 查看运行详情：步骤时间线（每步 tool/args/结果摘要/trace_id/planner_source）
curl.exe -s http://127.0.0.1:8200/api/v1/runs/<run_id> -H "Authorization: Bearer <token>"

# 4. 治理演示：目标换成「帮我建个待办」→ todo.create 恒送审 → run 挂起 WAITING_APPROVAL
#    用 reviewer 登录批准 → 再查 run 即自动续跑（lazy resume），时间线留完整出参与审计
```

## API 一览

统一信封 `{code, msg, data, trace_id}`，`code == 0` 为成功；鉴权 `Authorization: Bearer <JWT>`。

| 方法 | 路径（前缀 /api/v1） | 说明 |
|---|---|---|
| POST | /auth/login | 登录换 JWT（防账号探测：不存在与密码错同一提示） |
| GET | /agent/tools | 当前角色 Scope 可见的工具清单（含 JSON Schema） |
| GET | /agent/tools/{name} | 单个工具详情 |
| POST | /agent/tools/{name}/invoke | 调用工具；需审批工具只落审批单返回 pending |
| GET | /tasks | 任务执行记录（含 agent.run） |
| GET | /approvals | 审批单列表 |
| POST | /approvals/{id}/approve | 批准（审批人 ≠ 提交人，同人拒绝） |
| POST | /approvals/{id}/reject | 驳回（附意见，run 收敛终态留痕） |
| GET | /notifications | 本人站内通知（登录即可读自己的，unread_only 过滤） |
| POST | /notifications/scan | 扫描生成通知（审批超时/任务失败/今日简报；admin/approver，幂等可重扫） |
| POST | /notifications/{id}/read | 标记已读（幂等回执） |
| GET | /governance/status | 治理状态（工具数 / 熔断 / 可观测计数） |
| GET | /agents | 可用智能体清单（实扫 plugins/*/agent.yaml，热更新零重启） |
| POST | /runs | `{agent, goal}` 发起运行（受理即跑，错误进时间线不抛 500） |
| GET | /runs/{id} | 运行状态 + 步骤时间线（审批挂起态内联裁决，批准即续跑） |
| POST | /runs/{id}/resume | 显式断点续跑（checkpoint 回放原计划） |
| GET | /health、/ready | 存活 / 就绪探针（无鉴权） |

错误号段：`1xxx` 通用（1001 参数 / 1002 未认证 / 1003 无权 / 1004 不存在 / 1005 超限）、`2xxx` 模型（2000 LLM 失败）、`4xxx` 任务与工具（4003 需审批 / 4004 被驳回 / 4005 工具不存在 / 4006 Scope 不足 / 4007 熔断打开 / 4008 执行失败）、`5xxx` 系统（5000 内部 / 5001 上游失败）。

## 定制化智能体 = 一份 YAML

`plugins/*/agent.yaml` 就是「定制化」的全部——prompt、工具白名单、步数上限、规划规则，主包零 prompt、零业务词：

```yaml
name: daily-report-assistant
tools: [office.schedule.view, office.report.generate, office.todo.create]  # registry 之外的名字直接拒
max_steps: 6
llm: ""                # 留空走规则规划；配 LLM_PROVIDERS 后可设 profile 名走函数调用规划
rules:                 # 无 LLM 时的规则规划（也是 LLM 故障降级路径）
  - match: ["日报", "今天做了什么"]
    steps:
      - tool: office.schedule.view
        args: {view_type: daily}
      - tool: office.report.generate
        args: {title: 工作日报, metrics: {日程天数: "{steps[0].result.count}"}}  # 从上一步结果取值
```

- **降级链**：LLM 未配置 / 调用失败 / 未返回 tool_calls → 有 `rules` 走规则规划，没有则中文可操作报错，绝不静默编造。
- **白名单交集**：planner 拿到的候选 = AgentSpec 白名单 ∩ 当前角色 Scope 可见集，越权提议直接拒。
- **外部工具接入零代码**：`plugins/ecommerce-assistant/agent.yaml` 白名单直接写远程桥接工具名（`order.query` 等），与本地工具同一治理口径。

## 跨系统数据联动（可选插件，默认缺席）

外部系统能力经 `plugins/tools-ecommerce` 白名单工具接入，纪律（唯一出处：仓库骨架方案 §7）：

- **数据不出域**：只走 HTTP + 服务账号 Bearer token，绝不直连对方数据库；对端把本系统当普通登录用户（`X-On-Behalf-Of` 透传发起人身份）。
- **配置**：`.env` 中 `LINKAGE_PROVIDERS={"ecommerce":{"base_url":"http://127.0.0.1:8000","token":"<服务账号令牌>"}}`；未配置或对方离线 → 远程工具不注册（清单自然缺席），绝不降级成假数据。
- **溯源**：拉取结果必带 `source_endpoint + fetched_at`；日报等生成物每个数字带 `(来源: input.xxx)` 标注。
- **写动作恒送审**：落库生效的写工具一律过审批闸门，`idem_key` 幂等键透传对端防双单；`trace_id` 跨系统贯穿排障。
- **凭据只走环境变量/secret，绝不入库**；主包 `packages/` 禁止出现任何业务域词元（CI grep 门禁）。
- **拉取（模式①）+ 回流（模式②）均已实测**：读工具（order/logistics/stock/coupon/kb）拉取带溯源；首个回流写工具 `ticket.create`（ticket:write）——invoke 恒送审，复核员批准后以申请人身份出站执行，对端按 `(tenant, idem_key)` 唯一约束幂等回放（同键重放返回原单绝不双单）；HTTP 级冒烟 `tests/smoke_linkage_writeback.py` 6/6。

### M3：标准 MCP 协议桥（可选，装上即生效）

对端从「自定义 HTTP 网关」升级为**标准 MCP Server** 时，本侧只改一处配置，业务代码一行不改：

- **配置分流**：`LINKAGE_PROVIDERS` 条目加 `transport`（默认 `"http"` 归内核认领，`"mcp"` 归 mcp-bridge 认领）——
  内核不出现任何协议分支，协议细节不外泄进主包。示例见 `.env.example`。
- **实现**：本侧为 MCP **Client**，手写 JSON-RPC 2.0 over HTTP（协议版本 `2025-06-18`，方法
  `initialize` / `tools/list` / `tools/call`），不引 MCP SDK；`MCPProvider` 与 HTTP 网关客户端
  **同形**（`provider_id` + `invoke()` + `aclose()`），经 `linkage.register_provider()` 登记 →
  registry / executor / 审批 / 审计 / 溯源链路**零改动**。
- **工具自动注册**：启动时 `tools/list` 发现 → 按 MCP 注解映射治理属性：
  `readOnlyHint=true` → 只读 Scope + 免审批 + 幂等；**注解缺失或非只读 → 恒送审**（未知即从严）。
- **降级口径**：对端离线只告警不注册，不阻断启动；错误分级与 HTTP 网关共用同一份口径
  （`core/linkage/envelope.py`：上游 1xxx/3xxx/4xxx 原码透传，5xxx/非法码按依赖故障）。

### IM 审批通知出站（可选，留空即缺席）

审批单产生 / 超时扫描命中新单时推送到群机器人（`generic` / `feishu` / `dingtalk` / `wecom` 四种消息体 + 飞书/钉钉加签）：

- 挂在「审批落单」与「超时扫描」两处，是**旁路**——不进 linkage、不计熔断、不改审批状态；
- 未配置 `IM_WEBHOOK_URL` 直接返回 `{"sent": false, "reason": "not_configured"}`，**不发任何网络请求**；
- 推送失败/超时全 catch 只降级记录（`agent.im_webhook` 事件），**绝不阻断审批流**；超时催办聚合前 N 单防刷屏。

HTTP 级冒烟：`python tests/smoke_mcp_im.py`（自带假 MCP Server + 假 IM 对端，5 项断言）。

## V1.0 办公功能（PRD §5.1，全部走同一治理口径）

| 功能 | 工具 / 端点 | 口径要点 |
|---|---|---|
| 文案生成 | `office.report.generate`（daily/weekly）、`office.minutes.generate` | 模板直出；数值只取入参原值，缺板块留白不编造 |
| 知识库问答 | `kb.ask`（KB_DIR `*.md/*.txt` + 内置演示条目） | 只摘录命中原文片段，无命中 degraded 如实告知 |
| 图片 OCR | `ocr.image` | 元数据直读；Tesseract 缺失降级只回元数据，绝不编造文字 |
| 文档对比 | `office.doc.compare` | 段落级 diff（新增/删除/修改 + 摘要），只报实测差异 |
| 任务拆解 | `office.task.decompose` → `office.task.commit` | 缺人/缺期留空不臆造（missing_info 追问）；批量建单恒送审 + idem_key |
| 自定义模板 | `office.template.save`（送审落盘）→ `office.template.apply` | 占位符缺值保持原样并在 unfilled 如实列出 |
| 主动消息推送 | `/notifications`（scan / 列表 / 已读） | 审批超时/任务失败/今日简报三类信号，`(tenant, username, kind, ref_id)` 唯一去重 |

HTTP 级冒烟：`python tests/smoke_v1_features.py`（17 项断言，可重复执行）。

## V1.1 办公功能（PRD §5.2，同为读口径免审 / 写口径送审）

| 功能 | 工具 | 口径要点 |
|---|---|---|
| 数据自助分析 | `office.data.query` / `analyze` / `export` | 演示台账 + CSV 叠加；统计/趋势/异常只报实测，文本导出不写盘 |
| 会议协作 | `office.meeting.agenda` / `book` / `risks` | 议程模板直出；预约写口径恒送审；风险关键词预警无命中不编造 |
| 审批智能辅助 | `office.approval.draft` / `check`、`office.invoice.extract` | 五类单草稿必填校验追问；金额分级（>1000 部门负责人 / >5000 分管副总）+ 高危二次确认 need_confirm；发票四要素只摘录不推断，无命中 degraded |

## V1.2 办公功能·批次 A（PRD §5.3，工具侧三件）

| 功能 | 工具 | 口径要点 |
|---|---|---|
| PPT 生成 | `office.pptx.generate` | 大纲（每页标题+要点）直出 .pptx，不调大模型；写口径恒送审 + idem_key，python-pptx 缺失不注册；文件锁 DOCS_DIR 防穿越 |
| 合规风险检测 | `office.compliance.scan` | 隐私信息（手机号/身份证/银行卡）、绝对化用语、疑似泄密凭据三类确定性规则；只摘录命中片段与位置不推断（凭据值脱敏）；读口径免审 |
| 预算查询 | `office.budget.query` | 演示台账 + CSV 叠加；剩余额度/使用率为确定性计算并注明口径，附充足/紧张/超支状态；读口径免审，带 source+fetched_at 溯源 |

HTTP 级冒烟：`python tests/smoke_v1_2_batch_a.py`（9 项断言，可重复执行）。

## V1.2 办公功能·批次 B（PRD §5.3，前端两页：真实接口零 mock）

| 功能 | 入口 | 口径要点 |
|---|---|---|
| 数据可视化报表 | `/reports`（报表页） | 数据集四选一 → `office.data.query` 真查（列取自首行键不预设）+ 数值列 CSS 条形图（不引图表库）+ `office.data.analyze` 统计卡 + `office.data.export` markdown 预览与下载；查询失败置空，分析/导出失败本页红条绝不拿假数据顶 |
| 管理员运营看板 | `/admin`（管理页）+ `GET /admin/overview`（仅 admin） | `services/admin_stats.py` 四表只读聚合（用户分状态 / 任务分状态 / 审批分状态 / 工具调用 Top8 / 最近已决 5 单）；非 admin 403 如实提示权限不足，不降级给假数据 |

## 数据库迁移

`alembic` 是持久库 schema 演进唯一入口：async 引擎 `env.py`，URL 唯一出处 `Settings.DATABASE_URL`。
模型改列必须 `autogenerate` 迁移（`create_all` 不做 ALTER）；CI 用 `alembic check` 做漂移自检。

## 质量门禁（本地与 CI 同一口径）

```powershell
.venv\Scripts\ruff.exe check .                                   # lint（ruff 钉 0.16.7，githooks pre-commit 同步校验）
.venv\Scripts\python.exe -m pytest packages/core packages/server packages/runtime packages/tools-office packages/mcp-bridge -q
python skills/naming-check/scripts/check_naming.py               # 命名质量（棘轮：存量只准减）
python skills/anti-shit-code/scripts/check_arch.py               # 架构健康（分层/体量）
.venv\Scripts\alembic.exe check                                  # 模型与库零漂移
Get-ChildItem packages -Recurse -Filter *.py | Select-String -Pattern "finance|purchase|risk|sku|订单|电商"  # 领域无关 grep 门禁（仅 plugins/ 与 docs 允许）
cd apps\web; npm run build                                       # 前端构建门禁（含 vue-tsc）
```

克隆仓库后执行一次 `git config core.hooksPath githooks` 启用提交钩子。

## 里程碑状态

| 阶段 | 范围 | 状态 |
|---|---|---|
| M0 | 仓库骨架 + 内核提取（core/server） | ✅ |
| M1 | 审批闭环 + 办公工具 + 前端管理台 | ✅ |
| M2 | 跨系统 HTTP+JWT 桥接（tools-ecommerce，拉取模式实测） | ✅ |
| R0-R3 | 编排运行时：AgentSpec / 双 planner / 审批挂起续跑 / 数值校验 / 运行面板 | ✅ |
| V1.0 | PRD §5.1 七项办公功能（周报/纪要/知识库/OCR/对比/拆解/模板）+ 站内通知 | ✅ |
| M3 | mcp-bridge 标准 MCP 协议桥（transport 分流 + tools/list 自动注册）+ IM 审批通知出站 | ✅ |

## 文档

- 决策记录：`docs/ADR-0003-办公Agent拆分独立开源项目.md`、`docs/ADR-0004-MCP协议桥与IM审批通知出站.md`
- 骨架与内核提取：`docs/office-agent仓库骨架与内核提取方案.md`
- 编排层方案：`.trae/documents/智能体编排层实现方案.md`
- 贡献：`CONTRIBUTING.md`（PR 检查清单）；AI 协作纪律：`AGENTS.md`

## License

[Apache-2.0](LICENSE)
