# office-agent — A Domain-Agnostic Intelligent Office Agent Platform

> The four hard problems that block production Agents — **permissions, approvals, audit, and data semantics** — are packaged as a domain-agnostic governance kernel: tool registry → scope-aware executor (timeout / circuit breaker / idempotency) → approval gate (every write is always routed for approval) → append-only audit with end-to-end `trace_id`.
> On top of the kernel sits an **agent orchestration runtime**: a natural-language goal is planned into a chain of tool calls, executed step by step, and returned with provenance. The planner can only *propose* — every step goes through the executor, so governance cannot be bypassed.
> Office scenarios and external systems are plugin packages; the main packages carry zero domain-specific semantics.

Design records: `docs/office-agent仓库骨架与内核提取方案.md`, `docs/ADR-0003-办公Agent拆分独立开源项目.md` (Chinese); orchestration plan: `.trae/documents/智能体编排层实现方案.md`; AI collaboration rules: `AGENTS.md`.

## Architecture

```
User goal (natural language)
   │
   ▼
packages/runtime ── orchestration layer (optional; RUNTIME_ENABLED=false → pure-kernel mode)
   ├─ spec.py + loader.py   declarative AgentSpec (plugins/*/agent.yaml — customization without code)
   ├─ planner/              LlmFunctionCallPlanner (OpenAI-compatible) → RulePlanner fallback (works with zero LLM)
   ├─ runner.py             main loop: propose → execute → observe → checkpoint; max_steps cap; approval suspend + lazy resume
   └─ validation.py         answer numbers must trace back to tool output, otherwise flagged as unverified
   │
   ▼  each proposed step is restricted to (role-visible scopes ∩ agent whitelist)
packages/core ── governance kernel (frozen; runtime consumes public API only)
   ├─ registry.py   tool registry (ToolSpec + JSON Schema + Scope; duplicate names rejected)
   ├─ policy.py     scope auth / approval routing / idempotency keys
   ├─ executor.py   hard timeout / circuit breaker / full audit / trace_id
   ├─ linkage/      cross-system HTTP bridge (provider registry, credentials via env only)
   └─ audit.py      audit event seam (persistence provided by the shell, append-only)
   │
   ▼
packages/server ── FastAPI shell (auth / rbac / tasks / approvals / governance)
   + packages/tools-office ── built-in office tools (schedule.view / report.generate / todo.create)
   + plugins/tools-ecommerce ── optional read-only remote bridge (absent when provider unconfigured)
   + apps/web ── Vue3 console (login / tools / tasks / approvals / governance / agent run panel)
```

## Repository Layout

```
office-agent/
├── packages/
│   ├── core/            office_agent_core: contracts/registry/policy/executor/linkage/audit
│   ├── server/          office_agent_server: FastAPI shell (/api/v1, unified envelope)
│   ├── runtime/         office_agent_runtime: orchestration runtime mounted via mount(app)
│   └── tools-office/    office_agent_tools_office: built-in office tool package
├── plugins/
│   ├── tools-ecommerce/          read-only remote bridge tools (order/logistics/stock/coupon/kb)
│   ├── daily-report-assistant/agent.yaml   office demo agent (rule planner + approval-suspend demo)
│   └── ecommerce-assistant/agent.yaml      external-tool integration demo (whitelist only, zero code)
├── apps/web/            Vue3 + TS + Vite console (six routed views)
├── alembic/             database migrations (single entry point for schema evolution)
├── office_agent/        early M0/M1 single-package demo (HTTP smoke tests target it on 8201)
├── tests/               HTTP-level smoke scripts
├── docs/                ADRs and design plans (Chinese)
└── skills/ githooks/    quality-gate scripts and commit hooks
```

## Quick Start

Requires Python 3.11+ (`StrEnum` in core; packages' tests won't run on 3.10).

