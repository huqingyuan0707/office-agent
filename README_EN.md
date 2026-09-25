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
   + packages/mcp-bridge ── standard MCP protocol bridge (optional; transport="mcp" providers auto-discovered)
   + packages/tools-office ── built-in office tools (schedule / reports / minutes / knowledge / OCR / doc compare / task planner / templates)
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
│   ├── mcp-bridge/      office_agent_mcp_bridge: standard MCP bridge (this side is the Client, JSON-RPC over HTTP)
│   └── tools-office/    office_agent_tools_office: built-in office tool package
├── plugins/
│   ├── tools-ecommerce/          read-only remote bridge tools (order/logistics/stock/coupon/kb)
│   ├── daily-report-assistant/agent.yaml   office demo agent (rule planner + approval-suspend demo)
│   ├── office-assistant/agent.yaml         cross-domain demo agent (one sentence chains data query → report → policy Q&A)
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
# 1. Dependencies + the five packages (editable install)
python -m venv .venv
.venv\Scripts\pip.exe install -r requirements.txt
.venv\Scripts\pip.exe install -e packages/core -e packages/server -e packages/runtime -e packages/tools-office -e packages/mcp-bridge

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
| GET | /notifications | own in-app notifications (login is enough; unread_only filter) |
| POST | /notifications/scan | generate notifications (approval-stale / task-failed / daily briefing; admin/approver, idempotent rescans) |
| POST | /notifications/{id}/read | mark as read (idempotent receipt) |
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
- **Pull (mode 1) and write-back (mode 2) both verified**: read tools (order/logistics/stock/coupon/kb) fetch with provenance; the first write-back tool `ticket.create` (ticket:write) — invoke always files an approval; once approved it executes outbound as the applicant, and the peer replays idempotently on `(tenant, idem_key)` (same key returns the same ticket, never a duplicate). HTTP-level smoke: `tests/smoke_linkage_writeback.py` 6/6.

### M3: Standard MCP Protocol Bridge (optional, active once installed)

When a peer upgrades from the custom HTTP gateway to a **standard MCP Server**, only one config entry changes — zero business code:

- **Config-based routing**: add `transport` to a `LINKAGE_PROVIDERS` entry (default `"http"` is claimed by the kernel, `"mcp"` by mcp-bridge) — the kernel contains no protocol branches, keeping protocol details out of the main package. See `.env.example`.
- **Implementation**: this side is the MCP **Client**, hand-rolled JSON-RPC 2.0 over HTTP (protocol `2025-06-18`; methods `initialize` / `tools/list` / `tools/call`), no MCP SDK. `MCPProvider` is **shape-identical** to the HTTP gateway client (`provider_id` + `invoke()` + `aclose()`); registered via `linkage.register_provider()` → registry / executor / approvals / audit / provenance **unchanged**.
- **Automatic tool registration**: `tools/list` on startup → governance attributes mapped from MCP annotations: `readOnlyHint=true` → read-only scope + no approval + idempotent; **missing annotation or non-read-only → always approved** (strict by default when unknown).
- **Degradation**: an offline peer only logs a warning and registers nothing — startup is never blocked; error classification is shared with the HTTP gateway (`core/linkage/envelope.py`: upstream 1xxx/3xxx/4xxx pass through verbatim, 5xxx/invalid codes become dependency failures).

### IM Approval Notifications (optional, absent when unconfigured)

When an approval is filed or a stale-approval scan finds new items, a group-bot message goes out (`generic` / `feishu` / `dingtalk` / `wecom` bodies plus Feishu/DingTalk signing):

- Hooked at "approval filed" and "stale scan" as a **side channel** — never enters linkage, never counts toward circuit breaking, never mutates approval state.
- With `IM_WEBHOOK_URL` unset it returns `{"sent": false, "reason": "not_configured"}` and makes **no network call at all**.
- Failures/timeouts are fully caught and degraded to a recorded event (`agent.im_webhook`), **never blocking the approval flow**; stale reminders aggregate the first N items to avoid spamming.

HTTP-level smoke: `python tests/smoke_mcp_im.py` (bundled fake MCP server + fake IM endpoint, 5 assertions).

## V1.0 Office Features (PRD §5.1, all under the same governance)

