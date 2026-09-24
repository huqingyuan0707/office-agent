---
name: fullstack-code-rule
description: Use when writing or reviewing any Python under packages/ or any TS/Vue under apps/web/src in office-agent. Unified front+back gate: backend layering/governance, frontend data/routing, naming/arch health. Style details defer to backend-code-style/frontend-code-style/naming-check/anti-shit-code; this skill is the single entry checklist.
---

# 全栈代码规则（前后端统一约束 · office-agent 强制）

> 定位：本 Skill 是前后端唯一的统一入口规则，回答“这段代码能不能合入”。
> 细节唯一出处不复制：后端风格 `skills/backend-code-style/SKILL.md`，前端风格 `skills/frontend-code-style/SKILL.md`，
> 命名机检 `skills/naming-check/SKILL.md`，架构健康 `skills/anti-shit-code/SKILL.md`，项目纪律 `AGENTS.md` §3/§4。
> 总原则：**能进门禁的不写散文**——本文件每条规则末尾标注机检出处，无机检的不立规则。

## 1. 后端（packages/*/office_agent_*/ · FastAPI）

### 1.1 分层（机检：`check_arch.py` endpoint-db-op）
- `api/**` 只做薄封装：解析入参 → 调 `services/` → `ok()/fail()`。**禁止在端点写业务逻辑、禁止直接触 DB**（`select/execute/add/commit/scalar` 出现在 `api/` 即 FAIL，存量见 `check_arch.py BASELINE`）。
- `services/*.py` 放纯业务，不依赖 FastAPI 对象（只依赖 `settings`/其他 service/内核 `registry/policy/executor`）。
- 工具实现（`tools-office`/`plugins`）只写 handler + `ToolSpec` 纯函数，不 import ORM/FastAPI；入参校验交给内核 `validate_args`，handler 内不重复校验框架层已做的事。

### 1.2 信封与错误码（机检：review + `ruff`，信封形状靠 smoke 覆盖）
- 成功一律 `ok(data, msg)`，失败一律 `fail(code, 中文可操作msg)`，`trace_id` 由中间件自动带，禁止手拼信封、禁止裸返回 dict。
- 号段：`1xxx` 通用 / `2xxx` 模型 / `3xxx` 预留领域插件（内核不占用）/ `4xxx` 任务工具 / `5xxx` 系统。新增码必须落号段，上游透传码原样带不翻译。

### 1.3 治理红线（机检：smoke + 冒烟断言，违反即功能 FAIL）
- 工具注册一律走 `registry.register(ToolSpec + JSON Schema + Scope)`，禁止散落直调；重名热替换同规格，`spec_to_dict` 不含地址凭据。
- 写动作恒送审：`requires_approval=True` 的工具 invoke 只落审批单返 `pending_approval`，`idem_key` 必带；审批人≠提交人（同人 1001），批准后以申请人真实角色重放。
- 降级绝不 500：外部依赖不可用走降级 + `degraded` 标记；MCP 对端离线只告警不注册；IM 未配返 `not_configured` 不发网，推送失败全 catch 不阻断审批。
- 审计只追加不改：工具调用/审批/登录好坏两条路径都 `audit.emit`；跨系统返回必带 `source + fetched_at`（远程再加 `provider_id/source_endpoint/upstream_trace_id`），禁止编造溯源。
- 数据不出域：不直连外部库，一切经 `LINKAGE_PROVIDERS` 白名单工具拉取；凭据只走环境变量，绝不入库入码。核心包 `packages/` 禁止出现业务域词元（`finance/purchase/sku/订单/电商` 等，CI grep 门禁，仅 `plugins/`+`docs` 允许）。

### 1.4 配置与文件头
- 全部可调进 `packages/core/office_agent_core/settings.py::Settings`（字段名即环境变量名），禁止硬编码 URL/密钥/阈值/超时（超时/重试只读 `registry.timeout_of/retries_of`）。
- 每个新 Python 文件头必须中文 docstring：职责 + 链路 + 对齐章节（见 `backend-code-style` §2 范式）。

