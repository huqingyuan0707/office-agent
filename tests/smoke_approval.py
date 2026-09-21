"""审批闭环 HTTP 级实测（M1 验收）：需审批写工具 → 双人审批 → 同人红线 → 驳回流。

前置：office-agent 单包服务已启动（默认 http://127.0.0.1:8201，独立于演示实例 8200），
      且 REVIEWER_USERNAME / REVIEWER_PASSWORD 已配置（默认 reviewer/reviewer123）。
运行：python tests/smoke_approval.py [base_url]
流程：登录双账号 → 工具清单含 office.memo.submit → invoke 只落审批单（pending）→
      同人批准被 1001 红线拦截 → 复核员批准后真正执行（任务留痕 succeeded）→
      第二单复核员驳回（rejected）。
输出：逐步 PASS/FAIL + RESULT 汇总；退出码 0=全过 1=有失败。
对齐：docs/office-agent仓库骨架与内核提取方案.md §2（审批闸门）、§5（M1 验收：审批闭环 HTTP 级实测）。
"""

import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8201"
ADMIN = ("admin", "admin123")
REVIEWER = ("reviewer", "reviewer123")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

failures: list[str] = []


def check(step: str, cond: bool, detail: str = "") -> None:
    """单步断言：PASS/FAIL 即时打印，失败记入汇总。"""
    print(
        ("PASS | " if cond else "FAIL | ") + step + (f"（{detail}）" if detail and not cond else "")
    )
    if not cond:
        failures.append(step)


def login(client: httpx.Client, account: tuple[str, str]) -> str:
    """登录取 token（失败直接抛错：前置不满足无继续意义）。"""
    resp = client.post("/auth/login", json={"username": account[0], "password": account[1]})
    body = resp.json()
    assert resp.status_code == 200 and body.get("ok"), f"登录失败：{resp.status_code} {body}"
    return body["data"]["token"]


def main() -> int:
    with httpx.Client(base_url=BASE, trust_env=False, timeout=15) as client:
        # ① 双账号登录
        auth_admin = {"Authorization": f"Bearer {login(client, ADMIN)}"}
        auth_reviewer = {"Authorization": f"Bearer {login(client, REVIEWER)}"}
        check("① 管理员+复核员双账号登录", True)

        # ② 工具清单含需审批写工具（office:write + needs_approval）
        tools = client.get("/tools", headers=auth_admin).json()["data"]
        memo = next((t for t in tools if t["name"] == "office.memo.submit"), None)
        check(
            "② 工具清单含 office.memo.submit（office:write，需审批）",
            memo is not None and memo["needs_approval"] is True and memo["scope"] == "office:write",
        )

        # ③ invoke 需审批工具 → 只落审批单，不执行（写动作无直接生效通道）
        resp = client.post(
            "/tools/invoke",
            headers=auth_admin,
            json={
                "name": "office.memo.submit",
                "args": {
                    "title": "周四停机维护公告",
                    "content": "本周四 22:00-23:00 系统维护，期间服务暂不可用。",
                },
            },
        ).json()
        data = resp.get("data") or {}
        check(
            "③ invoke 返回 pending 审批单（未执行）",
            resp.get("ok") is True and data.get("status") == "pending",
            str(resp),
        )
        approval_id = data.get("approval_id")

        # ④ 审批单列表可见 pending 单
        rows = client.get("/approvals", headers=auth_admin).json()["data"]
        row = next((a for a in rows if a["id"] == approval_id), None)
        check("④ 审批单列表可见该 pending 单", row is not None and row["status"] == "pending")

        # ⑤ 同人批准 → 1001 红线拦截（防自审自批）
        resp = client.post(
            f"/approvals/{approval_id}/decide",
            headers=auth_admin,
            json={"approve": True, "comment": "自审自批尝试"},
        ).json()
        check(
            "⑤ 同人批准被 1001 红线拦截",
            resp.get("ok") is False and resp.get("error", {}).get("code") == 1001,
            str(resp),
        )

        # ⑥ 复核员批准 → 真正执行
        resp = client.post(
            f"/approvals/{approval_id}/decide",
            headers=auth_reviewer,
            json={"approve": True, "comment": "同意发布"},
        ).json()
        data = resp.get("data") or {}
        check(
            "⑥ 复核员批准后真正执行（approved + task_id）",
            resp.get("ok") is True and data.get("status") == "approved" and data.get("task_id"),
            str(resp),
        )

        # ⑦ 任务留痕 succeeded（公告回执）
        tasks = client.get("/tasks", headers=auth_admin).json()["data"]
        task = next(
            (t for t in tasks if t["type"] == "office.memo.submit" and t["status"] == "succeeded"),
            None,
        )
        check(
            "⑦ 任务留痕 succeeded（含发布回执）",
            task is not None and (task.get("result") or {}).get("published") is True,
        )

        # ⑧ 第二单：复核员驳回
        resp = client.post(
            "/tools/invoke",
            headers=auth_admin,
            json={
                "name": "office.memo.submit",
                "args": {"title": "待驳回的草稿", "content": "这条应被驳回。"},
            },
        ).json()
        approval_id2 = (resp.get("data") or {}).get("approval_id")
        resp = client.post(
            f"/approvals/{approval_id2}/decide",
            headers=auth_reviewer,
            json={"approve": False, "comment": "内容不合适，驳回"},
        ).json()
        data = resp.get("data") or {}
        check(
            "⑧ 复核员驳回 → rejected",
            resp.get("ok") is True and data.get("status") == "rejected",
            str(resp),
        )

    total, failed = 8, len(failures)
    print(
        f"RESULT: {total - failed}/{total} checks passed"
        + ("" if not failed else f"，失败：{failures}")
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
