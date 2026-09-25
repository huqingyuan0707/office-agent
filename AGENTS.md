# AGENTS.md — office-agent 项目执行入口（人 + AI 都认它）

> 领域无关的智能办公 Agent 平台。决策背景见 `docs/ADR-0003-办公Agent拆分独立开源项目.md`，
> 仓库结构与内核提取见 `docs/office-agent仓库骨架与内核提取方案.md`，ADR 体例见 `docs/ADR规范与模板.md`。

## 1. 文档索引（Single Source of Truth）

| 改什么 | 先读什么 |
| --- | --- |
| 决策/回滚/红线出处 | `docs/ADR-0003-办公Agent拆分独立开源项目.md` |
| 仓库结构/包边界/里程碑 | `docs/office-agent仓库骨架与内核提取方案.md` |
| 产品需求/任务拆解能力口径 | `智能办公Agent 产品需求文档.md`（§6 任务拆解能力详述） |
| 新增 ADR | `docs/ADR规范与模板.md`（选型变更先写 ADR 再动代码） |
| 前后端统一约束（入口） | `skills/fullstack-code-rule/SKILL.md` |
| 后端风格 | `skills/backend-code-style/SKILL.md` |
| 前端风格 | `skills/frontend-code-style/SKILL.md` |
| 命名质量 | `skills/naming-check/SKILL.md` |
| 架构健康 | `skills/anti-shit-code/SKILL.md` |
| 后端功能开发 | `skills/doubao-coding-develop-backend-features/SKILL.md` |
| 前端功能开发 | `skills/doubao-coding-develop-frontend-features/SKILL.md` |
| Bug 诊断修复 | `skills/doubao-coding-diagnose-and-fix-bugs/SKILL.md` |
| 代码审查 | `skills/doubao-coding-review-code/SKILL.md` |
| 单元测试 | `skills/doubao-coding-develop-unit-tests/SKILL.md` |
| 性能优化 | `skills/doubao-coding-optimize-performance/SKILL.md` |
| 安全威胁评审（STRIDE） | `skills/security-threat-review/SKILL.md` |
| SQL 诊断与最小修复 | `skills/sql-diagnose-refine/SKILL.md` |
| GitHub 远程操作 | `skills/github-remote/SKILL.md` |
| 应用/工程构建（全栈） | `skills/doubao-app-builder/SKILL.md` |

## 2. AI 工作流（强制）

1. 动代码前先读 §1 对应文档，不凭记忆写。
2. 回复开头声明一行：`对齐文档：<文件名> §<节> + Skill §<节>`。
3. 新文件必须写中文文件头 docstring（职责 + 链路 + 对齐章节）。
4. 改接口必须同步改 docs 里对应设计文档节；完成功能同步更新 README 的功能清单。
5. 贴验证命令输出，不说「应该过了」。
6. 完成一个任务/里程碑并验证通过后：自动总结本次修改内容（改了什么 + 验证输出），commit（走 githooks 校验）并 push 到 GitHub。

## 3. 后端红线（FastAPI）

- 分层：端点薄封装（解析 → 调服务 → `ok()/fail()`），业务进 services/纯函数，不依赖 FastAPI 对象。
- 信封：成功 `ok(data, msg)`、失败 `fail(code, 可操作 msg)`；错误号段：1xxx 通用 / 2xxx 对话 / 3xxx 业务 / 4xxx 任务 / 5xxx 系统。
- 工具注册：一律走 registry（ToolSpec + JSON Schema + Scope + 幂等键 + 超时熔断），禁止散落直调。
- 写动作恒送审：所有落库生效的写工具必须过审批闸门，幂等键 `idem_key` 必带。
- 降级绝不 500：外部依赖不可用走降级路径并置 `degraded` 标记。
- 审计：工具调用/审批/登录全记审计，只追加不改。
- 数据不出域：不直连外部项目数据库，一切经白名单工具拉取，返回必带 `source + fetched_at` 溯源。
- 可选插件：核心包禁止 import 任何具体业务域（电商/HR/财务）——领域能力一律插件包。

## 4. 前端红线（Vue3 + TS + Vite）

- `<script setup lang="ts">`，页面方法一律箭头函数；构建门禁 `npm run build`（含 vue-tsc）。
- 一切数据来自后端真实接口，禁止 mock/硬编码兜底；失败置空 + 报错提示。
- 401 走中央处理，禁页面自跳；样式用 `var(--*)` token + scoped。

