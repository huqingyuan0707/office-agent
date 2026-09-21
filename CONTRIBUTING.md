# 贡献指南（CONTRIBUTING）

感谢关注 office-agent！本指南帮助你快速上手开发环境与提交流程。

## 开发环境

- Python 3.11+
- 可选：Docker / Docker Compose（M1 起用于 demo）
- 工具链：`ruff`（lint + format）、`pytest`（测试）

```bash
pip install ruff pytest pytest-cov
```

## 本地开发流程

1. Fork 仓库并新建分支：`git checkout -b feat/your-feature`
2. 开发并保持测试全绿
3. 提交 PR，说明改动动机与验收方式

## 代码规范

- **零硬编码**：密钥、连接串只走环境变量（Settings/.env 模式），绝不入库；
- **风格**：遵循仓库既有代码风格，提交前通过 `ruff check` 与 `ruff format --check`；
- **文档**：公开 API 需有 docstring；面向用户的行为变更需同步中英 README。

## 测试要求

- 所有 PR 必须通过 `pytest`，新增功能必须附带测试；
- 内核文件移植/改造时逐文件 `py_compile` + 原测试移植（见 docs/ 内核提取方案）；
- LLM 相关逻辑需提供不依赖外部服务的可降级离线测试。

## 领域无关性红线（CI 门禁）

主包 `packages/` 内禁止出现任何特定业务域语义，CI 强制执行 grep 门禁：

```bash
grep -riE "finance|purchase|risk|sku|订单|电商" packages/  # 仅允许出现在 plugins/ 与 docs 示例
```

业务域特定逻辑请放入 `plugins/`。

## 提交信息

建议使用 Conventional Commits：`feat:` / `fix:` / `docs:` / `refactor:` / `test:` / `chore:`。

## PR 检查清单

- [ ] pytest 全绿，新增功能有测试
- [ ] ruff check / format 通过
- [ ] 领域无关性 grep 门禁 PASS
- [ ] 无新增硬编码凭据；.env 变更同步到 `.env.example`
- [ ] 用户可见行为变更已同步中英 README

## 行为准则

参与本项目即同意保持友善、尊重与专业的交流氛围，完整 Code of Conduct 即将补充。

## 安全问题

请勿在公开 Issue 中披露漏洞，参照 `SECURITY.md`（即将补充）私下上报。