```bash
# 1. Dependencies + the four packages (editable install)
python -m venv .venv
.venv\Scripts\pip.exe install -r requirements.txt
.venv\Scripts\pip.exe install -e packages/core -e packages/server -e packages/runtime -e packages/tools-office

# 2. Configuration (zero hard-coding; single source: packages/core/office_agent_core/settings.py)
copy .env.example .env

# 3. Schema (alembic is the only entry for schema evolution of existing databases)
.venv\Scripts\alembic.exe upgrade head

# 4. Start backend (127.0.0.1:8200; /api/v1/agents and /api/v1/runs mounted automatically)
.venv\Scripts\python.exe -m office_agent_server
```

Web console:

```bash
cd apps\web
npm install
npm run dev        # http://127.0.0.1:5173, backend address configurable on the login page
```

Seed accounts (created when `SEED_ON_START=true`; must be disabled in production):

| Account | Password | Roles | Purpose |
|---|---|---|---|
| admin | admin123 | `*` (all scopes) | invoke tools, start runs |
| reviewer | reviewer123 | admin + approver | approval duty (approver ≠ submitter red line) |

## Five-Minute Demo (no LLM required)

```bash
# 1. Login for a token
curl.exe -s -X POST http://127.0.0.1:8200/api/v1/auth/login -H "Content-Type: application/json" -d '{\"username\":\"admin\",\"password\":\"admin123\"}'

# 2. Start a run: the daily-report agent plans two steps from agent.yaml rules (schedule.view → report.generate)
curl.exe -s -X POST http://127.0.0.1:8200/api/v1/runs -H "Authorization: Bearer <token>" -H "Content-Type: application/json" -d '{\"agent\":\"daily-report-assistant\",\"goal\":\"生成今天的工作日报\"}'

# 3. Run detail: step timeline (tool / args / result digest / trace_id / planner_source)
curl.exe -s http://127.0.0.1:8200/api/v1/runs/<run_id> -H "Authorization: Bearer <token>"

# 4. Governance demo: goal "帮我建个待办" → todo.create is always routed for approval → run suspends in WAITING_APPROVAL.
#    Approve as reviewer → the next run query lazily resumes it, with full output and audit in the timeline.
```

## API Overview

Unified envelope `{code, msg, data, trace_id}`; `code == 0` means success; auth via `Authorization: Bearer <JWT>`.

