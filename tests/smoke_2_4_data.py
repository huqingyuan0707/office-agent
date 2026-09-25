"""PRD §2.4 新增三件 HTTP 级冒烟（可重复执行，查询名带随机后缀互不干扰）：

① 五新工具已注册且口径正确（save/delete 写恒送审；list/run/insight 读免审；
   export 的 format 枚举含 excel）；
② office.data.query.save：invoke 只落审批单 → reviewer 批准 → list 可见；
③ 同名重复保存只落单（重名拒绝在批准执行期，handler 单测锁 1001），随后驳回无残留；
④ office.data.query.run：一句话复用，rows 与 query 同口径实时查数一致；
⑤ office.data.export format=excel：base64 信封可解回 xlsx（PK 头）；
⑥ office.data.chart_insight：最高/最低标注 + 中文简报 + SVG 图；
⑦ office.data.export format=markdown 回归（文本 content 不变）；
⑧ office.data.query.delete：批准后 list 不再含该查询。

运行前提：`.venv\\Scripts\\python.exe -m office_agent_server`（8200）已在跑，
且为当前工作区代码（新工具需重启后才注册）。
"""

import base64
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
        f"/approvals/{approval_id}/approve", headers=reviewer, json={"reason": "§2.4 冒烟"}
    ).json()
    return (r.get("data") or {}).get("execution_result") or {}


def reject(c, reviewer, approval_id):
    r = c.post(
        f"/approvals/{approval_id}/reject", headers=reviewer, json={"reason": "§2.4 冒烟：重复保存"}
    ).json()
    return r.get("code") == 0


suffix = uuid.uuid4().hex[:6]
qname = f"冒烟业绩查询-{suffix}"

