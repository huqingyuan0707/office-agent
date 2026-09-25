"""PRD §2.9 项目台账/拆解 + §2.10 财务 + §2.11 通用工具 HTTP 级冒烟（可重复执行）：

① 六工具已注册且口径正确（desk.ticket 写恒送审；其余读免审；commit 写恒送审已在册）；
② office.project.query：按责任人过滤 + 简报含进度与当前里程碑；
③ office.task.decompose：无参与人工期追问 + 五阶段子任务；
④ office.task.commit：落单 → 批准执行（无责任人不通知）；
⑤ office.finance.reimburse：张三 2 单、在途 3200.5；
⑥ office.finance.expense：产品部 9 月总额 92000 + 官网改版剩余额度联带；
⑦ office.desk.ticket：落单 → 批准落台账；
⑧ office.desk.tickets：按 kind 过滤可见本次工单。

运行前提：`.venv\\Scripts\\python.exe -m office_agent_server`（8200）已在跑，
且为当前工作区代码（新工具需重启后才注册）。
"""

import sys
import uuid

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
OFFICE = "http://127.0.0.1:8200/api/v1"

failures: list[str] = []


def check(step: str, cond: bool, detail: str = "") -> None:
    print(
        ("PASS | " if cond else "FAIL | ") + step + (f"（{detail}）" if detail and not cond else "")
    )
    if not cond:
        failures.append(step)


def login(c: httpx.Client, u: str, p: str) -> dict:
    r = c.post("/auth/login", json={"username": u, "password": p}).json()
    return {"Authorization": f"Bearer {r['data']['token']}"}


def payload(r: dict) -> dict:
    """invoke 信封出参在 data.result（data 外层是 tool/status/args 等元信息）。"""
    return (r.get("data") or {}).get("result") or {}


def invoke(c, headers, tool, args):
    return c.post(f"/agent/tools/{tool}/invoke", headers=headers, json={"args": args}).json()


def approve(c, reviewer, approval_id):
    r = c.post(
        f"/approvals/{approval_id}/approve", headers=reviewer, json={"reason": "§2.9-2.11 冒烟"}
    ).json()
    return (r.get("data") or {}).get("execution_result") or {}


suffix = uuid.uuid4().hex[:6]

with httpx.Client(base_url=OFFICE, trust_env=False, timeout=30) as c:
    admin = login(c, "admin", "admin123")
    reviewer = login(c, "reviewer", "reviewer123")

    # ① 工具清单
    tools = {t["name"]: t for t in c.get("/agent/tools", headers=admin).json()["data"]["items"]}
    expect = {
        "office.project.query": ("office:read", False),
        "office.finance.reimburse": ("office:read", False),
        "office.finance.expense": ("office:read", False),
        "office.desk.ticket": ("office:write", True),
        "office.desk.tickets": ("office:read", False),
        "office.task.commit": ("office:write", True),
    }
    ok = all(
        name in tools and (tools[name]["scope"], tools[name]["requires_approval"]) == spec
        for name, spec in expect.items()
    )
    check("① 六工具已注册且口径正确", ok, str({n: tools.get(n) for n in expect})[:200])

    # ② 项目台账
    pq = payload(invoke(c, admin, "office.project.query", {"owner": "产品经理"}))
    check(
        "② project.query 责任人过滤 + 简报",
        pq.get("count") == 1
        and "上线发布" in (pq.get("brief") or "")
        and pq["projects"][0]["current_milestone"] == "上线发布（doing）",
        str(pq.get("brief"))[:200],
    )

    # ③ 拆解（缺参与人/工期 → 追问 + 五阶段）
    dc = payload(invoke(c, admin, "office.task.decompose", {"goal": "冒烟官网改版"}))
    check(
        "③ decompose 缺参追问 + 五阶段子任务",
        dc.get("total") == 5 and len(dc.get("missing_info", [])) >= 2,
        str(dc.get("missing_info"))[:200],
    )

    # ④ 批量建单（无责任人不通知）
    r = invoke(
        c,
        admin,
        "office.task.commit",
        {"tasks": [{"title": f"冒烟子任务-{suffix}"}], "idem_key": f"smoke-ops-{suffix}"},
    )
    d = r.get("data") or {}
    ex = approve(c, reviewer, d.get("approval_id", ""))
    check(
        "④ commit 批准执行且无通知人",
        ex.get("status") == "ok" and ex.get("notified_owners", []) == [],
        str(ex)[:200],
    )

    # ⑤ 报销进度
    rb = payload(invoke(c, admin, "office.finance.reimburse", {"person": "张三"}))
    check(
        "⑤ reimburse 张三 2 单在途 3200.5",
        rb.get("count") == 2 and rb.get("transit_amount") == 3200.5,
        str({k: rb.get(k) for k in ("count", "transit_amount")})[:200],
    )

    # ⑥ 费用统计 + 预算联带
    ex2 = payload(
        invoke(c, admin, "office.finance.expense", {"department": "产品部", "month": "2026-09"})
    )
    check(
        "⑥ expense 总额 92000 + 预算联带",
        ex2.get("total") == 92000.0
        and ex2.get("budgets", {}).get("官网改版", {}).get("remaining") == 74000.0,
        str({k: ex2.get(k) for k in ("total", "budgets")})[:200],
    )

    # ⑦ 工单：落单 → 批准
    title = f"冒烟报修-{suffix}"
    r = invoke(
        c,
        admin,
        "office.desk.ticket",
        {"kind": "it_repair", "title": title, "detail": "冒烟", "idem_key": f"smoke-desk-{suffix}"},
    )
    d = r.get("data") or {}
    check(
        "⑦a desk.ticket 只落审批单",
        r.get("code") == 0 and d.get("status") == "pending_approval",
        str(d)[:200],
    )
    ex = approve(c, reviewer, d["approval_id"])
    lt = payload(invoke(c, admin, "office.desk.tickets", {"kind": "it_repair"}))
    visible = any(t["title"] == title for t in lt.get("tickets", []))
    check(
        "⑦b 批准后落台账（清单可见）",
        ex.get("status") == "ok" and visible,
        str(lt.get("count"))[:200],
    )

    # ⑧ 清单过滤
    check(
        "⑧ tickets 按 kind 过滤计数≥1",
        lt.get("count", 0) >= 1,
        str(lt.get("count"))[:200],
    )

print()
print("RESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
