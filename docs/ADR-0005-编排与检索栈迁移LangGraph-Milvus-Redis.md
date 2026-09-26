# ADR-0005：编排与检索栈迁移 LangGraph / Milvus / Redis

- 日期：2026-09-26
- 状态：接受（阶段一 LangGraph 已落地（2026-09-26，实测见 §5 清单）→ 阶段二 Milvus 待实施 → 阶段三 Redis 待实施）
- 决策人：项目所有者（会话指令「项目改为 LangGraph/Milvus/Redis」，三项节奏决策已确认）
- 关联文档：`AGENTS.md` §3（后端红线）/ `CLAUDE.md` §一.2（依赖报批）§八（Agent 专属规则）/
  `.trae/documents/LangGraph-Milvus-Redis三阶段迁移方案.md`（实施计划 SSOT）

## 1. 背景与约束

- 现状与问题：
  - `CLAUDE.md` 声明技术栈为 LangGraph/Milvus/Redis，与实际（自研 `RunLoop` + 检索每次全量重算
    embedding + 单实例 asyncio 调度环）背离，开源读者按声明栈上手会踩空；
  - `packages/runtime/runloop.py`（381 行）逼近 arch 门禁 400 行上限，继续自研演进无处加能力；
  - `retrieval.retrieve_by_embedding` 每次查询对**全部段落**重新调 `/embeddings`（Ollama 串行、
    无向量库、无缓存），语料增长即线性变慢；
  - 调度环假设单实例，多实例部署会重复触发 `workflow_run` 作业。
- 约束（必须兼容）：
  - 治理内核 `packages/core`（executor/registry/policy/审批闸门/审计/TRANSITIONS）**零改动**；
  - `execute_run(db, *, task, spec, goal, user, trace_id)` 签名是 `approvals.resolve_pending`
    的注入契约，不得变更；
  - `Task.checkpoint` JSON 形状是前端轮询 `_run_view` 与审批中心的 SSOT，不得漂移；
  - SQLite 锁纪律（fb42ac3：落行即 commit、finalize_answer 前 commit、busy_timeout、WAL）逐点保持；
  - 「降级绝不 500」：Milvus/Redis 不可用一律回退现有路径并如实标注；
  - 本机 Windows：Milvus/Redis 无原生服务，经 Docker Compose（Docker Desktop 29.6.1 + WSL2）提供。

## 2. 候选方案

| 方案 | 优点 | 缺点 | 成本 |
|---|---|---|---|
| A 双轨并存（开关切 RunLoop/LangGraph） | 回滚最稳 | 两套循环骨架维护面翻倍，400 行门禁装不下，验证矩阵爆炸 | 高 |
| B 继续自研演进 | 零新依赖 | 与声明技术栈永久背离，放弃社区图可视化/HITL 原语 | 机会成本 |
| C（选定）分阶段直接替换：LangGraph 重写主循环 → Milvus 向量检索 → Redis 缓存/锁 | 每阶段独立验证可回滚；契约零破坏；收益逐段兑现 | 三阶段周期拉长；新增三个可选外部依赖 | 中 |

编排层内部再裁决（阶段一）：

| 路 | 做法 | 结论 |
|---|---|---|
| a LangGraph interrupt()+AsyncSqliteSaver | 图内持久化，社区惯用法 | **否**：双真相源漂移（图 checkpoint vs Task.checkpoint）、每 super-step 写 SQLite 撞锁纪律、execute_run 签名被迫分「新 run/Command 续跑」两态语义撕裂、msgpack 反序列化需额外安全配置 |
| b（选定）条件边到 END + 自管 checkpoint 续跑重建 | 审批挂起走现有 `suspend_for_approval`，route=suspended→END；续跑重建同一张图（编译产物无状态），从 `Task.checkpoint` 回放 `_plan_from_checkpoint`/`_replay_results`/`approved_steps` | 契约零破坏、37 例 runtime 测试绝大多数原样保过、回滚=revert 一个 commit；LangGraph 图骨架/条件边价值仍拿到 |

检索侧裁决（阶段二）：单 collection + `origin` 标量过滤（五源联查一次合查；本仓语料量级远未到分
collection 收益点）；`visibility` 权限**不进**向量库（检索后回表过滤，杜绝权限第二真相源）；
metric=COSINE 与现余弦同构，`EMBEDDING_MIN_SCORE=0.35` 直接复用。

## 3. 决策