## 5. 验证命令

```bash
# 后端（仓库根；venv 为 py3.11——office_agent_core 用 StrEnum，3.10 跑不了 packages 测试）
.venv\Scripts\ruff.exe check .
.venv\Scripts\python.exe -m pytest packages/core packages/server packages/runtime packages/tools-office packages/mcp-bridge -q
python skills/naming-check/scripts/check_naming.py   # 命名质量门禁（棘轮：存量只准减不准增）
python skills/anti-shit-code/scripts/check_arch.py   # 架构健康门禁（分层/体量，10 处 baseline 债务）
# 迁移（持久库 schema 演进唯一入口；模型改列必须 autogenerate 迁移，create_all 不做 ALTER）
.venv\Scripts\alembic.exe upgrade head               # 应用迁移（新库直接建全）
.venv\Scripts\alembic.exe check                      # 漂移自检（CI 用：模型与库不一致即 FAILED）
# HTTP 级冒烟（先启动服务：.venv\Scripts\python.exe -m office_agent_server）
.venv\Scripts\python.exe tests\smoke_v1_features.py  # V1.0 七项功能 17 断言（可重复执行）
.venv\Scripts\python.exe tests\smoke_mcp_im.py       # M3 协议桥 + IM 通知 5 断言（自带假对端，无需预启服务）
.venv\Scripts\python.exe tests\smoke_v1_2_batch_a.py # V1.2 批次 A：PPT 生成/合规检测/预算查询 9 断言（可重复执行）
.venv\Scripts\python.exe tests\smoke_personal_affairs.py # §2.2 个人事务：待办/日程/台账/主动推送 8 断言（可重复执行）
.venv\Scripts\python.exe tests\smoke_file_ask.py      # §2.1 文件解析（PDF 真实抽取）+ 文件内容问答 7 断言（可重复执行）
# 前端
cd apps/web && npm run build
```

## 6. 待办登记

