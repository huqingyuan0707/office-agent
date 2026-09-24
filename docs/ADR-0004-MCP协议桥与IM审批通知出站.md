# ADR-0004：M3 引入 MCP 标准协议桥与 IM 审批通知出站的决策

- 日期：2026-09-24
- 状态：已落地
- 决策人：office-agent 维护者
- 关联文档：`docs/office-agent仓库骨架与内核提取方案.md` §2/§5（M3）/§7.2/§7.4；
  `智能办公Agent 产品需求文档.md` §2.2（主动消息推送）；`AGENTS.md` §3（后端红线）

## 1. 背景与约束

- 现状：M2 的跨系统联动走 HTTP + JWT 自定义网关（`linkage.RemoteToolProvider`，
  出站 `POST {base_url}{path}`，请求体 `{"tool","args"}`，响应信封 `{code,msg,data,trace_id}`）。
  方案 §7.4 已定 M3 目标：对端升级为**标准 MCP Server**，本侧经 `mcp-bridge` 接入。
- 另一缺口：PRD §2.2 要求「审批单产生/超时时推送 IM」，此前只有站内 `notifications` 表，
  人不在系统里就看不到提醒。
- 约束：
  1. **换协议不改业务代码**（方案 §7.4）：ToolSpec/registry/executor/审批/审计一条链零改动；
  2. **数据不出域**：仍只经工具调用取数，溯源必带 `source_endpoint + fetched_at`；
  3. **降级绝不 500**：IM 推送失败不得阻断审批流，MCP 服务未起不得阻断启动；
  4. **凭据只走环境变量**：零硬编码，token 绝不入库入码；
  5. 可选性：开源用户多数既无电商项目也无 IM 机器人，两能力都必须「没配就自然缺席」。

## 2. 候选方案

### 2.1 MCP 接入方向

| 方案 | 优点 | 缺点 | 成本 |
|---|---|---|---|
| A 本侧作为 MCP **Server** 对外暴露工具 | 第三方 MCP 客户端可直连本内核 | 与 §7.4「经 mcp-bridge 接入」方向相反；需另造鉴权/审批映射，M3 验收（电商经 MCP 被消费）不成立 | 高 |
| B 本侧作为 MCP **Client** 消费外部 MCP Server（选定） | 与方案 §7.4 一致；ToolSpec 与 MCP tool 同构，可自动发现注册；对端只换协议栈 | 需新增协议解析层 | 中 |
| C 继续用 M2 自定义 HTTP 网关 | 零新增代码 | 方案 §7.4 的 M3 目标落空；每接一个系统重写一套适配 | 低但方向错 |

### 2.2 MCP 传输与实现方式

| 方案 | 优点 | 缺点 | 成本 |
|---|---|---|---|
| A 引入官方 `mcp` Python SDK（stdio/SSE） | 协议细节由 SDK 兜底 | 新增重依赖；stdio 需托管子进程，与「宿主无关、可独立 pip 安装」的包定位冲突；单测需真子进程，慢且脆 | 高 |
| B 手写 JSON-RPC 2.0 over HTTP（选定） | 零新增依赖；复用既有 httpx/MockTransport 测试基建；与内核「上游即 HTTP 服务」形态一致 | 需自行维护协议版本与错误映射 | 低 |
| C stdio 子进程 + 手写 JSON-RPC | 贴合本地工具型 MCP Server | 进程生命周期/僵尸进程/跨平台编码问题多，MVP 收益不抵复杂度 | 中 |

### 2.3 IM 通知落点

| 方案 | 优点 | 缺点 | 成本 |
|---|---|---|---|
| A 混入 `linkage` 出站层 | 复用出站头 | linkage 是「工具调用出口」，IM 是**旁路通知**，混入会把通知纳入工具审计与熔断语义，概念污染 | 中 |
| B 独立 `services/im_notifier.py`（选定） | 与审批流解耦，失败独立降级；三厂商消息体适配集中一处 | 需新增一个 service | 低 |
| C 让插件/工具承担推送 | 可扩展 | 通知不是「工具调用」，且需要审批单上下文，绕远 | 中 |

