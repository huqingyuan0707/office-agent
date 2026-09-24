"""V1.0 七项功能 HTTP 级冒烟：周报/纪要/知识库/OCR/文档对比/任务拆解/模板 + 站内通知。

前置：packages/server 服务已启动（默认 http://127.0.0.1:8200，仓库根目录启动——
      DOCS_DIR=data/docs、KB_DIR=data/knowledge 相对仓库根）；Pillow/python-docx 已安装。
运行：python tests/smoke_v1_features.py [base_url]
流程：双账号登录 → 九个新工具注册可见 → 周报（weekly 板块）→ 会议纪要 → 知识库问答（溯源）→
      OCR（元数据 + degraded 不编造 + 穿越拒绝）→ 文档对比（1增1改）→
      任务拆解（缺人不臆造为 0 + commit 审批闭环：同人 1001 → 复核员批准）→
      模板保存（审批落盘）+ 复用渲染（unfilled 如实列出）→ 通知扫描/列表/已读幂等。
输出：逐步 PASS/FAIL + RESULT 汇总；退出码 0=全过 1=有失败。
对齐：智能办公Agent 产品需求文档.md §5.1（V1.0 功能清单）、§6（任务拆解）；
      docs/office-agent仓库骨架与内核提取方案.md §4（tools 层）。
"""

import sys
from pathlib import Path

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8200"
API = f"{BASE}/api/v1"
ADMIN = ("admin", "admin123")
REVIEWER = ("reviewer", "reviewer123")
DOCS_DIR = Path("data/docs")

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
    assert resp.status_code == 200 and body.get("code") == 0, f"登录失败：{resp.status_code} {body}"
    return body["data"]["token"]


def invoke(client: httpx.Client, headers: dict, name: str, args: dict) -> dict:
    """工具调用，返回外层信封（data.status / data.result / data.approval_id）。"""
    return client.post(f"/agent/tools/{name}/invoke", headers=headers, json={"args": args}).json()


def result_of(body: dict) -> dict:
    """取工具真实出参（data.result）。"""
    return (body.get("data") or {}).get("result") or {}


def approve(client: httpx.Client, headers: dict, approval_id: str) -> dict:
    """复核员批准并返回信封。"""
    return client.post(
        f"/approvals/{approval_id}/approve", headers=headers, json={"reason": "smoke-v1"}
    ).json()


def make_fixtures() -> dict[str, Path]:
    """造样例文件：OCR 用 PNG（Pillow）、对比用两份 docx（python-docx）。"""
    from PIL import Image

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    png = DOCS_DIR / "smoke_v1.png"
    Image.new("RGB", (64, 32), color=(240, 240, 240)).save(png, format="PNG")

    import docx

    lines_v1 = ["第一段：项目背景介绍", "第二段：本期工作进展", "第三段：风险与问题"]
    lines_v2 = [
        "第一段：项目背景介绍",
        "第二段：本期工作进展",
        "第三段：风险与问题已更新",
        "第四段：新增的总结段落",
    ]
    v1, v2 = DOCS_DIR / "smoke_v1_old.docx", DOCS_DIR / "smoke_v1_new.docx"
    for path, lines in ((v1, lines_v1), (v2, lines_v2)):
        document = docx.Document()
        for line in lines:
            document.add_paragraph(line)
        document.save(str(path))
    return {"png": png, "v1": v1, "v2": v2}