- [x] `naming-check` / `anti-shit-code` 脚本本体适配：扫描根改 `office_agent`+`packages`、前端 `apps/web/src`、ratchet baseline 初始化（naming 0 债务；arch 12 处 endpoint-db-op 入基线，触碰对应文件时优先抽 service 层偿还）。
- [x] 提交钩子引入：`git config core.hooksPath githooks` 统一启用（克隆后执行一次）——`commit-msg`（type 白名单/sentence-case/行长 ≤100；电商侧 R1 一致性校验未引入本仓库）+ `pre-commit`（ruff==0.16.7，只校验暂存文件并回暂存）；`.pre-commit-config.yaml` 留作 CI/手动 `pre-commit run` 用。
- [x] ruff 配置落位：根 `ruff.toml` + 三子包各自 pyproject.toml（嵌套配置，根配置管不到 packages 内）；B904 修复 4 处、DTZ005 修复 1 处（显式 UTC）、security.py 统一 python-jose（消除 PyJWT 双依赖）。
- [x] M1 审批闭环：`REVIEWER_USERNAME/PASSWORD` 复核员双账号 + `office.memo.submit` 需审批写工具（office:write）+ `tests/smoke_approval.py` HTTP 级实测——invoke 落单 / 同人 1001 红线 / 复核员批准执行 / 任务留痕 / 驳回流，8/8 passed。
- [x] 办公文档工具包 `office_agent/tools_docs.py`：office.docx/xlsx.read（office:read 免审批，带 source+extracted_at 溯源）+ office.docx/pptx.write（office:write 需审批，审批通过才写盘）——依赖缺失自动降级不注册，读写锁 DOCS_DIR 防路径穿越；`tests/smoke_docs.py` HTTP 实测 8/8（含穿越拒绝与驳回流）。
- [x] alembic 引入：仓库根 `alembic.ini + alembic/`（async 引擎 env.py，URL 唯一出处 Settings.DATABASE_URL；基线 `init schema` 全表全列）；dev 库已 stamp，`alembic check` 零漂移。模型改列一律 autogenerate 迁移，禁止再手改库。
- [x] `runner.execute_run` C901 豁免偿还（2026-09-24）：循环执行态与私有助手整体迁入 `packages/runtime/office_agent_runtime/runloop.py`（RunLoop 类：prepare_plan / run_step / run / finish），runner.py 只留受理入口与薄装配；execute_run 签名保持（resolve_pending 注入契约）。根 ruff.toml 与 runtime pyproject 两处 C901 per-file-ignore 已删，全文件最高复杂度 7；HTTP 级 E2E 复验通过（日报直出 / 待办挂起→批准→续跑）。
- [x] 测试基线 core 14 / server 16 / runtime 25 = 55 passed（tools-office 无独立测试，行为由 runtime 冒烟覆盖；server +1 为直批执行路径回归锁定）。
- [x] README 重写：中英双版覆盖 packages 四包 + runtime 编排层 + plugins 示例 + 前端 + alembic + 启动/体验/门禁/里程碑（2026-09-24，替换 M0-M1 旧骨架描述）。
- [x] 产品文档合并（2026-09-24）：`智能办公Agent - 任务拆解.md` 整体收编进 `智能办公Agent 产品需求文档.md`——原文 §一~§五 → §6 任务拆解能力详述（用户操作流程）、原文 §六 精简需求 → §2.9 条目，新增 §3.7 拆解场景与 §4.1 交叉引用；源文件删除，产品口径只留单一 PRD 为 SSOT。
- [x] 联动模式②回流落地（2026-09-24）：首个回流写工具 `ticket.create`（ticket:write，恒送审 + idem_key 必填）——复核员批准后以申请人真实角色出站执行（顺带修复 decide_approval roles=[] 直批 4006 断点），电商侧按 `(tenant, idem_key)` 唯一约束幂等回放绝不双单（迁移 a897b11ead1b，电商仓 4364956）；`tests/smoke_linkage_writeback.py` HTTP 级 6/6（同键重放 replayed=True 同一单，EC 库单据数=1）。
- [x] M3 协议桥 + IM 审批通知落地（2026-09-24，ADR-0004）：①`packages/mcp-bridge`（`office_agent_mcp_bridge`）——本侧为 MCP **Client**，手写 JSON-RPC 2.0 over HTTP（协议 `2025-06-18`，方法 initialize/tools/list/tools/call，不引 MCP SDK），`MCPProvider` 与 HTTP 网关客户端**同形**（provider_id + invoke + aclose）经 `linkage.register_provider()` 登记 → registry/executor/审批/审计/溯源**零改动**（「换协议不改业务代码」）；配置经 `LINKAGE_PROVIDERS[].transport` 分流（内核 `OWN_TRANSPORT="http"` 只认领自己的，主包无协议分支）；启动期 `tools/list` 自动发现 → 注解 `readOnlyHint=true` 只读免审，**注解缺失/非只读恒送审**（未知即从严），对端离线只告警不注册；错误分级口径抽到 `core/linkage/envelope.py`（上游 1xxx/3xxx/4xxx 原码透传，5xxx/非法码按依赖故障）由 HTTP 网关与 MCP 桥共用。②IM 审批通知 `services/im_notifier.py`（旁路：不进 linkage/不计熔断/不改审批状态；generic/feishu/dingtalk/wecom 四消息体 + 飞书/钉钉加签；未配置直接返回 `not_configured` 不发网络；全 catch 降级记 `agent.im_webhook`），挂「审批落单」与「超时扫描命中新单」两处（聚合前 N 单防刷屏、二次扫描不重复）。HTTP 级冒烟 `tests/smoke_mcp_im.py` 5/5（自带假 MCP Server + 假 IM 对端）；测试基线 core 15 / server 33 / runtime 25 / tools-office 28 / mcp-bridge 23 = **124 passed**；文档同步 `.env.example` / README 中英 / 方案文档 §5§7.4 / 本文件 §5。
- [x] V1.1 办公功能三件套（PRD §5.2，2026-09-24）：①数据自助分析 `office.data.query/analyze/export`（提交 24e8332）②会议协作 `office.meeting.agenda/book/risks`（提交 a8f197e）③审批智能辅助 `office.approval.draft/check` + `office.invoice.extract`（approval.py，13 例单测）——五类常用单（请假/报销/采购/加班/出差）草稿必填校验追问 missing_fields；金额分级提示（>1000 部门负责人 / >5000 分管副总）+ 高危二次确认 need_confirm；发票四要素（金额/号码/日期/销售方）正则只摘录不推断、无命中 degraded；PRD §2.3 催办复用 notifications 扫描链不重复造。HTTP 级实测 8200 服务注册链（draft ready / check 6800 触发 need_confirm / invoice 提取 3200.5）；README 中英补 V1.1 功能表（前两项欠账一并登记）；tools-office 基线 28 → **61 passed**（新增 data_analysis 10 / meeting 13 / approval 13 等聚合）。
- [x] V1.2 批次 A 工具侧三件（PRD §5.3，2026-09-25）：①PPT 生成 `office.pptx.generate`（pptx_gen.py，office:write 恒送审+idem_key，大纲每页标题+要点直出 .pptx 不调大模型，python-pptx 缺失不注册，文件名锁 DOCS_DIR 防穿越）②合规风险检测 `office.compliance.scan`（compliance.py，office:read——隐私信息手机号/身份证/银行卡 + 绝对化用语词表 + 疑似泄密凭据三类确定性规则，只摘录命中片段与位置不推断、凭据值脱敏防二次泄露，身份证/银行卡共区间去重）③预算查询 `office.budget.query`（budget.py，office:read——演示台账 + DOCS_DIR/data/budget.csv 叠加复用 data_analysis 装载器，remaining/usage_pct 确定性计算注明口径，充足/紧张/超支状态标签，source+fetched_at 溯源）。测试基线 tools-office 61 → **81 passed**（新增 20 例），五包全量 **177 passed**；HTTP 冒烟 `tests/smoke_v1_2_batch_a.py` 双跑 9/9（含 pptx 恒送审→批准写盘可读 + 穿越「落单→批准→执行期 1001 驳回流」）；README 中英 V1.2 批次 A 功能表 + 本文件 §5 冒烟命令登记。
- [x] V1.2 批次 B 前端两页（PRD §5.3，2026-09-25）：①后端 `GET /admin/overview`（api/admin.py，仅 admin，薄封装 → `services/admin_stats.py` 纯函数四表只读聚合：用户分状态/任务分状态/审批分状态/工具调用 Top8/最近已决 5 单；server 单测 3 例：admin 200 五段齐备/viewer 403/无 token 401）②报表页 `apps/web/src/views/ReportsView.vue`（路由 `/reports`：数据集四选 → `office.data.query` 真查表格 + 数值列 CSS 条形图不引库 + `office.data.analyze` 统计卡 + `office.data.export` 预览与 Blob 下载）③管理页 `AdminView.vue`（路由 `/admin`：分卡渲染聚合，403 壳红条如实提示）。验证：8210 真实服务 E2E 4/4（overview 五段/users.total=2/query sales=4/analyze sum=360/budget rows=5）+ `npm run build` 通过（含 vue-tsc，两新页分包产出）；README 中英批次 B 功能表 + 本条登记。
- [x] V1.2 批次 C 工作流编排后端（PRD §5.3/§2.11，2026-09-25）：`Workflow` 模型 + 迁移 `46bf9fe408ba`（`alembic check` 零漂移）+ `services/workflows.py`（CRUD/parse+validate 双重校验/run 顺序执行：写步落单即停 pending_approval/失败即停，已完成步骤如实返回）+ `api/workflows.py` 六端点薄封装（server 单测 5 例）；远程工具步骤同链出站即 RPA 形态。HTTP 冒烟 `tests/smoke_v1_2_batch_c.py` 7/7（自带服务：建链/run ok/落单即停/批准执行/脏编排拒收/删除 404）；README 中英批次 C 功能表 + 本条登记。注：前端编排页 `WorkflowsView.vue` + 路由/导航已写完但暂缓提交——工作区撞见并发改动（`api.ts` AgentList/run_id 契约变更 + `runtime/api.py` + `router.py`，AgentsView 未跟进致 `npm run build` 红 6 错全在其文件，我方文件 vue-tsc 零错误），待对方落盘后再合前端单。
- [x] 对话主入口 + 一句话自动路由（2026-09-25）：①后端 `runtime/router.py` `route_agent_spec`——`POST /runs` 的 `agent` 改可选，省略即跨智能体路由：规则命中优先（与执行期 RulePlanner 同口径，路由选中即规划必成）、LLM 智能体兜底仅当 profile 已在 `LLM_PROVIDERS` 配置（`profile_configured` 零网络查，未配置不接免白等一轮执行失败）、全不中 1001 中文列出已装载智能体与能力描述；runtime 单测 6 例（规则优先/LLM 兜底/未配置不兜底/全不中报错/HTTP 自动路由跑通/1001）。②前端：默认页改 `/chat` 对话办理（ChatView.vue）——一句话发起 → createRunAuto → 2s 轮询气泡内呈现路由结果/步骤时间线/终答或末步结果/审批挂起卡，路由失败原文进气泡零 mock；顶栏「对话」列首位，工具页降级为管理员调试台。③顺手偿还前端契约漂移：`AgentItem.id→name`（+llm/rule_count）、`listAgents` 认 `{total,items}`、`createRun` 收 `{agent,goal}`、`RunItem.id→run_id`（+answer/validation/status_label/pending_approval 等），AgentsView 六错清零。验证：五包 pytest **218 passed**（含 router 6 例：规则优先/LLM 兜底/未配置不兜底/全不中报错/HTTP 自动路由跑通/1001）、ruff/naming/arch/alembic check 全绿、`npm run build` 通过；HTTP 实测路由两分支（日报→daily-report-assistant DONE / 订机票→1001 列智能体）；浏览器 E2E 对话页双场景 PASS。共享文件（api.ts/router/index.ts/App.vue）按「剥离并发方未提交行→提交→恢复」外科手术处理，不混入对方工作流前端单。
- [x] 前端 Element Plus + 模块化 + 侧边菜单壳（2026-09-25）：①EP 按需引入（unplugin-auto-import + unplugin-vue-components 的 ElementPlusResolver，dts 落 `src/types/` 随提交保证干净检出 vue-tsc 可跑，禁全局全量）。②菜单即路由：`router/types.ts` + `router/modules/{office,process,platform,operation}.ts` + `router/menu.ts`（MENU_GROUPS 聚合派生 menuRoutes 与顶栏标题回查），新增页面只改对应域模块；App.vue 瘦身纯路由出口。③`layouts/MainLayout.vue`：EP el-container/el-aside/el-menu 侧边分组菜单壳（可收起、shellError 红条与切页清错机制原样迁入，10 个视图零改动）；style.css 清理退役的 .panel/.topbar/.nav-tabs。批次 C 前端随本单一并提交（api.ts 工作流 API + WorkflowsView.vue——侧边栏「流程与审批」域引用编排页，不带则干净检出构建失败）。验证：`pnpm run build`（vue-tsc + vite）全绿（MainLayout 独立分包 116KB js + 40KB css）；naming-check 0 违规；arch 门禁前端零违规（其 2 处 FAIL 为并发在途后端 affairs.py/tools.py file-too-long，非本单引入）。
- [x] 个人事务智能管理（PRD §2.2，2026-09-25）：①`packages/tools-office/office_agent_tools_office/affairs.py` 本地事务存储（DOCS_DIR/data/affairs.json，读写锁仿 templates）+ 六工具——`office.todo.list`（读，status 过滤）、`office.todo.update/delete`（写恒送审，越权/不存在 404 中文，done 记 completed_at）、`office.schedule.create`（写恒送审，meeting 算 end/milestone 恒空，参会人去重截 50，「自动邀请」如实站内口径）、`office.schedule.freebusy`（读，工作时段 09:00-18:00 减忙碌区间实测）、`office.worklog.generate`（读，日/周窗口已完成/待完成/日程三段聚合留白不编造）；`todo.create` 从演示回执升级为真实落盘。②主动推送扩四类信号（notifications.py）：todo_due（临近/逾期两口径）/meeting_upcoming（创建人+参会人逐人）/milestone_alert（N 天内）/weekly_draft_hint（业务时区周五），今日简报并入「今日到期/逾期待办数+今日会议数」；tools-office 经守卫 import 可选装配，损坏只降级 `affairs_degraded` 绝不 500；四类 collector 拆纯函数保 C901≤15。③core settings 新增 BUSINESS_TIMEZONE/TODO_DUE_LOOKAHEAD_HOURS/MEETING_UPCOMING_HOURS/MILESTONE_LOOKAHEAD_DAYS（.env.example 登记）；business_now 缺 tzdata 降级 UTC 只告警（Windows 实测撞坑，tools-office 依赖补 tzdata）。④`plugins/personal-affairs-assistant` 示例智能体（查待办/台账/空闲只读直达，建待办/建日程恒送审挂起→批准续跑）；daily-report-assistant 关键词收窄（「日程」→「查日程/看日程」、裸「待办」→「建个待办/创建待办」）避免子串抢路由。验证：五包全量 **231 passed**（core 15 / server 43（新增四类信号+降级 2 例）/ runtime 31 / tools-office 119（新增 affairs 12 例）/ mcp-bridge 23）；server conftest 新增 autouse 夹具把事务存储与业务时基钉进沙箱（消除真实 DOCS_DIR 残留与真实星期几的跨用例污染）；ruff/naming/arch/alembic check 全绿；HTTP 冒烟 `tests/smoke_personal_affairs.py` 双跑 8/8（create 落单→批准落盘→update done→schedule+freebusy 实测忙碌→worklog 计数→scan 无降级→delete 移除）；README 中英「个人事务智能管理」功能表 + 本条登记。
- [x] 本地私有化大模型接入修复（2026-09-25）：实测发现 `.env` 里的 `LLM_PROVIDERS` 是**死配置**——`Settings` 的 `env_file` 只填配置模型、不写 `os.environ`，而 `planner/llm.py:_parse_providers()` 与种子账号读的 `REVIEWER_USERNAME/PASSWORD` 都直接读 `os.environ`（`.env.example` 却如实文档化了它们）。修法取最小面：`packages/server/__main__.py` 新增 `load_env_file()`（`load_dotenv(override=False)`，真实环境变量优先）置于 `main()` 起服务之前——一行同时救活 LLM profile 与复核员账号，且放 `__main__` 不污染测试（用例导入的是 `office_agent_server.app`）；`pyproject.toml` 显式声明 `python-dotenv`。②`planner/llm.py` 提示词注入业务当天日期（`_business_today()`，时区取 `Settings.BUSINESS_TIMEZONE`，缺 tzdata 降级本机时区只告警）：实测 0.5B 不知「今天」会凭空编日期（`2023-05-19`），注入后同目标产出 `2026-09-25`（真实当天）。验证：五包全量 **232 passed**（runtime +1 例：plan/answer 出站请求体均含日期基准）；ruff/naming/arch 全绿；HTTP 实测 LLM 真被触达（报错文案由「profile 未在 LLM_PROVIDERS 中配置」变为「LLM 没有返回任何 tool_calls」）；规则路径零回归（daily-report 两步 `planner_source=rule`、DONE、数值一致率 100%）。**能力边界如实记录**：qwen2.5:0.5b（397MB）规划命中 3/5、写参数会串改标题、终答合成会编内容（`validate_answer_numbers` 会标「未通过数值校验」）——故现有办公智能体维持 `llm: ""` 走确定性规则，LLM 仅在路由兜底与 `ecommerce-assistant` 上真实生效；换更大模型只需改 `.env` 的 profile 再逐智能体开 `llm: default`。
- [x] 跨域串联示例智能体 `plugins/office-assistant`（2026-09-25，一句话串起数据分析/文档生成/制度问答）：工具白名单**同时**含三域工具（`office.data.query`/`office.data.analyze` + `office.report.generate` + `kb.ask`）——这是现状关键缺口的补位：此前三个 plugin 白名单互不相交，跨域目标在一条 run 内无落点。三条规则按场景编链，步骤间数字用 `{steps[N].result.字段}` 从上一步**真实出参**注入（禁止硬编码）。HTTP 实测：不指定智能体的「帮我做一次经营数据分析」被自动路由到 office-assistant，三步全 `planner_source=rule`、DONE、同一 `trace_id`；步骤 1 的 `args.metrics={"命中记录数":4,"全量记录数":4}` 正是步骤 0 `office.data.query(dataset=sales)` 的真实 `count`/`total`，报告 `numeric_consistency=100%`。**如实标注两处边界**：①规则规划从目标抽不出关键词，末步 `kb.ask` 问句是固化的（配 LLM_PROVIDERS 后改 `llm: default` 即由模型现场编排，降级链保底回 rules）；②`kb.ask` 目前是字符 bigram 检索，实测「对外披露审批」命中的是报销/请假条目（score 0.0769，语义不相关），且 `KB_DIR` 未落文件（`kb_dir_missing=true`）——知识域升到「语义问答」需换向量检索。验证：五包全量 **232 passed**；ruff/naming/arch 全绿；README 中英补 plugins 树与功能表条目。
- [x] `kb.ask` 向量语义检索升级（2026-09-25，第二条边界落地：bigram → 本地 embedding）：①`packages/tools-office/office_agent_tools_office/kb.py` 改**双通道**——配合《本地向量检索》配置时按**段落块**（`_chunk_entry` 按空行切 + 合并到 600 字 + 超长硬切；整篇压一个向量会丢语义且模型静默截断）取向量，余弦排序取 top_k；未配置 `EMBEDDING_*` 或调用失败（对端离线/超时/响应条数或维度非法）一律回退原字符检索，并在出参 `retrieval_mode`/`embedding_model`/`retrieval_fallback_reason` 如实标注，**降级绝不 500**；向量通道 `snippet` 返回命中段落整段（相关区间即该段），字符通道沿用原窗口口径（契约不变）。②`core/settings.py` 新增 5 键（`EMBEDDING_BASE_URL`/`MODEL`/`API_KEY`/`TIMEOUT_SECONDS`/`MIN_SCORE`，`.env.example` 登记）；契约与 `LLM_PROVIDERS` 同形（OpenAI 兼容 `/embeddings`，base_url 以 `/v1` 结尾，Ollama 时 api_key 可空），本键由工具包直读 Settings、不走 os.environ。③相似度门槛不可省：余弦对任意文本都给分，不过滤会把「问库里没有的事」答成三条低分命中——**如实口径**：实测（qwen3-embedding:0.6b + 内置 4 条制度，16 查询）相关 top1 ∈ [0.378, 0.728]、不相关 top1 ∈ [0.235, 0.449]，**两簇有重叠**，故 0.35 只是精确率/召回率取舍杆而非干净分界线，换模型/语料必须重新校准，命中分数在 `score` 原样暴露供调用方自判强弱。④`packages/tools-office/pyproject.toml` 补 `httpx>=0.27`（kb.py 新增 import 而该包未声明）。⑤kb 用例拆出 `tests/test_kb_ask.py`（原 `test_tools_office.py` 因新增用例触顶 arch `MAX_FILE_LINES=400`），内含 autouse 夹具把 `EMBEDDING_*` 钉空——否则本机 `.env` 一登记就会让整个用例集去打真实 `/embeddings`；新增 3 例（语义通道排序与整段 snippet / 端点不可达回退且标注 / 默认零网络字符通道）。验证：`ruff check .` 全绿、naming 0 违规、arch PASS（0 new / 10 baseline）、五包 pytest **234 passed**（core 15 / server 43 / runtime 32 / tools-office 121 / mcp-bridge 23）；HTTP 实测 8200（重启加载 `EMBEDDING_*`）：`retrieval_mode=embedding`、`model=qwen3-embedding:0.6b`，「出差花销找谁签字」**字符通道 0 命中 → 向量通道命中差旅标准（0.426）**、「请假三天要提前几天申请」0.6997 命中请假流程、「公司wifi密码是多少」门槛挡住 count=0 且 `degraded=true`；**如实标注残留边界**：语义召回成立但排序不完美（「出差花销找谁签字」top1 是报销制度 0.4978、差旅标准排第二），且 `KB_DIR` 仍无文件（只有 4 条 builtin 演示条目）。
- [x] PRD §2.1 补齐两件（2026-09-25，文件解析/问答的最后缺口）：①**PDF 真实解析**——`packages/tools-office/pyproject.toml` 补 `pypdf>=5`（6.19.0），`file_read.py` 新增 `_read_pdf`（逐页 `extract_text`，`pages` 页数如实返回、每页文本带「第 N 页」前缀、超长截断；`is_encrypted` 空密码解不开与 `PdfReadError` 一律转 1001 可操作提示，扫描件取不到文字就留空不编造）；`_extract` 升为公开 `extract_document`、`_READ_SUFFIXES`/`_MAX_FILE_BYTES` 升为 `READ_SUFFIXES`/`MAX_FILE_BYTES` 供同包复用。②**新增 `office.file.ask` 文件内容问答**（`file_ask.py`，office:read 免审）：对 DOCS_DIR 指定文件按**段落**检索并只摘录原文片段（`chunks_of(..., max_chars=None)` 每段独立成块——问「文件里 X 是什么」要能定位到具体段，知识库口径仍是合并到 600 字的 `kb.ask` 不动）；为免复制 80 行检索代码，把原语抽到同包新模块 `retrieval.py`（`bigrams`/`score_entry`/`hard_wrap`/`chunk_entry`/`chunks_of`/`embedding_ready`/`embed_texts`/`cosine`/`retrieve_bigram`/`retrieve_by_embedding`/`first_snippet` 与 `CHUNK_CHARS`/`EMBED_BATCH`/`SNIPPET_CHARS`），`kb.py` 瘦身为「装载条目 + 双通道编排 + 出参」（361→231 行），`test_kb_ask.py` 的 patch 目标同步改 `retrieval.embed_texts`；两处 `degraded` 口径合并由 `first_snippet` 裁决，避免 mode 分支漂移。**如实标注实测边界**：向量通道下乱码问句 `zzzqxj999` 对短文件仍得 0.3883/0.3726（门槛 0.35 是取舍杆非分界线，与上轮 kb 校准结论一致），故未按工具分叉阈值，`score` 原样暴露供自判；字符通道零命中仍必然 `degraded`（单测锁定）。继续后置（docstring 写明理由）：文案润色改写与整句多语种机翻需 LLM（本机 chat 模型能力不足），批量转 PDF 需系统 `soffice`。验证：`ruff check .` 全绿、naming 0 违规、arch PASS（0 new / 10 baseline）、五包 pytest **243 passed**（tools-office 121 → **130**：+2 PDF 真实解析/损坏拒绝改判、+7 file.ask 双通道与边界）；HTTP 冒烟新增 `tests/smoke_file_ask.py` 7/7（8200 实跑：PDF `pages=1` 且文本命中 → `office.file.ask` 命中验收段且 `retrieval_mode=embedding`/`model=qwen3-embedding:0.6b` → 字面零重叠的「多久能走完流程」仍命中该段 → 穿越 1001 / 缺文件 1004）；README 中英新增「智能内容生成与文档处理（PRD §2.1，工具侧）」功能表（把此前散落未登记的 file.read/summarize/normalize/compose/terms/docs.rename/doc.compare 一并补齐口径）+ 本文件 §5 冒烟命令登记。
- [x] 本地模型切换 qwen3:8b（2026-09-25）：`.env` 的 `LLM_PROVIDERS.default.model` 由 `qwen2.5:0.5b` 改 `qwen3:8b`（Ollama 已在本机，4.68 GB），`timeout_seconds` 60→120（实测 8B 单次规划约 30s，首次冷启动更慢，60s 易误判为超时降级）；`.env.example` 的 Ollama 示例同步为 qwen3:8b；`plugins/office-assistant` 注释里写死的「本地 qwen2.5:0.5b」口径更新。实测证据：①直连 `/v1/chat/completions` 探针——含糊目标模型主动回头追问（不硬编造参数），明确目标返回 2 条 `tool_calls` 且 args 为合法 JSON，thinking 未污染 tool_calls；②服务重启后 `ecommerce-assistant`（llm: default）run 的 `planner_source=llm`、args 由模型现场生成（该 run 最终 FAILED 是电商对端未启动 ConnectError，与本切换无关）；③规则路径零回归（daily-report 两步 `planner_source=rule`、DONE）；④`ollama ps` 显示 qwen3:8b 已加载。注：`.env` 不入库（gitignore），代码侧本次只落 `.env.example` 与注释口径；办公智能体仍 `llm: ""`（要改由模型现场编排，逐智能体开 `llm: default` 即可）。
- [x] `office-assistant` 升 LLM 主路径（2026-09-25，qwen3:8b 现场编排验证落地）：`plugins/office-assistant/agent.yaml` 的 `llm` 由 `""` 改 `default`——跨域目标由 qwen3:8b 现场编排多步，rules 保留作降级兜底（LLM 不可用/未返回 tool_calls 自动落回规则链，`runloop._choose_and_plan` 降级链零改动）。HTTP 实测：不指定智能体的经营分析目标 run `067cbb617a7f4d28859ff749a61d621d` DONE，4 步全 `planner_source=llm`（data.query → data.analyze → report.generate → kb.ask），kb.ask 问句「对外披露数据审批流程」由模型自行组织（不再是规则链固化问句），终答数值校验 passed。**能力边界如实记录**：LLM 单轮一次性提议全部步骤、拿不到上一步真实出参，step1 `office.data.analyze` 的入参 `values=[1500,2300,3500,4200,1800]` 系模型编造（与 sales 台账真实数值不符）——跨步取数仍以规则链 `{steps[N].result.x}` 注入更可靠；报告数字必须来自真实查询的场景请走规则链关键词，或待规划器支持逐步再规划。另实测本机 ollama 串行：与其他 run 并发规划时 POST /runs 可被拖到 134s，客户端超时须放宽。验证：ruff/naming/arch/alembic check 四门禁全绿；除 server 外四包 pytest 全绿（server 2 例失败为并发会话在途编辑 approval 系文件的中间态，与本单无关）；README 中英跨域串联条目改双模口径（LLM 主路径 + rules 兜底 + 编数 caveat）。
