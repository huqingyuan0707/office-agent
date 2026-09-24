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
.venv\Scripts\python.exe -m pytest packages/core packages/server packages/runtime packages/tools-office -q
python skills/naming-check/scripts/check_naming.py   # 命名质量门禁（棘轮：存量只准减不准增）
python skills/anti-shit-code/scripts/check_arch.py   # 架构健康门禁（分层/体量，10 处 baseline 债务）
# 迁移（持久库 schema 演进唯一入口；模型改列必须 autogenerate 迁移，create_all 不做 ALTER）
.venv\Scripts\alembic.exe upgrade head               # 应用迁移（新库直接建全）
.venv\Scripts\alembic.exe check                      # 漂移自检（CI 用：模型与库不一致即 FAILED）
# HTTP 级冒烟（先启动服务：.venv\Scripts\python.exe -m office_agent_server）
.venv\Scripts\python.exe tests\smoke_v1_features.py  # V1.0 七项功能 17 断言（可重复执行）
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
- [x] PRD V1.0 七项功能落地（2026-09-24，全部走同一治理口径，提交 1e631dc / 15d34ce / 432536e / 本条随提交D）：①知识库问答 `kb.ask`（KB_DIR+内置条目，bigram 检索，无命中 degraded）②文案生成 `office.report.generate` 扩 weekly + 新增 `office.minutes.generate`（模板直出缺板块留白）③主动消息推送 `notifications` 表（迁移 4710c1599916）+ 三类扫描信号（审批超时/任务失败/今日简报）+ 列表/scan/已读端点（唯一键去重不刷屏）④图片OCR `ocr.image`（元数据直读 + Tesseract 缺失降级不编造）⑤文档对比 `office.doc.compare`（段落级 diff）⑥任务拆解 `office.task.decompose`（缺人/缺期留空不臆造）+ `office.task.commit`（恒送审批量建单二次确认）⑦自定义模板 `office.template.save`（送审落盘）+ `office.template.apply`（unfilled 如实列出）；HTTP 级冒烟 `tests/smoke_v1_features.py` 17/17（可重复执行）；测试基线 core 14 / server 21 / runtime 25 / tools-office 28 = 88 passed；工具包单测 28 例入 `packages/tools-office/tests/`。
