# office-agent

> 领域无关的智能办公 Agent 平台 —— 工具注册、审批闸门、长任务调度、审计回放**开箱即用**，办公场景以插件包形式提供。

<p>
  <img alt="License" src="https://img.shields.io/badge/license-Apache--2.0-blue">
  <img alt="Status" src="https://img.shields.io/badge/status-M0%20骨架-orange">
  <img alt="Python" src="https://img.shields.io/badge/python-3.11%2B-informational">
</p>

[English](README_EN.md) | 简体中文

## 项目状态

当前处于 **M0 骨架阶段**：仓库结构与本 README 先行落盘，内核代码、`docker compose` 演示、Web 管理台将随里程碑推进逐步补齐。欢迎 Star 关注、提 Issue 与 PR 共建。

## 为什么需要它

多数 Agent 框架把精力放在"让模型会调工具"，却在生产落地时卡在四件事上：**权限、审批、审计、数据口径**。office-agent 把这套治理底座做成领域无关、可复用的内核，让业务方只需专注"有哪些工具"，而不必重造轮子。

## 核心能力

- **工具注册中心** —— `ToolSpec` + `Scope` 命名空间 + 幂等键 + 熔断，工具即插即用。
- **审批闸门** —— 所有写操作**恒送审**，红线不可绕过。
- **长任务调度** —— `checkpoint` / `resume` / SSE 进度回流，超时退避重试。
- **审计回放** —— `trace_id` 全链路留痕，动作可回溯。
- **RBAC / 租户隔离** —— 最小权限集，凭据仅走环境变量、绝不入库。
- **数值不可编造** —— `verify_numbers` 后置校验，LLM 不可用时走纯数字模板**降级不 500**。

## 仓库结构

```
office-agent/
├── LICENSE                        # Apache-2.0
├── README.md / README_EN.md       # 中英双语
├── docker-compose.yml             # 五分钟跑通演示（规划中）
├── packages/
│   ├── core/                      # Agent 内核：contracts/registry/policy/executor/runtime
│   ├── server/                    # FastAPI 通用壳：auth/RBAC/tasks/approvals/audit/governance-status
│   ├── tools-office/              # 内置办公工具包
│   └── mcp-bridge/                # MCP wire 协议桥
├── plugins/
│   └── _template/                 # 自定义工具插件模板（SPI 文档配套）
└── apps/web/                      # Vue3 管理台：登录/任务/审批/审计四页
```

## 快速开始（规划中）

```bash
# M1 起可用
docker compose up -d
# 打开 http://localhost:8000 体验报表生成 demo
```

## 内置办公工具包（packages/tools-office）

| 层 | 工具 | Scope | 说明 |
|---|---|---|---|
| 读 | `report.generate` / `bi.query` | `office:read` | 数据源为用户配置的只读连接器白名单；数值一致率 ≥99% |
| 读 | `doc.summarize` / `kb.query` | `office:read` | 内置 RAG，默认 SQLite + 内置向量，可切 pgvector |
| 读 | `schedule.view` | `office:read` | 日历只读 |
| 写 | `todo.create` / `schedule.book` / `doc.draft` / `ticket.create` / `announce.draft` | `office:write` | **全部恒送审**；`doc.draft` 只起草不发布；`idem_key` 必带 |

## 领域无关性

主包 `packages/` 内**不含任何特定业务域语义**，此约束由 CI 的 grep 门禁强制执行：

```bash
grep -riE "finance|purchase|risk|sku|订单|电商" packages/  # 仅允许出现在 plugins/ 与 docs 示例
```

## Roadmap

| 期 | 内容 | 验收 |
|---|---|---|
| **M0** | 骨架 + 内核提取 + 测试移植全绿 | py_compile / ruff / pytest 过；grep 领域无关性 PASS |
| **M1** | server 壳 + tools-office 读层 + docker compose demo | 五分钟跑通报表生成；LLM 挂走纯数字模板降级 |
| **M2** | 审批闸门 + 写层工具 + web 四页 | 巡检→建单→审批 E2E；引导词 0 命中 |
| **M3** | mcp-bridge + 首个外部插件 + SPI 文档 | 经 MCP 注册成功消费 |

## 扩展与插件

自定义工具请基于 `plugins/_template/` 开发，通过 SPI 注册 `ToolSpec` 即可接入内核，无需改动主包。详见 `docs/` 内 SPI 文档（M3 补齐）。

## 文档

- 决策记录：[docs/ADR-0003-办公Agent拆分独立开源项目.md](docs/ADR-0003-办公Agent拆分独立开源项目.md)
- 骨架与提取方案：[docs/office-agent仓库骨架与内核提取方案.md](docs/office-agent仓库骨架与内核提取方案.md)

## 贡献

零硬编码（Settings/.env 模式）、遵循现有代码风格、PR 需通过 CI（pytest + ruff + 领域无关性 grep）。`CONTRIBUTING.md` 与行为准则即将补充。

## 安全

请勿公开提交漏洞，安全问题请参照 `SECURITY.md`（即将补充）私下上报。演示数据内置且不含任何 PII。

## 许可

本项目基于 [Apache-2.0](LICENSE) 开源。