| Method | Path (prefix /api/v1) | Notes |
|---|---|---|
| POST | /auth/login | exchange credentials for a JWT (login errors never reveal account existence) |
| GET | /agent/tools | tools visible under the current role's scopes (with JSON Schema) |
| GET | /agent/tools/{name} | single tool detail |
| POST | /agent/tools/{name}/invoke | invoke a tool; approval-required tools only create an approval ticket |
| GET | /tasks | task execution records (includes agent.run) |
| GET | /approvals | approval tickets |
| POST | /approvals/{id}/approve | approve (approver ≠ submitter enforced) |
| POST | /approvals/{id}/reject | reject with comment; run converges to a terminal state |
| GET | /governance/status | governance status (tool count / circuit breaker / observability counters) |
| GET | /agents | available agents (rescans plugins/*/agent.yaml per request — hot reload) |
| POST | /runs | `{agent, goal}` — accepted synchronously; execution errors surface in the timeline, never a 500 |
| GET | /runs/{id} | run status + step timeline (suspended approvals resolved inline; approved → resume) |
| POST | /runs/{id}/resume | explicit checkpoint resume (replays the persisted plan) |
| GET | /health, /ready | probes (unauthenticated) |

Error-code ranges: `1xxx` generic (1001 param / 1002 unauthenticated / 1003 forbidden / 1004 not found / 1005 quota), `2xxx` model (2000 LLM failed), `4xxx` task & tool (4003 approval required / 4004 denied / 4005 tool missing / 4006 scope denied / 4007 circuit open / 4008 call failed), `5xxx` system (5000 internal / 5001 upstream failed).

## A Custom Agent = One YAML File

`plugins/*/agent.yaml` is all that "customization" means — prompt, tool whitelist, step cap, planning rules; zero prompts and zero business words in the main packages:

```yaml
name: daily-report-assistant
tools: [office.schedule.view, office.report.generate, office.todo.create]  # names outside the registry are rejected
max_steps: 6
llm: ""                # empty → rule planner; set a profile name once LLM_PROVIDERS is configured
rules:                 # rule plan (also the LLM-outage fallback)
  - match: ["日报", "今天做了什么"]
    steps:
      - tool: office.schedule.view
        args: {view_type: daily}
      - tool: office.report.generate
        args: {title: 工作日报, metrics: {日程天数: "{steps[0].result.count}"}}  # pull values from the previous step
```

- **Fallback chain**: LLM unconfigured / call failed / no tool_calls → use `rules` if present, otherwise fail with an actionable Chinese error — never silently fabricate.
- **Whitelist intersection**: candidates = AgentSpec whitelist ∩ role-visible scopes; out-of-scope proposals are rejected at runtime.
- **External tools without code**: `plugins/ecommerce-assistant/agent.yaml` whitelists remote bridge tools (`order.query`, …) under identical governance.

## Cross-System Data Linkage (optional plugin, absent by default)

External capabilities arrive via `plugins/tools-ecommerce` whitelisted tools. Discipline (single source: skeleton plan §7):

- **Data never leaves its domain**: HTTP + service-account bearer token only; never a direct database connection. The peer system treats office-agent as an ordinary user (`X-On-Behalf-Of` forwards the initiator).
- **Configuration**: `LINKAGE_PROVIDERS={"ecommerce":{"base_url":"http://127.0.0.1:8000","token":"<service-account token>"}}` in `.env`. Unconfigured provider or offline peer → remote tools are simply not registered (naturally absent from listings); never degraded into fake data.
- **Provenance**: every fetch carries `source_endpoint + fetched_at`; generated reports annotate each number with `(来源: input.xxx)`.
- **Writes always go through approval**: idempotency key `idem_key` is forwarded to the peer to prevent double-submit; `trace_id` spans systems for troubleshooting.
- **Credentials only via env/secret, never stored**; a CI grep gate forbids business-domain tokens anywhere under `packages/`.

## Database Migrations

`alembic` is the single entry point for schema evolution: async `env.py`, URL sourced only from `Settings.DATABASE_URL`. Model column changes must ship as `autogenerate` migrations (`create_all` never ALTERs); CI runs `alembic check` for drift.

## Quality Gates (same commands locally and in CI)

```bash
.venv\Scripts\ruff.exe check .                                   # lint (ruff pinned 0.16.7, also in pre-commit hook)
.venv\Scripts\python.exe -m pytest packages/core packages/server packages/runtime packages/tools-office -q
python skills/naming-check/scripts/check_naming.py               # naming ratchet (debt may only shrink)
python skills/anti-shit-code/scripts/check_arch.py               # architecture health (layering / size)
.venv\Scripts\alembic.exe check                                  # zero model-vs-DB drift
grep -riE "finance|purchase|risk|sku|订单|电商" packages/         # domain-agnostic gate (allowed only in plugins/ and docs)
cd apps\web && npm run build                                     # frontend gate (includes vue-tsc)
```

After cloning, run `git config core.hooksPath githooks` once to enable commit hooks.

## Milestones

| Phase | Scope | Status |
|---|---|---|
| M0 | Repo skeleton + kernel extraction (core/server) | ✅ |
| M1 | Approval loop + office tools + web console | ✅ |
| M2 | Cross-system HTTP+JWT bridge (tools-ecommerce, pull mode verified E2E) | ✅ |
| R0-R3 | Orchestration runtime: AgentSpec / dual planners / approval suspend + resume / numeric validation / run panel | ✅ |
| M3 | mcp-bridge (standard MCP) + SPI docs | ⏳ planned |

## Documentation

- Decision record: `docs/ADR-0003-办公Agent拆分独立开源项目.md` (Chinese)
- Skeleton & extraction plan: `docs/office-agent仓库骨架与内核提取方案.md` (Chinese)
- Orchestration plan: `.trae/documents/智能体编排层实现方案.md` (Chinese)
- Contributing: `CONTRIBUTING.md` (Chinese); AI collaboration rules: `AGENTS.md`

## License

[Apache-2.0](LICENSE)
