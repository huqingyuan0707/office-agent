# office-agent —— 开源智能办公 Agent（M0-M1 骨架）

一句话架构：**FastAPI + SQLite 单进程后端 —— 工具注册中心 → Scope 鉴权执行器（超时硬拦 + 全程审计）→ 审批闸门 → 审计留痕；内置办公工具开箱即用，外部系统以 HTTP 桥接插件可选接入，主包零业务域语义。**

设计依据：`docs/office-agent仓库骨架与内核提取方案.md`、`docs/ADR-0003-办公Agent拆分独立开源项目.md`（本次交付对应 M0-M1 压缩版）。

## 五分钟启动（独立模式）

```powershell
# 1. 安装依赖（建议独立虚拟环境）
pip install -r requirements.txt

# 2.（可选）复制配置并修改
copy .env.example .env

# 3. 启动（默认管理员 admin / admin123）
python -m uvicorn office_agent.main:app --port 8200
```

体验：

```powershell
# 登录拿 token
curl.exe -s -X POST http://127.0.0.1:8200/auth/login -H "Content-Type: application/json" -d '{\"username\":\"admin\",\"password\":\"admin123\"}'

# 调用日报工具（每个数字自带 (来源: input.metrics) 溯源标注）
curl.exe -s -X POST http://127.0.0.1:8200/tools/invoke -H "Authorization: Bearer <token>" -H "Content-Type: application/json" -d '{\"name\":\"office.report.generate\",\"args\":{\"title\":\"测试日报\",\"metrics\":{\"gmv\":12345,\"orders\":100}}}'
```

生产环境务必用环境变量覆盖 `JWT_SECRET` 与 `ADMIN_PASSWORD`（明文口令仅在首次登录时 bcrypt hash 进内存，不落库不入日志）。

## API 一览

统一信封：成功 `{ok:true,data}`；业务失败 `{ok:false,error:{code,message}}`；鉴权失败 HTTP 401。

| 方法 | 路径 | 说明 | 鉴权 |
|---|---|---|---|
| GET | /healthz | 健康检查（含已注册工具数） | 否 |
| POST | /auth/login | 登录换取 JWT（sub+scopes） | 否 |
| GET | /tools | 当前用户 scope 可见的工具列表（含 JSON Schema） | 是 |
| POST | /tools/invoke | 调用工具 `{name,args}`；需审批工具只建审批单返回 pending | 是 |
| GET | /tasks | 任务执行记录（最新在前） | 是 |
| GET | /approvals | 审批单列表 | 是 |
| POST | /approvals/{id}/decide | 审批 `{approve,comment}`；审批人≠提交人（同人 1001） | 是 |

错误码约定：400 参数 / 403 Scope 不足 / 404 不存在 / 409 重名或重复审批 / 500 执行失败 / 504 超时 / 502 桥接失败 / 1001 审批同人红线。

## 联动电商客服系统（可选插件，默认关闭）

在 `.env` 中配置后重启：

```
ECOMMERCE_ENABLED=true
ECOMMERCE_API_BASE=http://127.0.0.1:8000
ECOMMERCE_TOKEN=<电商服务账号 JWT（最小只读权限）>
```

- 自动注册 `ecommerce.screen.summary` / `ecommerce.finance.bills` 两个只读桥接工具，响应必带 `source:"ecommerce-api"` + `fetched_at` 溯源；
- 电商服务不在线时启动期自动跳过注册（log warning），独立运行零影响；
- 数据不出域：桥接只走 HTTP + Bearer token，**绝不直连电商数据库**；
- 领域桥接语义全仓唯一出处：`office_agent/tools_ecommerce.py`，主包其余文件 grep 无 finance/purchase/risk/sku 等电商词元。

## 目录结构

```
office_agent/
├── config.py          # Settings 全量可调项（.env/环境变量双读，零硬编码）
├── db.py              # async engine + tasks/approvals/audit_logs 三表 + init_db
├── contracts.py       # ToolSpec / ToolError / Scope 常量（内核契约）
├── registry.py        # 工具注册中心（重名拒绝）
├── executor.py        # 执行器：Scope 硬拦 / 审批分流 / 超时 / 全程审计 / trace_id
├── approvals.py       # 审批闭环：create/list/decide（同人 1001 红线）
├── auth.py            # 登录（bcrypt）+ JWT + get_current 依赖
├── tools_office.py    # 内置办公工具（领域无关）：日报生成 / BI 演示查询
├── tools_ecommerce.py # 电商桥接插件（全仓唯一电商语义文件，可选）
└── main.py            # FastAPI 壳：路由/统一信封/CORS/可选静态托管
```

## 硬红线（自检口令）

1. **领域无关性**：`Select-String -Path office_agent\*.py -Pattern "finance|purchase|risk|sku"` 仅允许 tools_ecommerce.py 命中；
2. **可选插件**：ECOMMERCE_ENABLED=false 或外部服务离线 → 服务正常启动，只剩内置工具；
3. **数据不出域**：桥接仅 HTTP + Bearer token，零数据库直连；
4. **数值不可编造**：日报纯模板直出、每个数字带溯源标注；BI 查询显式标注 `source=builtin-demo`；
5. **审批红线**：需审批工具 invoke 不执行只建单；审批人 ≠ 提交人（同人 1001）。

## License

Apache-2.0（随 M0 收尾补 LICENSE 文件）。