| Feature | Tool / endpoint | Key guarantees |
|---|---|---|
| Copywriting (daily/weekly/minutes) | `office.report.generate` (daily/weekly), `office.minutes.generate` | template-driven; numbers come only from inputs; missing sections are left blank, never fabricated |
| Knowledge-base Q&A | `kb.ask` (KB_DIR `*.md/*.txt` + built-in demo entries) | dual-channel retrieval: with `EMBEDDING_*` configured it does vector semantic search (per-paragraph cosine + a similarity floor, so rephrased questions still hit); otherwise, or when the endpoint is unreachable, it falls back to character bigrams and says so in `retrieval_mode` / `retrieval_fallback_reason`; quotes matched source text only and reports `degraded` honestly on no match |
| Image OCR | `ocr.image` | real metadata; falls back to metadata-only when Tesseract is absent — never fabricates text |
| Document comparison | `office.doc.compare` | paragraph-level diff (added/removed/changed + summary), measured facts only |
| Task decomposition | `office.task.decompose` → `office.task.commit` | missing people/dates stay blank with follow-up prompts; batch creation is always approved + idem_key |
| Custom templates | `office.template.save` (approved write) → `office.template.apply` | unfilled placeholders stay literal and are listed in `unfilled` |
| Proactive notifications | `/notifications` (scan / list / read) | approval-stale / task-failed / daily briefing (extended to seven signal kinds by PRD §2.2, see "Personal affairs management"); deduped on `(tenant, username, kind, ref_id)` |

HTTP-level smoke: `python tests/smoke_v1_features.py` (17 assertions, re-runnable).

## Content Generation & Document Processing (PRD §2.1; tool side)

| Feature | Tools | Key guarantees |
|---|---|---|
| Copywriting (monthly added) | `office.report.generate` (daily/weekly/monthly) | monthly reports aggregate measured counts over the month window; numbers come only from inputs, missing sections stay blank |
| Generic drafting | `office.memo.compose` | five template kinds (notice/email/proposal/summary/briefing); missing sections stay blank, never fabricated |
| Text processing | `office.text.summarize` / `office.text.normalize` | extractive summaries (single doc + multi-doc via `texts`, verbatim sentences only) and whitespace-only normalisation; prose polishing needs an LLM and is explicitly deferred (see module footer) |
| Document parsing | `office.file.read` | docx paragraphs/tables/image list, xlsx sheet dimensions + first N rows, plain csv/txt/md, and **real page-by-page PDF text extraction via pypdf** (page count returned honestly; missing engine / encrypted / corrupted files degrade or return 1001 instead of inventing text); read scope, 404 on missing file |
| Document Q&A | `office.file.ask` | retrieves **paragraph-level** fragments from a given DOCS_DIR file and quotes source text only (so "what is X in this file" lands on the right paragraph); reuses the `retrieval.py` dual channel (vector semantic search when `EMBEDDING_*` is set — rephrased questions still hit; otherwise/by failure it falls back to character bigrams and says so in `retrieval_mode`/`retrieval_fallback_reason`); zero hits or no text layer degrade honestly. **Stated boundary**: cosine scores every text, so a garbage query still scored 0.37–0.39 in testing (the 0.35 floor is a precision/recall knob, not a clean separator) — `score` is exposed verbatim for the caller to judge |
| Batch document ops | `office.docs.rename` | batch rename (≤20 pairs) is an approved write, refuses to overwrite, replay fails honestly (idempotent=False); image extraction is folded into `office.file.read`'s image list and multi-doc summaries reuse the `texts` mode; batch PDF conversion needs system LibreOffice and is explicitly deferred |
| Custom templates | `office.template.save` → `office.template.apply` | save weekly-report/leave-note templates and reuse them; unfilled placeholders stay literal and are listed in `unfilled` |
| Internal terminology | `office.terms.translate` | built-in glossary + `DOCS_DIR/terms.csv`, deterministic longest-first replacement for term consistency; zero hits returned verbatim; full-sentence multilingual translation needs an LLM and is explicitly deferred |
| Document comparison | `office.doc.compare` | paragraph-level diff + change summary, measured facts only |

HTTP-level smoke: `python tests/smoke_file_ask.py` (7 assertions: real PDF extraction / paragraph Q&A / vector semantic channel / degradation / traversal rejection; re-runnable).

## V1.1 Office Features (PRD §5.2; read scope is approval-free, writes go through approval)

