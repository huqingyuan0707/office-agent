"""PRD §2.2 个人事务智能管理 HTTP 级冒烟（可重复执行，标题带随机后缀互不干扰）：

① 六新工具已注册且口径正确（list/freebusy/worklog 只读免审；update/delete/schedule.create 恒送审）；
② office.todo.create：invoke 只落审批单 → reviewer 批准 → 真实落事务存储（todo.list 可见）；
③ office.todo.update：标记 done 批准后生效（open 清单不再含、done 清单含）；
④ office.schedule.create：会议批准后落盘 → office.schedule.freebusy 实测明日 10:00-11:00 忙碌；
⑤ office.worklog.generate：周台账含已完成事项计数；
⑥ 通知扫描 affairs_degraded=false（④~⑦ 信号链在线）；
⑦ office.todo.delete：批准后从存储移除（all 清单不再含该 id）。

运行前提：`.venv\\Scripts\\python.exe -m office_agent_server`（8200）已在跑。
"""

import sys
import uuid
from datetime import datetime, timedelta

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
        f"/approvals/{approval_id}/approve", headers=reviewer, json={"reason": "§2.2 冒烟"}
    ).json()
    return (r.get("data") or {}).get("execution_result") or {}


def find_todo(c, admin, title, status="all"):
    d = payload(invoke(c, admin, "office.todo.list", {"status": status}))
    return next((t for t in d.get("items", []) if t.get("title") == title), None)


today = datetime.now().strftime("%Y-%m-%d")
tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
suffix = uuid.uuid4().hex[:6]
todo_title = f"冒烟待办-{suffix}"
meet_title = f"冒烟评审会-{suffix}"

with httpx.Client(base_url=OFFICE, trust_env=False, timeout=30) as c:
    admin = login(c, "admin", "admin123")
    reviewer = login(c, "reviewer", "reviewer123")

    # ① 工具清单
    tools = {t["name"]: t for t in c.get("/agent/tools", headers=admin).json()["data"]["items"]}
    expect = {
        "office.todo.list": ("office:read", False),
        "office.todo.update": ("office:write", True),
        "office.todo.delete": ("office:write", True),
        "office.schedule.create": ("office:write", True),
        "office.schedule.freebusy": ("office:read", False),
        "office.worklog.generate": ("office:read", False),
    }
    ok = all(
        name in tools and (tools[name]["scope"], tools[name]["requires_approval"]) == spec
        for name, spec in expect.items()
    )
    check(
        "① 六新工具已注册且口径正确",
        ok,
        str(
            {n: tools.get(n) and (tools[n]["scope"], tools[n]["requires_approval"]) for n in expect}
        ),
    )

    # ② 建待办：落单 → 批准 → 真实落盘
    r = invoke(
        c,
        admin,
        "office.todo.create",
        {"title": todo_title, "priority": "high", "due_date": today},
    )
    d = r.get("data") or {}
    check(
        "②a todo.create 只落审批单",
        r.get("code") == 0 and d.get("status") == "pending_approval",
        str(d)[:200],
    )
    ex = approve(c, reviewer, d["approval_id"])
    rec = find_todo(c, admin, todo_title)
    check(
        "②b 批准后真实落盘（open 清单可见）",
        ex.get("status") == "ok" and rec is not None,
        str(ex)[:200],
    )

    # ③ 标记完成
    r = invoke(c, admin, "office.todo.update", {"todo_id": rec["id"], "status": "done"})
    ex = approve(c, reviewer, (r.get("data") or {})["approval_id"])
    check(
        "③ todo.update 批准后 done 生效（completed_at 落值）",
        ex.get("status") == "ok"
        and find_todo(c, admin, todo_title, "open") is None
        and (find_todo(c, admin, todo_title, "done") or {}).get("completed_at"),
        str(ex)[:200],
    )

    # ④ 建会议 + 空闲查询
    r = invoke(
        c,
        admin,
        "office.schedule.create",
        {
            "title": meet_title,
            "kind": "meeting",
            "start": f"{tomorrow} 10:00",
            "duration_minutes": 60,
            "attendees": ["reviewer"],
        },
    )
    ex = approve(c, reviewer, (r.get("data") or {})["approval_id"])
    fb = payload(invoke(c, admin, "office.schedule.freebusy", {"date": tomorrow}))
    mine = next((p for p in fb.get("persons", []) if p["person"] == "admin"), {})
    check(
        "④ schedule.create 批准后落盘且 freebusy 实测明日 10:00-11:00 忙碌",
        ex.get("status") == "ok"
        and any(b["from"] == "10:00" and b["to"] == "11:00" for b in mine.get("busy", [])),
        str(mine)[:200],
    )

    # ⑤ 工作台账
    wl = payload(invoke(c, admin, "office.worklog.generate", {"period": "weekly"}))
    check(
        "⑤ worklog 周窗口含已完成计数与标题",
        wl.get("counts", {}).get("done", 0) >= 1 and todo_title in wl.get("worklog", ""),
        str(wl.get("counts"))[:200],
    )

    # ⑥ 通知扫描链在线（事务存储可读）
    sc = c.post("/notifications/scan", headers=admin).json()
    check(
        "⑥ notifications.scan 无降级（affairs_degraded=false）",
        sc.get("code") == 0 and sc["data"]["affairs_degraded"] is False,
        str(sc["data"])[:200],
    )

    # ⑦ 删除待办
    r = invoke(c, admin, "office.todo.delete", {"todo_id": rec["id"]})
    ex = approve(c, reviewer, (r.get("data") or {})["approval_id"])
    check(
        "⑦ todo.delete 批准后从存储移除",
        ex.get("status") == "ok" and find_todo(c, admin, todo_title) is None,
        str(ex)[:200],
    )

print()
print("RESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
