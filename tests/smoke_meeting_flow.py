"""PRD §2.5 会议全流程补充三件 HTTP 级冒烟（可重复执行，标题带随机后缀互不干扰）：

① 三新工具已注册且口径正确（materials/digest/followup 全 office:read 免审）；
② office.meeting.materials：对 DOCS_DIR 真实文件抽取汇编（字数+开头摘录）；
③ 资料清单混入缺失文件：单文件如实 failed 不炸整包；全缺失 degraded 不编造；
④ office.meeting.digest：速记文本归类决议/行动/风险三类要点（原句摘录）；
⑤ office.meeting.followup：行动项×待办台账对账——建单批准后「进行中」、
   标记完成批准后「已完成」、未建单如实「未建单」。

运行前提：`.venv\\Scripts\\python.exe -m office_agent_server`（8200）已在跑；
脚本会往 Settings.DOCS_DIR 写入 meeting_demo.txt 演示文件。
"""

import sys
import uuid
from pathlib import Path

import httpx

from office_agent_core.settings import settings

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
        f"/approvals/{approval_id}/approve", headers=reviewer, json={"reason": "§2.5 冒烟"}
    ).json()
    return (r.get("data") or {}).get("execution_result") or {}


suffix = uuid.uuid4().hex[:6]
todo_title = f"冒烟会议行动项-{suffix}"
demo_name = "meeting_demo.txt"
demo_text = (
    "官网改版方案：一期范围含首页与产品页，预算 12 万，第 4 周上线。验收标准以设计稿评审通过为准。"
)
demo_path = Path(settings.DOCS_DIR).resolve() / demo_name
demo_path.parent.mkdir(parents=True, exist_ok=True)
demo_path.write_text(demo_text, encoding="utf-8")

with httpx.Client(base_url=OFFICE, trust_env=False, timeout=30) as c:
    admin = login(c, "admin", "admin123")
    reviewer = login(c, "reviewer", "reviewer123")

    # ① 工具清单
    tools = {t["name"]: t for t in c.get("/agent/tools", headers=admin).json()["data"]["items"]}
    expect = dict.fromkeys(
        ("office.meeting.materials", "office.meeting.digest", "office.meeting.followup"),
        ("office:read", False),
    )
    ok = all(
        name in tools and (tools[name]["scope"], tools[name]["requires_approval"]) == spec
        for name, spec in expect.items()
    )
    check(
        "① 三新工具已注册且全读免审",
        ok,
        str(
            {n: tools.get(n) and (tools[n]["scope"], tools[n]["requires_approval"]) for n in expect}
        ),
    )

    # ② 会前资料包：真实抽取汇编
    m = payload(
        invoke(
            c,
            admin,
            "office.meeting.materials",
            {"title": "改版评审", "files": [demo_name], "topics": ["范围走查"]},
        )
    )
    check(
        "② materials 真实抽取（字数+开头摘录）",
        m.get("counts", {}).get("file_ok") == 1
        and "官网改版方案" in m.get("pack", "")
        and demo_text[:20] in m.get("pack", ""),
        str(m.get("materials"))[:200],
    )

    # ③ 缺失文件如实 failed 不炸整包；全缺失 degraded
    m2 = payload(
        invoke(
            c,
            admin,
            "office.meeting.materials",
            {"title": "改版评审", "files": [demo_name, f"缺失-{suffix}.txt"]},
        )
    )
    states = {x["status"] for x in m2.get("materials", [])}
    m3 = payload(
        invoke(
            c,
            admin,
            "office.meeting.materials",
            {"title": "改版评审", "files": [f"缺失-{suffix}.txt"]},
        )
    )
    check(
        "③ 单文件缺失如实 failed 不炸整包；全缺失 degraded 不编造",
        states == {"ok", "failed"}
        and m2.get("degraded") is False
        and m3.get("degraded") is True
        and "不编造" in m3.get("degraded_reason", ""),
        str(m2.get("counts")) + "|" + str(m3.get("degraded_reason"))[:80],
    )

    # ④ 会中要点梳理：三类归类
    notes = "\n".join(
        [
            "决议：改版方案通过，进入排期。",
            "张三负责下周三前输出设计稿。",
            "风险：接口联调可能延期两天。",
        ]
    )
    d = payload(invoke(c, admin, "office.meeting.digest", {"notes": notes, "title": "评审会"}))
    cnt = d.get("counts", {})
    check(
        "④ digest 归类决议/行动/风险各≥1 且原句摘录",
        cnt.get("decision", 0) >= 1
        and cnt.get("action", 0) >= 1
        and cnt.get("risk", 0) >= 1
        and "决议：改版方案通过" in d.get("digest", ""),
        str(cnt)[:200],
    )

    # ⑤ 会后跟进对账：未建单 → 建单进行中 → 完成
    f0 = payload(
        invoke(
            c,
            admin,
            "office.meeting.followup",
            {"action_items": [{"task": todo_title, "owner": "张三"}]},
        )
    )
    r = invoke(c, admin, "office.todo.create", {"title": todo_title, "priority": "high"})
    approve(c, reviewer, (r.get("data") or {})["approval_id"])
    f1 = payload(
        invoke(
            c,
            admin,
            "office.meeting.followup",
            {"action_items": [{"task": todo_title, "owner": "张三"}]},
        )
    )
    lst = payload(invoke(c, admin, "office.todo.list", {"status": "open"}))
    rec = next((t for t in lst.get("items", []) if t.get("title") == todo_title), None)
    r = invoke(c, admin, "office.todo.update", {"todo_id": rec["id"], "status": "done"})
    approve(c, reviewer, (r.get("data") or {})["approval_id"])
    f2 = payload(
        invoke(
            c,
            admin,
            "office.meeting.followup",
            {"action_items": [{"task": todo_title}, {"task": f"未建单-{suffix}"}]},
        )
    )
    st0 = (f0.get("items") or [{}])[0].get("state")
    st1 = (f1.get("items") or [{}])[0].get("state")
    st2 = {x["task"]: x["state"] for x in f2.get("items", [])}
    check(
        "⑤ followup 对账三态：未建单→进行中→已完成（含台账原值）",
        st0 == "missing"
        and st1 == "open"
        and st2.get(todo_title) == "done"
        and st2.get(f"未建单-{suffix}") == "missing",
        f"{st0}|{st1}|{st2}",
    )

print()
print("RESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