## 2. 前端（apps/web/src · Vue3 + TS + Vite）

### 2.1 基础（机检：`npm run build` 含 vue-tsc + ESLint 思想沿用）
- `<script setup lang="ts">`，页面方法一律箭头函数（禁 `function foo(){}` 声明）；样式用 `var(--*)` token + `<style scoped>`，禁止硬编码主色。
- 类型：PEP 604 思想对应 TS 侧——复用 `api.ts` 已定义接口，禁止各视图自造同一实体的第二形状；同一字段前后端同名（`security_level` 不许改名 `level`）。

### 2.2 数据层（机检：`check_arch.py` views-fetch + 人工空态检查）
- 一切数据走 `src/api.ts` 具名函数（`request` 统一解包 `{code,msg,data,trace_id}`，`code!==0` 抛服务端 msg），**禁止视图直写 `fetch/axios`**。
- 禁止 mock/硬编码兜底：列表 `onMounted` 调真接口，失败置空 + 报错提示，不编造数据；缺失字段不渲染不编造。
- 401 走中央 `setUnauthorizedHandler`，禁止页面自跳；GET query 一律 `encodeURIComponent`。

### 2.3 文案与反馈
- 界面文案注释用中文；枚举中文化走映射表（如 `LEVEL_TAG`），禁止模板散落字面量；破坏性操作先确认框，成功失败一律消息提示。

## 3. 命名与体量（前后端通用机检）

### 3.1 命名（机检：`python skills/naming-check/scripts/check_naming.py`，棘轮只减不增）
- Python 文件 `snake_case`，函数 `snake_case`（dunder 除外），类 `PascalCase`，模块级禁 camelCase；`composables/*.ts` 必须 `useXxx`，`.vue` 必须 `PascalCase`，`stores/*.ts` camelCase。
- 泛化黑名单禁入：`foo/bar/temp/doSomething/handleStuff/processData/myFunc` 等；单字母参数仅放行 `_ i j k n x y z a b q`。
- 语义五问（人工）：一句话说清职责 / 缩写项目通用 / 布尔 `is/has/can` / 全链路同名 / 与领域词汇表一致。

### 3.2 体量（机检：`ruff C901` + `check_arch.py` file-too-long）
- `ruff` 圈复杂度 `max-complexity=12`（见根 `ruff.toml`）；单函数超 60 行或参数超 4 个即拆；单文件超 400 行即拆（豁免只进 `BASELINE/FILE_LINE_BUDGET` 记名，涨一行即红）。
- 前端嵌套 ≤3 层；同一行为只允许一个实现（权限判断/去重/错误映射收口一处），不抽“长得像”的代码。

## 4. 提交前必跑（本地与 CI 同口径）

```bash
.venv\Scripts\ruff.exe check .
.venv\Scripts\python.exe -m pytest packages/core packages/server packages/runtime packages/tools-office packages/mcp-bridge -q
python skills/naming-check/scripts/check_naming.py
python skills/anti-shit-code/scripts/check_arch.py
.venv\Scripts\alembic.exe check
cd apps/web && npm run build
```

- 模型改列必须 `alembic` autogenerate 迁移，禁手改库；改接口同步改 `docs/` 对应节 + README 功能清单；新文件带中文头；贴命令输出不说“应该过了”。

## 5. 自检清单（每项都必须是“是”）

- [ ] 后端：端点薄封装无 DB 操作？信封 `ok/fail`？写工具恒送审 + `idem_key`？降级 `degraded` 不 500？审计双路径？溯源齐备？主包无业务域词？
- [ ] 前端：走 `api.ts` 无直写 fetch？无 mock 兜底失败置空？401 中央处理？箭头函数 + token 样式 + scoped？
- [ ] 命名体量：两脚本全绿且基线未增？复杂度/行数/嵌套/参数未超？至少完成一条最小清洁（改名/消重/删死分支/明边界/补测试）？