| Feature | Tools | Key guarantees |
|---|---|---|
| Self-serve data analysis | `office.data.query` / `analyze` / `export` | demo ledger + CSV overlay; stats/trends/anomalies report measured facts only; text export never writes to disk; `export` also supports `excel` (base64 .xlsx envelope, approval-free read scope, renderers centralized in `data_export.py`) |
| Saved queries & one-sentence reuse (PRD §2.4) | `office.data.query.save` / `list` / `run` / `delete` | the (dataset + filters + limit) triple lands in `DOCS_DIR/data_queries/`; save/delete are approved writes with idem_key (duplicate names rejected, never silently overwritten; the duplicate check runs at approval-execution time); list/run are approval-free reads isolated per tenant; run re-queries live with the same implementation, never a stale snapshot; corrupt archives report honest 1001, never 500 |
| Automatic chart insight (PRD §2.4) | `office.data.chart_insight` | per-category stats + highest/lowest/±2σ anomaly labels pinned to category names + Chinese brief + same-data SVG bar chart (anomalies in red); images go out as plain-text SVG with zero third-party deps (deliberately avoiding matplotlib + CJK fonts — the browser renders them in system fonts); approval-free read scope, values quoted verbatim |
| Meeting collaboration | `office.meeting.agenda` / `book` / `risks` | agenda from templates; booking is an approved write; risk keyword alerts stay blank on no match |
| Approval assistant | `office.approval.draft` / `check` / `opinion` / `submit`, `office.invoice.extract` | five form kinds with required-field validation and ask-back prompts; tiered amounts (>1000 dept head / >5000 VP) + high-risk `need_confirm`; invoice fields are quoted verbatim, `degraded` on no match; deterministic drafting of application notes and review opinions (rejection requires a reason); one-click submit is an approved write with idem_key — the ticket lands in the local ledger only after review approval, same-key replay never double-books |
| Approval urging | `POST /approvals/{id}/urge` | applicant or admin sends one IM reminder for a pending ticket (side-channel: never changes approval state, never persists; honestly reports not_configured; decided tickets rejected with 4004); stale-ticket alerts stay on the notification scanner |
| Meeting full-flow supplement (PRD §2.5) | `office.meeting.materials` / `digest` / `followup` | pre-meeting pack extracts each file verbatim (a failed file is honestly flagged, the pack never breaks); in-meeting notes are classified into decisions/actions/risks by keyword rules with verbatim quotes (live audio transcription honestly deferred, needs audio infra); post-meeting action items are reconciled against the todo ledger into five states (overdue judged on the business-timezone today; no match = "not tracked", never fabricated); all read-scoped, no approval |
| Mail & message handling (PRD §2.7) | `office.mail.classify` / `reply_draft` / `action_items` / `precheck` + `office.im.digest` | four mail tools are input-driven, all approval-free reads (no real mailbox attached, so no fake data source): keyword classification into four kinds / drafts leave placeholders instead of inventing content / action items extract three fields with null fallbacks (escalation to todos goes through todo.create) / precheck reuses compliance rules plus a tone wordlist and demands confirmation on any hit; group digest takes pasted messages (counts per sender + @me task candidates + question/decision/risk quotes + brief; scheduled runs wait on §2.11); outbound auto-send is honestly deferred; the `/mail` page wires all three blocks |
| HR & office resources (PRD §2.8) | `office.hr.attendance` / `checklist` + `office.resource.query` / `book` | per-person attendance and overtime (present/late/leave days + total hours, overtime = Σmax(0, daily hours − 8); drafts go through approval.draft); onboarding/offboarding checklists rendered from templates with blanks left blank (handover reminders quote the input); resource ledger is builtin + CSV overlay, query is an approval-free read with booked slots, book is an approved write that rejects overlaps with 1001; the `/hr` page wires all three blocks |

HTTP-level smoke: `python tests/smoke_meeting_flow.py` (5 assertions, repeatable).

HTTP-level smoke: `python tests/smoke_hr_im.py` (§2.7 digest + §2.8 HR, 9 assertions, re-runnable: digest → attendance → checklist → query → book → clash-reject).

| Project ledger & task breakdown (PRD §2.9/§6) | `office.project.query` + `office.task.decompose` / `commit` | ledger carries milestones/risks/owners/progress (builtin + CSV overlay; filter by name/owner/status/risk-presence, with a Chinese brief); breakdown follows the project lifecycle with deliverables/owners/durations/priorities/dependencies, blanks plus ask-back prompts instead of invented people or dates, and commit is an approved write that doubles as re-confirmation; the `/projects` page wires ledger plus the two-step breakdown (owners editable inline) |
| Lightweight finance (PRD §2.10) | `office.finance.reimburse` / `expense` (plus existing `office.budget.query`) | per-person reimbursement progress with in-transit amounts (pending + approved-unpaid); department totals/counts/by-category plus budget-remaining linkage for matched projects; amounts are quoted verbatim, unparseable rows are skipped with a count; the `/finance` page wires all three blocks |
| Admin & logistics tickets (PRD §2.11) | `office.desk.ticket` / `tickets` | IT repair / asset claim / work order allowlist; ticket is an approved write landing in the local ledger, tickets is an approval-free read; syncing to external systems stays with linkage; new `/desk` page (list + approval-id hint on filing); scene chaining and visual orchestration live under workflows (`/workflows`); fine-grained per-tool permissions already exist as tool scopes + agent whitelists; scheduled jobs honestly wait on scheduler infra |

