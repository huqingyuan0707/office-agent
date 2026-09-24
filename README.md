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
   + packages/tools-office ── 内置办公工具（schedule.view / report.generate / todo.create）
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
# 1. 依赖 + 四个包（可编辑安装）
python -m venv .venv
.venv\Scripts\pip.exe install -r requirements.txt
.venv\Scripts\pip.exe install -e packages/core -e packages/server -e packages/runtime -e packages/tools-office

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

## 数据库迁移

`alembic` 是持久库 schema 演进唯一入口：async 引擎 `env.py`，URL 唯一出处 `Settings.DATABASE_URL`。
模型改列必须 `autogenerate` 迁移（`create_all` 不做 ALTER）；CI 用 `alembic check` 做漂移自检。

## 质量门禁（本地与 CI 同一口径）

```powershell
.venv\Scripts\ruff.exe check .                                   # lint（ruff 钉 0.16.7，githooks pre-commit 同步校验）
.venv\Scripts\python.exe -m pytest packages/core packages/server packages/runtime packages/tools-office -q
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
| M3 | mcp-bridge 标准 MCP 协议 + SPI 文档 | ⏳ 规划中 |

## 文档

- 决策记录：`docs/ADR-0003-办公Agent拆分独立开源项目.md`
- 骨架与内核提取：`docs/office-agent仓库骨架与内核提取方案.md`
- 编排层方案：`.trae/documents/智能体编排层实现方案.md`
- 贡献：`CONTRIBUTING.md`（PR 检查清单）；AI 协作纪律：`AGENTS.md`

## License

[Apache-2.0](LICENSE)
