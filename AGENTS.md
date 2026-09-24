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