HTTP-level smoke: `python tests/smoke_project_ops.py` (§2.9 ledger/breakdown + §2.10 finance + §2.11 tickets, 9 assertions, re-runnable).

## V1.2 Office Features · Batch A (PRD §5.3; three tool-side items)

| Feature | Tools | Key guarantees |
|---|---|---|
| PPT generation | `office.pptx.generate` | outline (per-slide title + bullets) rendered straight into .pptx, no LLM calls; approved write + idem_key; not registered when python-pptx is missing; filename locked inside DOCS_DIR |
| Compliance scanning | `office.compliance.scan` | three deterministic rule sets: privacy data (phone / ID / bank card), absolute claims, leaked credentials; matches are quoted verbatim (credential values masked); read scope, approval-free |
| Budget query | `office.budget.query` | demo ledger + CSV overlay; remaining/usage are deterministic computations with the formula stated; sufficient/tight/over-budget labels; read scope, approval-free, with source + fetched_at provenance |

HTTP-level smoke: `python tests/smoke_v1_2_batch_a.py` (9 assertions, re-runnable).

## V1.2 Office Features · Batch B (PRD §5.3; two frontend pages, zero mock data)

| Feature | Entry | Key guarantees |
|---|---|---|
| Data visualization reports | `/reports` (Reports page) | pick 1 of 4 datasets → real `office.data.query` (columns taken from the first row, never presumed) + CSS bar chart on the numeric column (no chart lib) + `office.data.analyze` stat cards + `office.data.chart_insight` interpretation (anomaly tags + verbatim SVG rendering; a failed insight only empties its own block) + `office.data.export` preview & download in three formats (Markdown/CSV/Excel, excel restored from base64 to .xlsx); failed queries render empty, analyze/insight/export failures stay page-local — never papered over with fake data |

HTTP-level smoke: `python tests/smoke_2_4_data.py` (11 assertions, re-runnable: full save → approve → reuse → excel/SVG → delete chain).
| Admin operations dashboard | `/admin` (Admin page) + `GET /admin/overview` (admin only) | `services/admin_stats.py` read-only aggregation across four tables (users by status / tasks by status / approvals by status / Top-8 tools by calls / last 5 decided approvals); non-admin gets an honest 403, never downgraded fake data |

## Chat-first entry (one sentence in: no agent picking, no parameter filling)

| Capability | Entry | Key guarantees |
|---|---|---|
| Chat assistant | `/chat` (default home) | One sentence → `POST /runs` without `agent` triggers auto-routing (`runtime/router.py`: rule hits first; LLM agents only used as fallback when their profile is configured in `LLM_PROVIDERS`; when nobody can take it, 1001 with an actionable Chinese message listing loaded agents) → 2s polling renders the routed agent, step timeline, final answer / last-step result, and a pending-approval card linking to the approvals page; routing failures surface verbatim in the bubble, zero mock. The tools page becomes an admin debug console |

## V1.2 Office Features · Batch C (PRD §5.3; multi-scenario chaining / visual orchestration / RPA)

| Feature | Entry | Key guarantees |
|---|---|---|
| Workflow orchestration | `/workflows` (editor page) + `/workflows` CRUD and `/run` | definitions in the `workflows` table (migration-chain evolution, 20-step cap, validated both on save and before run); sequential kernel executor calls; approval-gated steps stop with pending_approval, failures stop with completed steps reported verbatim; remote-tool steps ride the same outbound chain (provenance included) — the in-repo RPA shape, no separate engine |

HTTP-level smoke: `python tests/smoke_v1_2_batch_c.py` (7 assertions, self-contained server).

## Personal Affairs Management (PRD §2.2: todos / schedules / worklog / proactive reminders)