- 选择 C：三阶段独立落地，每阶段「验证全绿 → commit → push」，RunLoop 直接替换不留双轨。
- 新依赖报批清单（CLAUDE.md §一.2，py3.11 兼容、pip 可装）：
  | 包 | 版本钉 | 落位 | 阶段 |
  |---|---|---|---|
  | `langgraph` | `>=1.2.12,<2.0` | packages/runtime | 一 |
  | `pymilvus` | `>=2.6.17,<2.7` | packages/tools-office | 二 |
  | `redis` | `>=8.0,<9` | packages/core | 三 |
  - `langgraph-checkpoint-sqlite` 阶段一**不装**（路 b 无 checkpointer）；
  - planner 保持自研 httpx，不引 langchain 模型集成（LLM_PROVIDERS 契约不重写）；
  - LangGraph 只用 StateGraph/条件边/END 最小面，不碰 prebuilt/checkpoint/streaming。
- 服务侧：`deploy/docker-compose.yml` 提供 `milvusdb/milvus:v2.6.23` + `etcd v3.5.18` +
  `minio`（阶段三追加 `redis:8-alpine`）；全部可选——`MILVUS_URI`/`REDIS_URL` 留空即零网络现行为。

## 4. 后果

- 正面：技术栈与声明一致；编排循环换成社区标准骨架（后续图可视化/streaming 有升级路径）；
  Milvus 把「全量重算」降为「只算增量」；Redis 补查询向量缓存与调度环多实例护栏。
- 负面/风险 + 缓解：
  - LangGraph minor 节奏快 → 钉 `>=1.2.12,<2.0`，最小面使用，venv 实测版本记录进 AGENTS §6；
  - 锁纪律回退风险（`database is locked` 复发）→ 续跑形状锁定测试 + 并发双 run 线程实测 +
    `smoke_approval_flow.py` 双跑；
  - 中文路径 `d:\智能办公Agent` 的 Docker bind mount 有踩坑史 → compose 实测，失败退 named volume；
  - 首查全量灌库突发拖垮同机 Ollama chat → 单次 embed 上限 128 块，超出下轮续灌；
  - 换 embedding 模型 → 维度/门槛失效，collection 记 model，走显式 reindex 重建；
  - 阶段二撞并发会话 §2.6 在途文件 → 开工前 `git status` 必须干净。
- 回滚方案：每阶段独立 commit，`git revert` 即回；无模型/迁移改动（alembic check 零漂移），
  外部服务全可选、不配即现行为，无数据迁出负担。
- 可观测验证：出参 `retrieval_mode`（milvus/embedding/bigram 三值）+ `retrieval_fallback_reason`
  原样暴露；五包 pytest 基线只增不减；各阶段 HTTP 冒烟双跑。

## 5. 落地清单

- [x] ADR-0005 落盘（本文件）
- [x] 阶段一：`loop_state.py`（LoopState 宿主）+ `graph.py`（StateGraph 装配）替换 `runloop.py`，
      `runner.py` 薄装配改接图；api/approvals/checkpoint/validation/router/planner 零改动
- [x] 阶段一：runtime 测试 patch 目标迁移 + 新增图结构用例（route 裁决表驱动/续跑形状锁定/并发不串）
- [x] 阶段一：五包 pytest + 四门禁 + alembic check + 冒烟三件双跑 + 并发实测 + 浏览器 E2E
      （实测：五包 354 passed、ruff/naming/arch/alembic 全绿、approval_flow 10/10、
      personal_affairs 8/8、v1_features 17/17、并发双 run code 0 零 locked、E2E 双路径 DONE 无报错）
- [ ] 阶段二：`vector_store.py` 抽象 + Milvus 实现 + 三级降级 + `deploy/docker-compose.yml` +
      MILVUS_* 配置键 + FakeVectorStore 用例 + `smoke_milvus.py`（前置：§2.6 并发在途落库）
- [ ] 阶段三：`core/kv.py` + embedding 缓存（embed_texts 前置透明）+ 调度环 SET NX 锁 +
      REDIS_* 配置键 + compose 追加 redis + 用例
- [ ] 文档同步：README 中英技术栈与功能表、AGENTS.md §5 验证命令/§6 登记、CLAUDE.md 技术栈行改真实
- [ ] 升级路径预留：若未来走路 a（图内持久化），LangGraph checkpoint 定位为**只写不读的调试镜像**，
      `Task.checkpoint` 仍是业务 SSOT；届时需配 `LANGGRAPH_STRICT_MSGPACK`