## 3. 决策

- **MCP 方向**：选 2.1-B（本侧为 MCP Client）。
- **传输与实现**：选 2.2-B（JSON-RPC 2.0 over HTTP，手写，不引 MCP SDK）。
  协议版本 `2025-06-18`；方法 `initialize` / `tools/list` / `tools/call`。
- **接入方式**：`MCPProvider` 与 `RemoteToolProvider` **同形**（`provider_id` + `invoke()` + `aclose()`），
  经既有 `linkage.register_provider()` 登记 → executor 一行不改。
- **工具注册**：`tools/list` 自动发现 → 按 MCP 注解映射 ToolSpec：
  `readOnlyHint=true` → 只读 Scope、免审批、幂等；**注解缺失或非只读 → 恒送审**
  （未知即从严，写动作唯一放行口仍是审批中心）。
- **配置分流**：`Settings.LINKAGE_PROVIDERS` 条目新增 `transport` 字段（默认 `"http"`）；
  内核只认领自己的 `"http"`，`transport="mcp"` 的条目由 mcp-bridge 认领——
  内核不出现任何协议分支，协议细节不外泄进主包。
- **IM 通知**：选 2.3-B。`services/im_notifier.py` 通用出站（`generic`/`feishu`/`dingtalk`/`wecom`
  四种消息体 + 飞书/钉钉签名），挂在「审批落单」与「审批超时扫描」两处，**全 catch + 超时上限**，
  未配置即 `{"sent": false, "reason": "not_configured"}` 直接返回，不发网络。

## 4. 后果

- 正面：对端换 MCP 协议栈时，本侧只改配置（`transport: "mcp"` + MCP 端点），
  工具清单、Scope、审批、审计、溯源全部复用；IM 通知与审批流解耦，机器人挂了不影响审批。
- 负面/风险 + 缓解：
  1. 手写协议易随 MCP 版本漂移 → 协议版本钉死为常量 `protocol.PROTOCOL_VERSION`
     （`office_agent_mcp_bridge/protocol.py`，不设环境变量：版本是协议契约而非部署参数）；
     `initialize` 回执版本不符只告警并按对端版本继续；
  2. MCP `tools/list` 依赖对端在线 → 启动期发现失败只告警不注册，不阻断启动（降级口径）；
  3. 注解缺失导致读工具被误判送审 → 保守方向的误判可人工纠正（注解补全即免审），
     反向误判（写动作免审）才是红线，不做；
  4. IM webhook 是群机器人，消息面向群而非个人 → 只推「有单要处理」类事件，不含敏感明细。
- 回滚方案：删掉 `Settings.LINKAGE_PROVIDERS` 里的 `transport="mcp"` 条目即回到 M2 HTTP 网关；
  清空 `IM_WEBHOOK_URL` 即关闭 IM 推送（两处均无需改代码）。
- 可观测验证：`agent.linkage` 事件带 `provider_id/tool/ok/latency_ms/trace_id`（MCP 与 HTTP 同口径）；
  IM 出站记 `agent.im_webhook`（`kind/ok/latency_ms/vendor`）。

## 5. 落地清单

- [x] `packages/mcp-bridge`（`office_agent_mcp_bridge`）：`protocol.py` JSON-RPC 编解码与错误分级、
      `provider.py` MCPProvider、`catalog.py` tools/list → ToolSpec 映射、`bootstrap.py` 装配入口
- [x] `Settings.LINKAGE_PROVIDERS[].transport` + `ProviderConfig.transport`；内核认领 `"http"`
- [x] `Settings.IM_WEBHOOK_*`（url/secret/type/timeout）
- [x] `services/im_notifier.py` + 审批落单/超时两处挂钩
- [x] 单测：mcp-bridge（协议往返/溯源/错误分级/注册规则）+ server（IM 四厂商消息体/降级不阻断）
- [x] 文档同步：`.env.example`、README 中英、方案文档 §5 M3/§7.4、`AGENTS.md` §5/§6
