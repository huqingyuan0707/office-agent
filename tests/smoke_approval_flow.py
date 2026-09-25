"""审批流程智能助手 HTTP 级冒烟（PRD §2.3 三缺口，可重复执行）：

① 工具清单含 office.approval.submit（office:write 恒送审）与 office.approval.opinion（office:read 免审）；
② opinion 成稿：申请人审批说明引用字段原文；复核人同意意见带金额分级提示（6800>5000 分管副总）；
③ submit 缺必填：invoke 落单 → 批准 → 执行期诚实 1001 拒收（不落残缺台账）；
④ submit 完整（新 idem_key）：落单 → 批准 → 台账生效（ticket_id + source 溯源）；
⑤ 同 idem_key 重放：再落单 → 批准 → replayed=True 且 ticket_id 相同（绝不双单）；
⑥ 一键催办：本人催 pending 单 200 且 IM 未配置如实 not_configured（状态不变）；
   已决单拒催 4004；催完批准收尾，共享库不留 pending 单。

运行前提：`.venv\\Scripts\\python.exe -m office_agent_server`（8200）已在跑，
          且服务已加载本轮新代码（submit/opinion/urge）。
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
    return c.post(f"/agent/tools/{tool}/invoke", json={"args": args}, headers=headers).json()


def approve(c, headers, approval_id, reason="冒烟批准"):
    return c.post(f"/approvals/{approval_id}/approve", json={"reason": reason}, headers=headers)


EXPENSE = {"amount": 6800, "expense_date": "2026-09-20", "reason": "客户拜访打车与餐费"}

with httpx.Client(base_url=OFFICE, trust_env=False, timeout=30) as c:
    admin = login(c, "admin", "admin123")
    reviewer = login(c, "reviewer", "reviewer123")

    # ① 工具清单口径
    tools = {t["name"]: t for t in c.get("/agent/tools", headers=admin).json()["data"]["items"]}
    submit_spec, opinion_spec = (
        tools.get("office.approval.submit"),
        tools.get("office.approval.opinion"),
    )
    check(
        "① submit=office:write 恒送审 / opinion=office:read 免审",
        submit_spec is not None
        and submit_spec["scope"] == "office:write"
        and submit_spec["requires_approval"] is True
        and opinion_spec is not None
        and opinion_spec["scope"] == "office:read"
        and opinion_spec["requires_approval"] is False,
        str(
            {
                k: v and (v["scope"], v["requires_approval"])
                for k, v in (("submit", submit_spec), ("opinion", opinion_spec))
            }
        ),
    )

    # ② opinion 成稿（申请人说明 + 复核人同意带分级）
    r = invoke(
        c,
        admin,
        "office.approval.opinion",
        {"kind": "expense", "fields": EXPENSE, "role": "applicant"},
    )
    text_a = payload(r).get("text", "")
    check(
        "②a 申请人审批说明引用字段原文",
        r.get("code") == 0 and "客户拜访打车与餐费" in text_a and "6800" in text_a,
        text_a,
    )
    r = invoke(
        c,
        reviewer,
        "office.approval.opinion",
        {"kind": "expense", "fields": EXPENSE, "role": "reviewer", "stance": "approve"},
    )
    text_r = payload(r).get("text", "")
    check(
        "②b 复核同意意见含金额分级（6800>5000 分管副总）",
        r.get("code") == 0 and "同意" in text_r and "分管副总" in text_r,
        text_r,
    )

    # ③ submit 缺必填 → 落单 → 批准 → 执行期 1001 拒收
    r = invoke(
        c,
        admin,
        "office.approval.submit",
        {
            "kind": "expense",
            "fields": {"reason": "缺金额与日期"},
            "idem_key": f"smoke-bad-{uuid.uuid4().hex[:8]}",
        },
    )
    bad_id = (r.get("data") or {}).get("approval_id")
    check(
        "③a 缺必填仍先落审批单（pending_approval）",
        r.get("code") == 0 and r["data"]["status"] == "pending_approval",
        str(r),
    )
    decided = approve(c, reviewer, bad_id).json()
    execution = (decided.get("data") or {}).get("execution_result") or {}
    check(
        "③b 批准后执行期诚实 1001 拒收",
        decided.get("code") == 0
        and execution.get("status") == "failed"
        and execution.get("code") == 1001,
        str(execution),
    )

    # ④ submit 完整 → 批准 → 台账生效
    idem = f"smoke-ok-{uuid.uuid4().hex[:8]}"
    r = invoke(
        c, admin, "office.approval.submit", {"kind": "expense", "fields": EXPENSE, "idem_key": idem}
    )
    ok_id = (r.get("data") or {}).get("approval_id")
    check(
        "④a 完整单据 invoke 落审批单",
        r.get("code") == 0 and r["data"]["status"] == "pending_approval",
        str(r),
    )
    decided = approve(c, reviewer, ok_id).json()
    execution = (decided.get("data") or {}).get("execution_result") or {}
    result = execution.get("result") or {}
    ticket_1 = result.get("ticket") or {}
    check(
        "④b 批准后落台账（ticket_id + source 溯源）",
        decided.get("code") == 0
        and execution.get("status") == "ok"
        and ticket_1.get("ticket_id")
        and result.get("replayed") is False
        and str(result.get("source", "")).startswith("local-ledger"),
        str(execution)[:200],
    )

    # ⑤ 同 idem_key 重放 → 绝不双单
    r = invoke(
        c, admin, "office.approval.submit", {"kind": "expense", "fields": EXPENSE, "idem_key": idem}
    )
    replay_id = (r.get("data") or {}).get("approval_id")
    decided = approve(c, reviewer, replay_id).json()
    execution = (decided.get("data") or {}).get("execution_result") or {}
    result = execution.get("result") or {}
    ticket_2 = result.get("ticket") or {}
    check(
        "⑤ 同键重放 replayed=True 且返回原单（不双落账）",
        result.get("replayed") is True and ticket_2.get("ticket_id") == ticket_1.get("ticket_id"),
        str(execution)[:200],
    )

    # ⑥ 一键催办
    r = invoke(c, admin, "office.todo.create", {"title": "催办冒烟单", "priority": "low"})
    urge_id = (r.get("data") or {}).get("approval_id")
    resp = c.post(f"/approvals/{urge_id}/urge", headers=admin).json()
    data = resp.get("data") or {}
    check(
        "⑥a 本人催 pending 单 200，IM 未配置如实 not_configured 且状态不变",
        resp.get("code") == 0
        and data.get("status") == "pending"
        and (data.get("im") or {}).get("sent") is False
        and (data.get("im") or {}).get("reason") == "not_configured"
        and "未送达" in data.get("note", ""),
        str(resp)[:200],
    )
    approve(c, reviewer, urge_id)
    resp = c.post(f"/approvals/{urge_id}/urge", headers=admin).json()
    check("⑥b 已决单拒催（4004）", resp.get("code") == 4004, str(resp))

total, failed = 10, len(failures)
print(
    f"RESULT: {total - failed}/{total} checks passed"
    + ("" if not failed else f"，失败：{failures}")
)
sys.exit(0 if not failed else 1)
