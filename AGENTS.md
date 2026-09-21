# AGENTS.md — office-agent 项目执行入口（人 + AI 都认它）

> 领域无关的智能办公 Agent 平台。决策背景见 `docs/ADR-0003-办公Agent拆分独立开源项目.md`，
> 仓库结构与内核提取见 `docs/office-agent仓库骨架与内核提取方案.md`，ADR 体例见 `docs/ADR规范与模板.md`。

## 1. 文档索引（Single Source of Truth）

| 改什么 | 先读什么 |
| --- | --- |
| 决策/回滚/红线出处 | `docs/ADR-0003-办公Agent拆分独立开源项目.md` |
| 仓库结构/包边界/里程碑 | `docs/office-agent仓库骨架与内核提取方案.md` |
| 新增 ADR | `docs/ADR规范与模板.md`（选型变更先写 ADR 再动代码） |
| 后端风格 | `skills/backend-code-style/SKILL.md` |
| 前端风格 | `skills/frontend-code-style/SKILL.md` |
| 命名质量 | `skills/naming-check/SKILL.md` |
| 架构健康 | `skills/anti-shit-code/SKILL.md` |

## 2. AI 工作流（强制）

1. 动代码前先读 §1 对应文档，不凭记忆写。
2. 回复开头声明一行：`对齐文档：<文件名> §<节> + Skill §<节>`。
3. 新文件必须写中文文件头 docstring（职责 + 链路 + 对齐章节）。
4. 改接口必须同步改 docs 里对应设计文档节；完成功能同步更新 README 的功能清单。
5. 贴验证命令输出，不说「应该过了」。

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
# 后端（仓库根）
ruff check .
python -m pytest packages -q          # 门禁脚本适配前：先跑 packages 内既有 pytest
# 前端
cd apps/web && npm run build
```

## 6. 待办登记（门禁脚本适配）

- [ ] `naming-check` / `anti-shit-code` 脚本本体适配：扫描根 `backend/app`→`office_agent`+`packages/server/office_agent`、前端 `frontend/src`→`apps/web/src`，ratchet baseline 重新初始化（规则口径不变，见两 SKILL.md）。
- [ ] pre-commit（ruff 钉版 0.16.7）+ commitlint 引入。