def main() -> int:
    files = make_fixtures()
    with httpx.Client(base_url=API, trust_env=False, timeout=30) as client:
        auth_admin = {"Authorization": f"Bearer {login(client, ADMIN)}"}
        auth_reviewer = {"Authorization": f"Bearer {login(client, REVIEWER)}"}

        # ① 九个新工具注册可见（读侧免审批；task.commit / template.save 恒送审）
        items = client.get("/agent/tools", headers=auth_admin).json()["data"]["items"]
        tools = {t["name"]: t for t in items}
        names = [
            "office.report.generate",
            "office.minutes.generate",
            "kb.ask",
            "ocr.image",
            "office.doc.compare",
            "office.task.decompose",
            "office.task.commit",
            "office.template.save",
            "office.template.apply",
        ]
        check(
            "① 九个 V1.0 工具注册（写侧 task.commit/template.save 需审批）",
            all(name in tools for name in names)
            and tools["office.task.commit"]["requires_approval"] is True
            and tools["office.template.save"]["requires_approval"] is True
            and tools["office.report.generate"]["requires_approval"] is False
            and tools["office.task.commit"]["scope"] == "office:write",
            str(sorted(tools))[:200],
        )

        # ② 周报：report_type=weekly 追加亮点/下周计划板块
        body = invoke(
            client,
            auth_admin,
            "office.report.generate",
            {
                "title": "研发周报",
                "report_type": "weekly",
                "metrics": {"完成需求": 12, "关闭缺陷": 8},
                "highlights": ["审批闭环上线"],
                "next_plan": ["接入站内通知"],
            },
        )
        report = result_of(body).get("report", "")
        check(
            "② 周报生成（报告类型：周报 + 本周亮点 + 下周计划板块）",
            body.get("code") == 0
            and "报告类型：周报" in report
            and "本周亮点" in report
            and "审批闭环上线" in report
            and "下周计划" in report,
            str(body)[:200],
        )

        # ③ 会议纪要：出席/议题/决议/行动项模板直出
        body = invoke(
            client,
            auth_admin,
            "office.minutes.generate",
            {
                "title": "周会纪要",
                "attendees": ["张三", "李四"],
                "agenda": ["进展同步", "风险评审"],
                "decisions": ["方案 A 通过"],
                "action_items": [{"task": "补齐回归测试", "owner": "张三"}],
            },
        )
        minutes, counts = result_of(body).get("minutes", ""), result_of(body).get("counts", {})
        check(
            "③ 会议纪要生成（模板直出 + 计数）",
            body.get("code") == 0
            and "周会纪要" in minutes
            and "方案 A 通过" in minutes
            and counts.get("attendee") == 2
            and counts.get("action_item") == 1,
            str(body)[:200],
        )

        # ④ 知识库问答：内置条目命中 + 溯源
        body = invoke(client, auth_admin, "kb.ask", {"query": "报销超过 1000 元找谁审批"})
        results = result_of(body).get("results", [])
        check(
            "④ 知识库问答（命中内置报销条目 + source 溯源）",
            body.get("code") == 0
            and results
            and any("报销" in item.get("title", "") for item in results)
            and result_of(body).get("source") == "local-kb"
            and result_of(body).get("fetched_at"),
            str(body)[:200],
        )

        # ⑤ OCR：元数据直读；引擎缺失走 degraded 且 text 留空（绝不编造）；穿越拒绝
        body = invoke(client, auth_admin, "ocr.image", {"file_path": "smoke_v1.png"})
        data = (body.get("data") or {}).get("result") or {}
        image_meta = data.get("image", {})
        check(
            "⑤ ocr.image 元数据直读 + degraded 时不编造文本",
            body.get("code") == 0
            and image_meta.get("width") == 64
            and image_meta.get("format") == "PNG"
            and data.get("source") == "local-docs:smoke_v1.png"
            and ((not data.get("degraded")) or (not data.get("text"))),
            str(body)[:200],
        )
        body = invoke(client, auth_admin, "ocr.image", {"file_path": "../evil.png"})
        check(
            "⑥ OCR 路径穿越被 400 拒绝",
            body.get("code") in (400, 1001),
            str(body)[:120],
        )

        # ⑦ 文档对比：第三段改写 + 第四段新增 → 1 改 1 增 0 删
        body = invoke(
            client,
            auth_admin,
            "office.doc.compare",
            {"file_a": "smoke_v1_old.docx", "file_b": "smoke_v1_new.docx"},
        )
        counts = result_of(body).get("counts", {})
        check(
            "⑦ 文档对比（新增 1 / 删除 0 / 修改 1 + 摘要）",
            body.get("code") == 0
            and counts.get("added") == 1
            and counts.get("removed") == 0
            and counts.get("changed") == 1
            and "新增 1 段" in result_of(body).get("change_summary", ""),
            str(body)[:200],
        )

        # ⑧ 任务拆解：参与人齐全 → 5 阶段全匹配、责任人不臆造、期限按启动日推算
        body = invoke(
            client,
            auth_admin,
            "office.task.decompose",
            {
                "goal": "上线新版内部办公系统",
                "duration_weeks": 6,
                "start_date": "2026-10-01",
                "participants": ["产品经理", "前端工程师", "测试工程师"],
            },
        )
        plan = result_of(body)
        subtasks = plan.get("subtasks", [])
        due_last = subtasks[-1].get("due_date", "") if subtasks else ""
        check(
            "⑧ 任务拆解（5 阶段责任人全匹配 + missing_info 为空 + 期限推算）",
            body.get("code") == 0
            and plan.get("total") == 5
            and all(item.get("owner") for item in subtasks)
            and plan.get("missing_info") == []
            and due_last > "2026-10-01"  # 末阶段期限 = 启动日 + 6 周推算
            and due_last <= "2026-11-11",
            str(body)[:300],
        )

        # ⑨ 批量建单走审批：invoke 只落单 → 同人 1001 → 复核员批准 → 回执含通知标记
        body = invoke(
            client,
            auth_admin,
            "office.task.commit",
            {
                "tasks": [
                    {
                        "title": subtasks[0]["title"],
                        "owner": subtasks[0]["owner"],
                        "due_date": subtasks[0]["due_date"],
                        "priority": "high",
                    },
                    {"title": "机动任务（无责任人）"},
                ],
                "idem_key": "smoke-v1-task-2026",
            },
        )
        approval_id = (body.get("data") or {}).get("approval_id")
        check(
            "⑨ task.commit invoke 只落审批单（pending_approval + idem_key）",
            body.get("code") == 0
            and (body.get("data") or {}).get("status") == "pending_approval"
            and approval_id,
            str(body)[:200],
        )
        same_person = client.post(
            f"/approvals/{approval_id}/approve", headers=auth_admin, json={"reason": "smoke"}
        ).json()
        check(
            "⑩ 同人批准被 1001 红线拦截",
            same_person.get("code") == 1001,
            str(same_person)[:150],
        )
        body = approve(client, auth_reviewer, approval_id)
        execution = (body.get("data") or {}).get("execution_result") or {}
        created = (execution.get("result") or {}).get("created") or []
        check(
            "⑪ 复核员批准后批量建单回执（无责任人任务不发送通知）",
            body.get("code") == 0
            and execution.get("status") == "ok"
            and (execution.get("result") or {}).get("count") == 2
            and created
            and created[0].get("notified") == "true"
            and created[1].get("notified") == "false",
            str(body)[:300],
        )

        # ⑫ 模板保存走审批 → 落盘 templates/；复用渲染：缺值占位符保持原样并如实列出
        body = invoke(
            client,
            auth_admin,
            "office.template.save",
            {
                "name": "周报模板V1",
                "title": "{姓名}的周报",
                "sections": ["本周完成：{本周工作}", "下周计划：{下周计划}"],
                "idem_key": "smoke-v1-tpl-01",
            },
        )
        approval_id = (body.get("data") or {}).get("approval_id")
        check(
            "⑫ template.save invoke 只落审批单",
            body.get("code") == 0 and (body.get("data") or {}).get("status") == "pending_approval",
            str(body)[:200],
        )
        body = approve(client, auth_reviewer, approval_id)
        template_file = DOCS_DIR / "templates" / "周报模板V1.json"
        check(
            "⑬ 复核员批准后模板真实落盘",
            body.get("code") == 0 and template_file.exists(),
            str(body)[:200],
        )
        body = invoke(
            client,
            auth_admin,
            "office.template.apply",
            {"name": "周报模板V1", "values": {"姓名": "张三", "本周工作": "上线审批闭环"}},
        )
        applied = result_of(body)
        check(
            "⑭ 模板复用渲染（已填充 + unfilled 如实列出）",
            body.get("code") == 0
            and applied.get("title") == "张三的周报"
            and "上线审批闭环" in " ".join(applied.get("sections", []))
            and "{下周计划}" in " ".join(applied.get("sections", []))
            and applied.get("unfilled") == ["下周计划"],
            str(body)[:250],
        )

        # ⑮ 站内通知：扫描生成（可重复执行——同键去重）→ 本人列表 → 已读幂等
        first = client.post("/notifications/scan", headers=auth_admin).json()["data"]
        second = client.post("/notifications/scan", headers=auth_admin).json()["data"]
        check(
            "⑯ 通知扫描（有信号可扫 + 重复扫描幂等 created=0 不刷屏）",
            first.get("created", 0) + first.get("skipped_existing", 0) >= 1
            and second.get("created") == 0
            and second.get("skipped_existing", 0) >= 1,
            f"first={first} second={second}",
        )
        mine = client.get("/notifications", headers=auth_admin).json()["data"]
        reviewer_items = client.get("/notifications", headers=auth_reviewer).json()["data"]
        target = (reviewer_items.get("items") or [{}])[0].get("id")
        read_once = client.post(f"/notifications/{target}/read", headers=auth_reviewer).json()
        read_twice = client.post(f"/notifications/{target}/read", headers=auth_reviewer).json()
        check(
            "⑰ 通知列表按人隔离 + 已读回执幂等",
            mine.get("total", 0) >= 1
            and reviewer_items.get("total", 0) >= 1
            and target
            and read_once.get("data", {}).get("read_at")
            and read_twice.get("data", {}).get("already_read") is True,
            f"mine={mine.get('total')} reviewer={reviewer_items.get('total')} {read_once} {read_twice}",
        )

    # 清理 smoke 产物（保持工作目录干净；templates 下只删本次同名文件）
    for leftover in (
        files["png"],
        files["v1"],
        files["v2"],
        DOCS_DIR / "templates" / "周报模板V1.json",
    ):
        leftover.unlink(missing_ok=True)
    templates = DOCS_DIR / "templates"
    if templates.is_dir() and not any(templates.iterdir()):
        templates.rmdir()

    total, failed = 17, len(failures)
    print(
        f"RESULT: {total - failed}/{total} checks passed"
        + ("" if not failed else f"，失败：{failures}")
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
