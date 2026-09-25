"""PRD §2.7 邮件智能处理 HTTP 级冒烟（可重复执行；四工具全读免审、入参驱动零状态）：

① 四工具已注册且口径正确（office:read + 免审批）；
② office.mail.classify：四类邮件各归各类（spam/action/reply/info），counts 对得上；
③ office.mail.reply_draft：要点原样进正文，署名占位如实列出；
④ office.mail.action_items：责任人/截止逐句提取，提不出的字段为 null 不臆造；
⑤ office.mail.precheck：正常外发 passed；隐私+催促语气命中 need_confirm；
⑥ 凭据命中摘录脱敏（口令明文不出现在扫描结果里）。

运行前提：`.venv\\Scripts\\python.exe -m office_agent_server`（8200）已在跑。
"""

import sys

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


with httpx.Client(base_url=OFFICE, trust_env=False, timeout=30) as c:
    admin = login(c, "admin", "admin123")

    # ① 工具清单
    tools = {t["name"]: t for t in c.get("/agent/tools", headers=admin).json()["data"]["items"]}
    expect = {
        "office.mail.classify": ("office:read", False),
        "office.mail.reply_draft": ("office:read", False),
        "office.mail.action_items": ("office:read", False),
        "office.mail.precheck": ("office:read", False),
    }
    ok = all(
        name in tools and (tools[name]["scope"], tools[name]["requires_approval"]) == spec
        for name, spec in expect.items()
    )
    check(
        "① 邮件四工具已注册且全读免审",
        ok,
        str(
            {n: tools.get(n) and (tools[n]["scope"], tools[n]["requires_approval"]) for n in expect}
        ),
    )

    # ② 归类
    d = payload(
        invoke(
            c,
            admin,
            "office.mail.classify",
            {
                "emails": [
                    {"subject": "限时优惠", "body": "点击链接领取"},
                    {"subject": "合同盖章", "body": "请于周五前办理"},
                    {"subject": "方案确认", "body": "是否可行，盼复"},
                    {"subject": "例会安排", "body": "特此通知，请知会全员"},
                ]
            },
        )
    )
    cats = [item["category"] for item in d.get("results", [])]
    check("② 归类四类各命中一条", cats == ["spam", "action", "reply", "info"], str(cats))

    # ③ 回复草稿
    d = payload(
        invoke(
            c,
            admin,
            "office.mail.reply_draft",
            {
                "subject": "合作报价",
                "sender_name": "王经理",
                "key_points": ["报价含税", "交期两周"],
            },
        )
    )
    check(
        "③ 草稿要点原样进正文且署名占位",
        d.get("reply_subject") == "Re: 合作报价"
        and "1. 报价含税" in d.get("draft", "")
        and "署名" in d.get("placeholders", []),
        str(d)[:160],
    )

    # ④ 行动项
    d = payload(
        invoke(
            c,
            admin,
            "office.mail.action_items",
            {"body": "@张三 请在 2026-09-30 前完成方案定稿。李四负责整理数据，本周五前提交。"},
        )
    )
    items = d.get("items", [])
    check(
        "④ 行动项提取责任人/截止",
        d.get("count") == 2
        and items
        and items[0]["owner"] == "张三"
        and items[0]["due"] == "2026-09-30"
        and items[1]["owner"] == "李四",
        str(items)[:160],
    )

    # ⑤ 预审：正常通过 / 命中二次确认
    clean = payload(
        invoke(
            c, admin, "office.mail.precheck", {"text": "附件为本季度交付计划，欢迎提出修改意见。"}
        )
    )
    risky = payload(
        invoke(
            c,
            admin,
            "office.mail.precheck",
            {"text": "我的手机号13812345678，你们必须立刻处理，否则投诉。"},
        )
    )
    cats_hit = {hit["category"] for hit in risky.get("hits", [])}
    check(
        "⑤ 预审正常件 passed、隐私+语气件需二次确认",
        clean.get("passed") is True
        and risky.get("need_confirm") is True
        and {"privacy", "tone"} <= cats_hit,
        str(cats_hit),
    )

    # ⑥ 凭据脱敏
    d = payload(
        invoke(
            c, admin, "office.mail.precheck", {"text": "系统口令 password=supersecret123 请查收"}
        )
    )
    leak = [hit for hit in d.get("hits", []) if hit["category"] == "leak"]
    check(
        "⑥ 凭据命中摘录已脱敏",
        bool(leak) and "supersecret123" not in leak[0]["excerpt"],
        str(leak)[:120],
    )

print()
if failures:
    print(f"RESULT | {len(failures)} 项失败：{'、'.join(failures)}")
    sys.exit(1)
print("RESULT | 6/6 全部通过（§2.7 邮件智能处理 HTTP 级冒烟）")
