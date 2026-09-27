"""执行链路追踪 HTTP 级冒烟（可重复执行）：调试面板数据口径验证

① 管理员发起规则 run（office-assistant「报销制度」→ kb.ask 单步快路径）；
② POST /runs 受理概要不带 trace（经 summary 返回，不向任何角色透出）；
③ 管理员 GET /runs/{id} 带 trace：意图/思考/路由/工具调用/工具返回/汇总六段齐备、
   总耗时为整数、候选只标选中不编造打分；
④ 普通员工（zhangsan，同租户）GET 同一 run 无 trace 键（服务端权限拦截）。

运行前提：`.venv\\Scripts\\python.exe -m office_agent_server`（8200）已在跑。
"""

import sys
import time

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8200"
OFFICE = f"{BASE.rstrip('/')}/api/v1"

failures: list[str] = []


def check(step: str, cond: bool, detail: str = "") -> None:
    print(
        ("PASS | " if cond else "FAIL | ") + step + (f"（{detail}）" if detail and not cond else "")
    )
    if not cond:
        failures.append(step)


def login(c: httpx.Client, u: str, p: str) -> dict:
    r = c.post("/auth/login", json={"username": u, "password": p}).json()
    assert r["code"] == 0, r
    return {"Authorization": f"Bearer {r['data']['token']}"}


def wait_terminal(c: httpx.Client, headers: dict, run_id: str, timeout_s: int = 120) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        d = c.get(f"/runs/{run_id}", headers=headers).json()["data"]
        if d.get("status") in ("DONE", "FAILED"):
            return d
        time.sleep(2)
    raise TimeoutError(f"run {run_id} 在 {timeout_s}s 内未到终态")


c = httpx.Client(base_url=OFFICE, timeout=30, trust_env=False)
admin = login(c, "admin", "admin123")
staff = login(c, "zhangsan", "zhangsan123")

body = c.post("/runs", headers=admin, json={"agent": "office-assistant", "goal": "报销制度"}).json()
check("发起 run 受理 code 0", body["code"] == 0, str(body)[:120])
summary = body["data"]
run_id = summary["run_id"]
check("受理概要不带 trace（任何角色都不经 summary 透出）", "trace" not in summary)

detail = wait_terminal(c, admin, run_id)
check("run 收敛 DONE", detail.get("status") == "DONE", str(detail.get("error"))[:120])
trace = detail.get("trace") or {}
check("管理员可见 trace", isinstance(detail.get("trace"), dict))
kinds = [s.get("step_type") for s in trace.get("steps", [])]
for want in ("intent", "thought", "route", "tool_call", "tool_result", "summary"):
    check(f"trace 含 {want} 段", want in kinds, str(kinds)[:160])
check("trace 总耗时为整数", isinstance(trace.get("total_ms"), int), str(trace.get("total_ms")))
route_content = next(
    (s.get("content", "") for s in trace.get("steps", []) if s.get("step_type") == "route"), ""
)
check("路由段标选中智能体", "✅（选中）" in route_content, route_content[:120])
check("路由段不编造小数打分", "0.91" not in route_content and "0.23" not in route_content)
intent_content = next(
    (s.get("content", "") for s in trace.get("steps", []) if s.get("step_type") == "intent"), ""
)
check("意图段注明置信度口径（规则非打分）", "非模型打分" in intent_content, intent_content[:120])

staff_view = c.get(f"/runs/{run_id}", headers=staff).json()["data"]
check("普通员工响应无 trace 键", "trace" not in staff_view)
check("普通员工仍可见步骤时间线", isinstance(staff_view.get("steps"), list))

print()
if failures:
    print(f"SMOKE FAILED：{len(failures)} 项未过")
    sys.exit(1)
print("SMOKE PASSED：执行链路追踪（受理不透出 / 管理员六段 / 员工无键）全过")
