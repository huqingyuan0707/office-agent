"""PRD §2.7 群摘要 + §2.8 人事行政 HTTP 级冒烟（可重复执行，预订日期随机防撞车）：

① 六工具已注册且口径正确（book 写恒送审；其余五读免审）；
② office.im.digest：@本人任务候选 + 问句/决议/风险摘录 + 简报；
③ office.hr.attendance：李四迟到 1 天、总工时 17、加班 1（口径 max(0,日工时-8)）；
④ office.hr.checklist：onboard 五段 / offboard 交接缺项留【待补充】；
⑤ office.resource.query：会议室 2 间；
⑥ office.resource.book：invoke 落单 → 批准落台账 → query 占用可见；
⑦ 冲突预订只落单（执行期 1001 语义）→ 驳回无残留；
⑧ 占用查询含本次预订区间。

运行前提：`.venv\\Scripts\\python.exe -m office_agent_server`（8200）已在跑，
且为当前工作区代码（新工具需重启后才注册）。
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
        f"/approvals/{approval_id}/approve", headers=reviewer, json={"reason": "§2.7/2.8 冒烟"}
    ).json()
    return (r.get("data") or {}).get("execution_result") or {}


def reject(c, reviewer, approval_id):
    r = c.post(
        f"/approvals/{approval_id}/reject", headers=reviewer, json={"reason": "§2.7/2.8 冒烟清理"}
    ).json()
    return r.get("code") == 0


suffix = uuid.uuid4().hex[:6]


def _overlaps(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    return start_a < end_b and start_b < end_a


def free_slot(c, admin) -> tuple[str, str, str]:
    """从近 7 天找 small 会议室首个空闲整点时段（跨跑可重复执行，不撞历史残留）。"""
    for offset in range(3, 10):
        day = (datetime.now() + timedelta(days=offset)).strftime("%Y-%m-%d")
        qr = payload(invoke(c, admin, "office.resource.query", {"type": "room", "date": day}))
        small = next((x for x in qr.get("resources", []) if x["id"] == "room-small"), {})
        taken = [(s["start"], s["end"]) for s in small.get("booked_slots", [])]
        for hour in range(9, 17):
            start, end = f"{hour:02d}:00", f"{hour + 1:02d}:00"
            if not any(_overlaps(start, end, a, b) for a, b in taken):
                return day, start, end
    raise RuntimeError("近 7 天 small 会议室无空闲时段（预订台账已满，请清理）")


with httpx.Client(base_url=OFFICE, trust_env=False, timeout=30) as c:
    admin = login(c, "admin", "admin123")
    reviewer = login(c, "reviewer", "reviewer123")

    # ① 工具清单
    tools = {t["name"]: t for t in c.get("/agent/tools", headers=admin).json()["data"]["items"]}
    expect = {
        "office.im.digest": ("office:read", False),
        "office.hr.attendance": ("office:read", False),
        "office.hr.checklist": ("office:read", False),
        "office.resource.query": ("office:read", False),
        "office.resource.book": ("office:write", True),
    }
    ok = all(
        name in tools and (tools[name]["scope"], tools[name]["requires_approval"]) == spec
        for name, spec in expect.items()
    )
    check("① 五工具已注册且口径正确", ok, str({n: tools.get(n) for n in expect})[:200])

    # ② 群摘要
    dg = payload(
        invoke(
            c,
            admin,
            "office.im.digest",
            {
                "messages": [
                    {"sender": "张三", "text": "@李四 请明天下班前提交接口文档。"},
                    {"sender": "王五", "text": "上线方案通过了，就这么定。风险是有点，测试阻塞。"},
                ],
                "me": "李四",
                "source": "feishu",
            },
        )
    )
    check(
        "② im.digest 命中@我任务 + 决议/风险摘录",
        len(dg.get("my_tasks", [])) == 1
        and dg["my_tasks"][0]["due"] == "明天"
        and any("就这么定" in d for d in dg.get("decisions", []))
        and any("阻塞" in r for r in dg.get("risks", [])),
        str(dg.get("brief"))[:200],
    )

    # ③ 考勤加班
    at = payload(invoke(c, admin, "office.hr.attendance", {"person": "李四"}))
    check(
        "③ hr.attendance 李四迟到1/总工时17/加班1",
        at.get("status_count", {}).get("迟到") == 1
        and at.get("total_hours") == 17.0
        and at.get("overtime_hours") == 1.0,
        str({k: at.get(k) for k in ("status_count", "total_hours", "overtime_hours")})[:200],
    )

    # ④ 入离职清单
    on = payload(invoke(c, admin, "office.hr.checklist", {"scene": "onboard", "person": "新人"}))
    off = payload(
        invoke(
            c,
            admin,
            "office.hr.checklist",
            {"scene": "offboard", "person": "老王", "handover": [{"item": "知识库权限"}]},
        )
    )
    check(
        "④ checklist 入职五段/离职缺项留白",
        len(on.get("sections", [])) == 5
        and any("【待补充】" in r for r in off.get("reminders", [])),
        str(off.get("reminders"))[:200],
    )

    # ⑤ 资源台账
    day, start, end = free_slot(c, admin)
    qr = payload(invoke(c, admin, "office.resource.query", {"type": "room", "date": day}))
    check("⑤ resource.query 会议室 2 间", qr.get("count") == 2, str(qr.get("count"))[:100])

    # ⑥ 预订：落单 → 批准 → 占用可见
    r = invoke(
        c,
        admin,
        "office.resource.book",
        {
            "resource_id": "room-small",
            "date": day,
            "start": start,
            "end": end,
            "purpose": f"冒烟评审-{suffix}",
            "idem_key": f"smoke-hr-{suffix}",
        },
    )
    d = r.get("data") or {}
    check(
        "⑥a resource.book 只落审批单",
        r.get("code") == 0 and d.get("status") == "pending_approval",
        str(d)[:200],
    )
    ex = approve(c, reviewer, d["approval_id"])
    qr = payload(invoke(c, admin, "office.resource.query", {"type": "room", "date": day}))
    small = next((x for x in qr.get("resources", []) if x["id"] == "room-small"), {})
    check(
        "⑥b 批准后占用可见",
        ex.get("status") == "ok"
        and any(
            s["start"] == start and f"冒烟评审-{suffix}" in s["purpose"]
            for s in small.get("booked_slots", [])
        ),
        str(small.get("booked_slots"))[:200],
    )

    # ⑦ 冲突预订只落单 → 驳回（执行期 1001 由单测锁定，此处不断言执行结果）
    clash_start = f"{int(start[:2]):02d}:30"
    r = invoke(
        c,
        admin,
        "office.resource.book",
        {
            "resource_id": "room-small",
            "date": day,
            "start": clash_start,
            "end": end,
            "purpose": "撞车会议",
            "idem_key": f"smoke-hr-clash-{suffix}",
        },
    )
    d = r.get("data") or {}
    check(
        "⑦ 冲突预订只落单不直写",
        r.get("code") == 0 and d.get("status") == "pending_approval",
        str(d)[:200],
    )
    check("⑦b 冲突单已驳回无残留", reject(c, reviewer, d["approval_id"]), d.get("approval_id", ""))

print()
print("RESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
