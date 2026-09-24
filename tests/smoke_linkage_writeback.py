"""联动模式②跨系统 E2E 冒烟：
① office admin invoke ticket.create（恒送审）→ pending；
② reviewer 批准 → decide_approval 以申请人角色 executor.call → linkage 出站 → EC 网关建单（replayed=False）；
③ 同 idem_key 再走一遍 invoke+approve → EC 幂等回放（replayed=True，同一 ticket_id）→ 绝不双单；
④ 直查 EC 库：同键有且只有一单。
"""

import sqlite3
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


def flow(c: httpx.Client, admin: dict, reviewer: dict, idem_key: str) -> dict:
    """invoke → approve，返回 execution_result。"""
    r = c.post(
        "/agent/tools/ticket.create/invoke",
        headers=admin,
        json={
            "args": {
                "kind": "office-callback",
                "source_ref": "smoke-mode2",
                "assignee": "cs1",
                "sla_hours": 24,
                "idem_key": idem_key,
            }
        },
    ).json()
    assert r.get("code") == 0, r
    d = r["data"]
    assert d["status"] == "pending_approval" and d["approval_required"], d
    r2 = c.post(
        f"/approvals/{d['approval_id']}/approve", headers=reviewer, json={"reason": "模式②冒烟"}
    ).json()
    assert r2.get("code") == 0, r2
    return r2["data"]["execution_result"]


with httpx.Client(base_url=OFFICE, trust_env=False, timeout=30) as c:
    admin = login(c, "admin", "admin123")
    reviewer = login(c, "reviewer", "reviewer123")
    check("① 双账号登录 office-agent", True)

    tools = c.get("/agent/tools", headers=admin).json()["data"]["items"]
    tc = next((t for t in tools if t["name"] == "ticket.create"), None)
    check(
        "② 工具清单含 ticket.create（ticket:write，需审批）",
        tc is not None and tc["requires_approval"] is True and tc["scope"] == "ticket:write",
    )

    idem = f"smoke-mode2-{uuid.uuid4().hex[:8]}"
    ex1 = flow(c, admin, reviewer, idem)
    # office 内核 data.result = EC 网关 outcome 信封（其 .result 才是单据字段：双层 result）
    r1 = (ex1.get("result") or {}).get("result") or {}
    check(
        "③ 批准后出站建单成功（status=ok，replayed=False）",
        ex1.get("status") == "ok" and r1.get("replayed") is False,
        str(ex1)[:400],
    )
    ticket_id = r1.get("ticket_id", "")
    check("④ EC 返回真实 ticket_id", bool(ticket_id), str(ex1)[:200])

    ex2 = flow(c, admin, reviewer, idem)
    r2 = ((ex2.get("result") or {}).get("result")) or {}
    check(
        "⑤ 同 idem_key 重放：replayed=True 且同一单（绝不双单）",
        ex2.get("status") == "ok"
        and r2.get("replayed") is True
        and r2.get("ticket_id") == ticket_id,
        str(ex2)[:400],
    )

# 直查 EC 库确认单据数
conn = sqlite3.connect(r"d:\多模态智能电商客服系统\backend\dev.db")
n = conn.execute("select count(*) from tickets where idem_key=?", (idem,)).fetchone()[0]
row = conn.execute(
    "select id, kind, status, assignee from tickets where idem_key=?", (idem,)
).fetchone()
check("⑥ EC 库同键只有一单且字段落库正确", n == 1 and row[1] == "office-callback", str((n, row)))

print()
print("RESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