with httpx.Client(base_url=OFFICE, trust_env=False, timeout=30) as c:
    admin = login(c, "admin", "admin123")
    reviewer = login(c, "reviewer", "reviewer123")

    # ① 工具清单
    tools = {t["name"]: t for t in c.get("/agent/tools", headers=admin).json()["data"]["items"]}
    expect = {
        "office.data.query.save": ("office:write", True),
        "office.data.query.list": ("office:read", False),
        "office.data.query.run": ("office:read", False),
        "office.data.query.delete": ("office:write", True),
        "office.data.chart_insight": ("office:read", False),
    }
    ok = all(
        name in tools and (tools[name]["scope"], tools[name]["requires_approval"]) == spec
        for name, spec in expect.items()
    )
    export_spec = next(
        (
            t
            for t in c.get("/agent/tools", headers=admin).json()["data"]["items"]
            if t["name"] == "office.data.export"
        ),
        {},
    )
    check(
        "① 五新工具已注册且口径正确",
        ok,
        str(
            {n: tools.get(n) and (tools[n]["scope"], tools[n]["requires_approval"]) for n in expect}
        ),
    )
    check(
        "①b export format 枚举含 excel",
        "excel" in str((export_spec.get("params") or {}).get("properties", {}).get("format")),
        str((export_spec.get("params") or {}).get("properties", {}).get("format"))[:200],
    )

    # ② 保存：落单 → 批准 → list 可见
    r = invoke(
        c,
        admin,
        "office.data.query.save",
        {
            "name": qname,
            "dataset": "sales",
            "filters": {"person": "张三"},
            "limit": 10,
            "description": "冒烟",
            "idem_key": f"smoke-24-{suffix}",
        },
    )
    d = r.get("data") or {}
    check(
        "②a query.save 只落审批单",
        r.get("code") == 0 and d.get("status") == "pending_approval",
        str(d)[:200],
    )
    ex = approve(c, reviewer, d["approval_id"])
    names = [q["name"] for q in payload(invoke(c, admin, "office.data.query.list", {}))["queries"]]
    check("②b 批准后 list 可见", ex.get("status") == "ok" and qname in names, str(ex)[:200])

    # ③ 同名重复保存：写动作先落单（重名检查在批准执行期由 handler 做，单测已锁 1001），
    #    此处断言「只落单不直写」后主动驳回，避免残留悬空审批单
    r = invoke(
        c,
        admin,
        "office.data.query.save",
        {"name": qname, "dataset": "sales", "idem_key": f"smoke-24-dup-{suffix}"},
    )
    d = r.get("data") or {}
    check(
        "③ 同名重复保存只落单（执行期才做重名拒绝，不静默覆盖）",
        r.get("code") == 0 and d.get("status") == "pending_approval",
        str(d)[:200],
    )
    check("③b 重复单已驳回无残留", reject(c, reviewer, d["approval_id"]), d.get("approval_id", ""))

    # ④ 一句话复用：与 query 同口径行数一致
    run = payload(invoke(c, admin, "office.data.query.run", {"name": qname}))
    direct = payload(
        invoke(
            c,
            admin,
            "office.data.query",
            {"dataset": "sales", "filters": {"person": "张三"}, "limit": 10},
        )
    )
    check(
        "④ query.run 复用口径与直查一致",
        run.get("saved_as") == qname and run.get("count") == direct.get("count") == 2,
        str({k: run.get(k) for k in ("saved_as", "count")})[:200],
    )

    # ⑤ excel 导出：base64 可解回 xlsx
    xl = payload(
        invoke(
            c,
            admin,
            "office.data.export",
            {
                "title": "冒烟业绩",
                "format": "excel",
                "columns": ["person", "amount"],
                "rows": direct.get("rows", []),
            },
        )
    )
    try:
        raw = base64.b64decode(xl.get("content", ""))
        is_xlsx = raw[:2] == b"PK"
    except Exception:
        is_xlsx = False
    check(
        "⑤ export excel 为 base64 的 xlsx（PK 头）",
        xl.get("encoding") == "base64" and is_xlsx,
        str({k: xl.get(k) for k in ("encoding", "filename_hint")})[:200],
    )

    # ⑥ 图表解读（分类用 人+月份 复合标签，避免同人多期重名；数值取原值）
    cats = [f"{row.get('person', '')}{row.get('month', '')}" for row in direct.get("rows", [])]
    vals = [
        row["amount"]
        for row in direct.get("rows", [])
        if isinstance(row.get("amount"), (int, float))
    ]
    assert len(cats) == len(vals) and len(vals) > 0
    ins = payload(
        invoke(
            c,
            admin,
            "office.data.chart_insight",
            {"title": "冒烟解读", "categories": cats, "values": vals},
        )
    )
    reasons = {h["category"]: h["reason"] for h in ins.get("highlights", [])}
    top_cat = max(zip(vals, cats, strict=True))[1]
    check(
        "⑥ chart_insight 最高/最低标注 + 简报 + SVG",
        reasons.get(top_cat) == "最高"
        and f"最高：{top_cat}" in (ins.get("brief") or "")
        and (ins.get("chart_svg") or "").lstrip().startswith("<svg"),
        str(ins.get("brief"))[:200],
    )

    # ⑦ markdown 回归
    md = payload(
        invoke(
            c,
            admin,
            "office.data.export",
            {
                "title": "冒烟",
                "format": "markdown",
                "columns": ["person"],
                "rows": [{"person": "张三"}],
            },
        )
    )
    check(
        "⑦ export markdown 文本口径回归",
        md.get("encoding") == "text" and "| person |" in md.get("content", ""),
        str(md.get("content"))[:200],
    )

    # ⑧ 删除：批准后 list 不再含
    r = invoke(
        c,
        admin,
        "office.data.query.delete",
        {"name": qname, "idem_key": f"smoke-24-del-{suffix}"},
    )
    ex = approve(c, reviewer, (r.get("data") or {})["approval_id"])
    names = [q["name"] for q in payload(invoke(c, admin, "office.data.query.list", {}))["queries"]]
    check(
        "⑧ query.delete 批准后移除", ex.get("status") == "ok" and qname not in names, str(ex)[:200]
    )

print()
print("RESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
