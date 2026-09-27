"""对话办理重构 HTTP 级冒烟（可重复执行）：大模型串联三场景链路验证

① office.doc.compose 已注册（office:read 免审批）；
② daily-report-assistant「生成今天的工作日报」规则链 → 末步 office.doc.compose，
   composed_by=llm/fallback 与 degraded 一致、document 非空、数字校验字段齐备；
③ office-assistant「经营复盘」规则快路径 → 三步链（data.query → kb.ask → doc.compose）；
④ office-assistant 自由目标（规则不中）→ react：LLM 可用走逐步再规划
   （planner_source=react），不可用明确中文报错（受理与执行分离），两种收敛均验证到位；
⑤ office.doc.compose 直接 invoke：降级/成稿口径一致（素材数字可溯源）。

运行前提：`.venv\\Scripts\\python.exe -m office_agent_server`（8200）已在跑；
LLM 可用时②③④会真实出站成稿（本地 qwen3:8b 单步约 30-60s，轮询上限放宽到 300s）。
"""

import sys
import time

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


def wait_terminal(c: httpx.Client, headers: dict, run_id: str, timeout_s: int = 300) -> dict:
    """轮询到终态（DONE/FAILED）；LLM 成稿慢，默认放宽到 300s。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        d = c.get(f"/runs/{run_id}", headers=headers).json()["data"]
        if d.get("status") in ("DONE", "FAILED"):
            return d
        time.sleep(2)
    raise TimeoutError(f"run {run_id} 在 {timeout_s}s 内未到终态")


def start_run(c: httpx.Client, headers: dict, agent: str, goal: str) -> dict:
    r = c.post("/runs", headers=headers, json={"agent": agent, "goal": goal}).json()
    assert r["code"] == 0, r
    return r["data"]


def check_compose_result(step: str, result: dict) -> None:
    """office.doc.compose 出参口径：composed_by/degraded 一致 + document 非空 + 数字校验齐备。"""
    composed_by = str(result.get("composed_by") or "")
    degraded = result.get("degraded")
    check(
        f"{step} composed_by 合法（llm/fallback）", composed_by in ("llm", "fallback"), composed_by
    )
    check(
        f"{step} degraded 与 composed_by 一致",
        degraded is (composed_by == "fallback"),
        f"composed_by={composed_by} degraded={degraded}",
    )
    document = str(result.get("document") or "")
    check(f"{step} document 非空 markdown", len(document) > 40 and "\n" in document)
    if composed_by == "llm":
        check(f"{step} LLM 成稿不含降级占位说明", "素材原文直出" not in document)
    numbers = result.get("numbers_check")
    check(
        f"{step} 数字溯源校验字段齐备",
        isinstance(numbers, dict) and "verified" in numbers and "unverified" in numbers,
        str(numbers)[:120],
    )


with httpx.Client(base_url=OFFICE, trust_env=False, timeout=300) as c:
    admin = login(c, "admin", "admin123")

    # ① 工具注册与口径
    tools = {t["name"]: t for t in c.get("/agent/tools", headers=admin).json()["data"]["items"]}
    spec = tools.get("office.doc.compose")
    check("① office.doc.compose 已注册", spec is not None)
    if spec:
        check(
            "① 免审批读口径",
            spec.get("scope") == "office:read" and not spec.get("requires_approval"),
        )

    # ② 日报规则链：schedule.view → doc.compose（大模型成稿或降级素材直出）
    data = wait_terminal(
        c, admin, start_run(c, admin, "daily-report-assistant", "生成今天的工作日报")["run_id"]
    )
    check("② 日报 run 收敛 DONE", data["status"] == "DONE", str(data.get("error"))[:120])
    steps = data.get("steps") or []
    check(
        "② 末步是 office.doc.compose",
        bool(steps) and steps[-1]["tool"] == "office.doc.compose",
        str([s["tool"] for s in steps]),
    )
    if steps and steps[-1]["tool"] == "office.doc.compose":
        check_compose_result("②", steps[-1].get("result") or {})

    # ③ 跨域规则快路径：data.query → kb.ask → doc.compose（混合串联三场景）
    data = wait_terminal(c, admin, start_run(c, admin, "office-assistant", "经营复盘")["run_id"])
    check("③ 经营复盘 run 收敛 DONE", data["status"] == "DONE", str(data.get("error"))[:120])
    tools_chain = [s["tool"] for s in data.get("steps") or []]
    check(
        "③ 三步链 = 数据查询 → 制度检索 → 大模型成稿",
        tools_chain == ["office.data.query", "kb.ask", "office.doc.compose"],
        str(tools_chain),
    )
    steps = data.get("steps") or []
    if tools_chain and tools_chain[-1] == "office.doc.compose":
        check_compose_result("③", steps[-1].get("result") or {})

    # ④ react 自由目标（不中规则）：LLM 可用走逐步再规划；不可用明确中文报错
    data = wait_terminal(
        c, admin, start_run(c, admin, "office-assistant", "帮我看看销售情况怎么样")["run_id"]
    )
    steps = data.get("steps") or []
    if data["status"] == "DONE":
        check("④ react 自由目标收敛 DONE", True)
        check(
            "④ planner_source=react（逐步再规划）",
            bool(steps) and steps[0].get("planner_source") == "react",
            str([(s.get("planner_source"), s.get("tool")) for s in steps])[:160],
        )
    else:
        check(
            "④ LLM 不可用时中文报错（受理与执行分离）",
            "LLM" in str(data.get("error") or "") and not steps,
            str(data.get("error"))[:160],
        )

    # ⑤ 直接 invoke：出参与链路末步同口径
    r = c.post(
        "/agent/tools/office.doc.compose/invoke",
        headers=admin,
        json={
            "args": {
                "title": "冒烟成稿",
                "material": [{"index": 0, "tool": "demo", "result": {"count": 2}}],
            }
        },
    ).json()
    result = (r.get("data") or {}).get("result") or {}
    check("⑤ invoke 成功", r.get("code") == 0, str(r)[:160])
    check_compose_result("⑤", result)
    if result.get("composed_by") == "fallback":
        check("⑤ 降级素材直出含素材原文", "素材原文" in str(result.get("document") or ""))

print()
if failures:
    print(f"SMOKE FAILED：{len(failures)} 项未过")
    sys.exit(1)
print("SMOKE PASSED：大模型串联链路（占位符整体注入 → 成稿/降级 → react 逐步再规划）全部通过")
