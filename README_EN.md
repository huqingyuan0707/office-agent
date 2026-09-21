# office-agent

> A domain-agnostic intelligent office Agent platform — tool registry, approval gate, long-task scheduling, and audit replay **out of the box**; office scenarios ship as plugin packages.

<p>
  <img alt="License" src="https://img.shields.io/badge/license-Apache--2.0-blue">
  <img alt="Status" src="https://img.shields.io/badge/status-M0%20skeleton-orange">
  <img alt="Python" src="https://img.shields.io/badge/python-3.11%2B-informational">
</p>

English | [简体中文](README.md)

## Project Status

The project is currently at the **M0 skeleton stage**: the repository structure and this README are in place first; kernel code, the `docker compose` demo, and the web console will be delivered milestone by milestone. Feel free to Star, open Issues, and submit PRs.

## Why It Matters

Most Agent frameworks focus on "teaching the model to call tools", but production rollouts get stuck on four hard problems: **permissions, approvals, audit, and data semantics**. office-agent turns this governance layer into a domain-agnostic, reusable kernel, so integrators can focus purely on "which tools to provide" instead of rebuilding the wheel.

## Core Capabilities

- **Tool Registry** — `ToolSpec` + `Scope` namespaces + idempotency keys + circuit breaking; plug-and-play tools.
- **Approval Gate** — all write operations are **always routed for approval**; the red line cannot be bypassed.
- **Long-Task Scheduling** — `checkpoint` / `resume` / SSE progress streaming, timeout backoff and retry.
- **Audit Replay** — end-to-end tracing via `trace_id`; every action is replayable.
- **RBAC / Tenant Isolation** — least-privilege sets; credentials only via environment variables, never stored.
- **No Fabricated Numbers** — `verify_numbers` post-validation; when the LLM is unavailable, fall back to a numeric-only template — **degraded, never a 500**.

## Repository Layout

```
office-agent/
├── LICENSE                        # Apache-2.0
├── README.md / README_EN.md       # Bilingual docs
├── docker-compose.yml             # Five-minute demo (planned)
├── packages/
│   ├── core/                      # Agent kernel: contracts/registry/policy/executor/runtime
│   ├── server/                    # FastAPI shell: auth/RBAC/tasks/approvals/audit/governance-status
│   ├── tools-office/              # Built-in office tool package
│   └── mcp-bridge/                # MCP wire protocol bridge
├── plugins/
│   └── _template/                 # Custom tool plugin template (with SPI docs)
└── apps/web/                      # Vue3 console: login/tasks/approvals/audit pages
```

## Quick Start (planned)

```bash
# Available from M1
docker compose up -d
# Open http://localhost:8000 to try the report generation demo
```

## Built-in Office Tool Package (packages/tools-office)

| Layer | Tools | Scope | Notes |
|---|---|---|---|
| Read | `report.generate` / `bi.query` | `office:read` | Data sources are user-configured read-only connector whitelists; numeric consistency ≥99% |
| Read | `doc.summarize` / `kb.query` | `office:read` | Built-in RAG; SQLite + built-in vectors by default, switchable to pgvector |
| Read | `schedule.view` | `office:read` | Read-only calendar |
| Write | `todo.create` / `schedule.book` / `doc.draft` / `ticket.create` / `announce.draft` | `office:write` | **All always routed for approval**; `doc.draft` drafts only, never publishes; `idem_key` required |

## Domain Agnosticism

The main packages under `packages/` contain **no domain-specific business semantics**. This is enforced by a grep gate in CI:

```bash
grep -riE "finance|purchase|risk|sku" packages/  # allowed only in plugins/ and doc examples
```

## Roadmap

| Phase | Scope | Acceptance |
|---|---|---|
| **M0** | Skeleton + kernel extraction + green ported tests | py_compile / ruff / pytest pass; domain-agnostic grep PASS |
| **M1** | Server shell + tools-office read layer + docker compose demo | Report generation running in five minutes; LLM outage falls back to numeric templates |
| **M2** | Approval gate + write-layer tools + four web pages | Inspection → ticket → approval E2E; zero prompt-injection hits |
| **M3** | mcp-bridge + first external plugin + SPI docs | External consumer registered successfully via MCP |

## Extending with Plugins

Build custom tools from `plugins/_template/` and register a `ToolSpec` through the SPI to hook into the kernel — no changes to the main packages required. See the SPI docs under `docs/` (delivered in M3).

## Documentation

- Decision record: [docs/ADR-0003-办公Agent拆分独立开源项目.md](docs/ADR-0003-办公Agent拆分独立开源项目.md) (Chinese)
- Skeleton & extraction plan: [docs/office-agent仓库骨架与内核提取方案.md](docs/office-agent仓库骨架与内核提取方案.md) (Chinese)

## Contributing

Zero hard-coding (Settings/.env pattern), follow the existing code style, and PRs must pass CI (pytest + ruff + domain-agnostic grep). See [CONTRIBUTING.md](CONTRIBUTING.md) (Chinese); a Code of Conduct is coming soon.

## Security

Please do not report vulnerabilities publicly. Refer to `SECURITY.md` (coming soon) to disclose privately. Bundled demo data contains no PII.

## License

Released under the [Apache-2.0](LICENSE) license.