| Capability | Tools / entry | Key guarantees |
|---|---|---|
| Todo management | `office.todo.create` / `list` / `update` / `delete` | real persistence in the local affairs store (`DOCS_DIR/data/affairs.json`, read/write locked); create upgraded from a demo receipt to a stored record; update/delete are approved writes, foreign/missing ids get an actionable 404; marking done records `completed_at` (feeds the worklog) |
| Schedules & meetings | `office.schedule.create` / `office.schedule.freebusy` | one-shot meeting creation (attendees + duration) or project milestones (approved writes); free/busy computed by subtracting the day's busy windows from working hours 09:00-18:00, never guessing availability; "invitations" are honestly in-app notifications, no external email is sent |
| Worklog | `office.worklog.generate` | personal ledger: done / pending / schedule segments aggregated over a daily/weekly window, measured counts with blanks instead of fabrication, source + fetched_at provenance |
| Proactive reminders | `/notifications/scan` extended with four signals | todo-due (approaching/overdue), meeting-upcoming (per creator + attendee), milestone-alert (within N days), Friday weekly-draft hint; the daily briefing now embeds today's due/overdue todo and meeting counts; business timezone configurable via `BUSINESS_TIMEZONE` (falls back to UTC with a warning when tzdata is missing); a corrupt affairs store only degrades `affairs_degraded`, never 500 |
| Example agent | `plugins/personal-affairs-assistant` | read-only lookups (todos / worklog / freebusy) answer directly from one sentence; creating todos/schedules goes through the always-approved chat flow (suspend → approve → resume); daily-report-assistant keywords narrowed in sync to avoid substring route hijacking |
| Cross-domain chaining | `plugins/office-assistant` | one sentence (agent omitted) auto-routes here and chains three domains inside a single run. Main path is `llm: default` (local qwen3:8b) which orchestrates steps on the fly and phrases the `kb.ask` query itself; when the LLM is unavailable it falls back to the `rules` chain — `office.data.query` → `office.report.generate` (metric values injected from the previous step's **real output** via `{steps[0].result.count}`, 100% numeric consistency) → `kb.ask` policy lookup, fully traceable. Caveat: the LLM proposes all steps in one shot without seeing prior outputs, so stats inputs may be invented — cross-step data passing stays more reliable via rules |

HTTP-level smoke: `python tests/smoke_personal_affairs.py` (8 assertions, re-runnable).

## Knowledge-Base Enhanced Retrieval (PRD §2.6: policy Q&A / doc search / permission scoping / federated search / image Q&A)

| Capability | Tools | Key guarantees |
|---|---|---|
| Policy Q&A | `kb.ask` (KB_DIR `*.md/*.txt` + 8 built-in demo entries: attendance/expense/leave/travel/HR/admin/compliance + confidential compensation) | dual-channel retrieval unchanged; new per-entry `visibility` (`public` or a role name; KB_DIR files declare it with a first-line `visibility: hr`; `*`/`admin` see everything; filtered entries are only counted in `permission_filtered`, titles never leak); hits echo their `visibility` |
| Federated search | `office.kb.search_unified` | one query searches knowledge / drive docs (DOCS_DIR, `restricted-` prefix is restricted) / own todos & schedules (identity-scoped) / approval ledger (own or admin) / project data ledgers; per-source dual-channel retrieval merged by score, verbatim quotes only; remote chat/OA sources join via linkage allow-list tools — no local source, no fabricated data |
| Image Q&A | `office.image.ask` | OCR the real text first (same engine probe as `ocr.image`), then retrieve paragraph fragments; missing engine / empty text / zero hits degrade honestly; image metadata measured by Pillow |
| Example agent | `plugins/office-assistant` | allow-list extended with `office.kb.search_unified` and `office.image.ask` so cross-domain chains can reach federated search and image Q&A |

HTTP-level smoke: `python tests/smoke_kb_26.py` (8 assertions, re-runnable; the expense query scored 0.6452 over the vector channel in testing).

## Database Migrations

`alembic` is the single entry point for schema evolution: async `env.py`, URL sourced only from `Settings.DATABASE_URL`. Model column changes must ship as `autogenerate` migrations (`create_all` never ALTERs); CI runs `alembic check` for drift.

## Quality Gates (same commands locally and in CI)

```bash
.venv\Scripts\ruff.exe check .                                   # lint (ruff pinned 0.16.7, also in pre-commit hook)
.venv\Scripts\python.exe -m pytest packages/core packages/server packages/runtime packages/tools-office packages/mcp-bridge -q
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
| V1.0 | PRD §5.1 office features (reports / minutes / knowledge / OCR / compare / planner / templates) + notifications | ✅ |
| M3 | mcp-bridge standard MCP bridge (transport routing + tools/list auto-discovery) + IM approval notifications | ✅ |

## Documentation

- Decision record: `docs/ADR-0003-办公Agent拆分独立开源项目.md`, `docs/ADR-0004-MCP协议桥与IM审批通知出站.md` (Chinese)
- Skeleton & extraction plan: `docs/office-agent仓库骨架与内核提取方案.md` (Chinese)
- Orchestration plan: `.trae/documents/智能体编排层实现方案.md` (Chinese)
- Contributing: `CONTRIBUTING.md` (Chinese); AI collaboration rules: `AGENTS.md`

## License

[Apache-2.0](LICENSE)
