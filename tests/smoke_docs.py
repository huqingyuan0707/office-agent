"""办公文档工具 HTTP 级实测：读侧直读 + 写侧审批闭环 + 路径穿越防护。

前置：office-agent 单包服务已启动（默认 http://127.0.0.1:8201），python-docx/openpyxl/python-pptx 已安装。
运行：python tests/smoke_docs.py [base_url]
流程：双账号登录 → 四工具注册可见 → 造样例 docx/xlsx → 读侧直读（内容+溯源）→
      路径穿越被拒（400）→ docx.write 走审批（同人 1001 拦截 → 复核员批准 → 文件真实落盘）→
      pptx.write 复核员驳回 → 文件不存在（写动作无直接生效通道）。
输出：逐步 PASS/FAIL + RESULT 汇总；退出码 0=全过 1=有失败。
对齐：docs/office-agent仓库骨架与内核提取方案.md §4（tools 层：办公文档域）。
"""

import sys

import docx
import httpx
import openpyxl

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8201"
ADMIN = ("admin", "admin123")
REVIEWER = ("reviewer", "reviewer123")
DOCS_DIR = "data/docs"

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


def decide(client: httpx.Client, headers: dict, approval_id, approve: bool) -> dict:
    """审批裁定并返回响应体。"""
    return client.post(
        f"/approvals/{approval_id}/decide",
        headers=headers,
        json={"approve": approve, "comment": "smoke"},
    ).json()


def invoke(client: httpx.Client, headers: dict, name: str, args: dict) -> dict:
    """工具调用并返回响应体。"""
    return client.post("/tools/invoke", headers=headers, json={"name": name, "args": args}).json()


def main() -> int:
    import os
    from pathlib import Path

    docs = Path(DOCS_DIR)
    docs.mkdir(parents=True, exist_ok=True)

    # 前置：造样例 docx / xlsx（用与被测工具相同的库，保证内容可预期）
    sample_docx = docs / "sample.docx"
    document = docx.Document()
    document.add_heading("Q3 复盘纪要", level=0)
    document.add_paragraph("营收环比增长 12%（来源：财务口径）")
    document.add_paragraph("客诉率下降 0.8 个百分点")
    table = document.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "指标"
    table.rows[0].cells[1].text = "数值"
    table.rows[1].cells[0].text = "营收（万元）"
    table.rows[1].cells[1].text = "1200"
    document.save(str(sample_docx))
    sample_xlsx = docs / "sample.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "销售"
    sheet.append(["月份", "销售额"])
    sheet.append(["7月", 320])
    sheet.append(["8月", 355])
    workbook.save(str(sample_xlsx))

    with httpx.Client(base_url=BASE, trust_env=False, timeout=30) as client:
        auth_admin = {"Authorization": f"Bearer {login(client, ADMIN)}"}
        auth_reviewer = {"Authorization": f"Bearer {login(client, REVIEWER)}"}

        # ① 四个文档工具注册可见（读写 scope 与审批要求正确）
        tools = {t["name"]: t for t in client.get("/tools", headers=auth_admin).json()["data"]}
        doc_read = tools.get("office.docx.read")
        doc_write = tools.get("office.docx.write")
        pptx_write = tools.get("office.pptx.write")
        check(
            "① 文档四工具注册（读 office:read 免审批 / 写 office:write 需审批）",
            doc_read is not None
            and doc_read["scope"] == "office:read"
            and doc_read["needs_approval"] is False
            and doc_write is not None
            and doc_write["needs_approval"] is True
            and pptx_write is not None
            and pptx_write["needs_approval"] is True,
        )

        # ② docx.read 直读（内容 + 表格 + 溯源；invoke 响应双层 data：外层 status/trace_id，内层为工具结果）
        body = invoke(client, auth_admin, "office.docx.read", {"path": "sample.docx"})
        data = ((body.get("data") or {}).get("data")) or {}
        check(
            "② office.docx.read 解析段落与表格（带 source 溯源）",
            body.get("ok") is True
            and "营收环比增长 12%" in " ".join(data.get("paragraphs", []))
            and data.get("tables")
            and str(data.get("source", "")).startswith("local-docs:"),
            str(body),
        )

        # ③ xlsx.read 直读（表头 + 数据行）
        body = invoke(client, auth_reviewer, "office.xlsx.read", {"path": "sample.xlsx"})
        data = ((body.get("data") or {}).get("data")) or {}
        check(
            "③ office.xlsx.read 读取表头与数据行",
            body.get("ok") is True
            and data.get("header") == ["月份", "销售额"]
            and data.get("counts", {}).get("row") == 2,
            str(body),
        )

        # ④ 路径穿越被拒（../ 越界访问防护）
        body = invoke(client, auth_admin, "office.docx.read", {"path": "../evil.docx"})
        check(
            "④ 路径穿越（../）被 400 拒绝",
            body.get("ok") is False and (body.get("error", {}).get("code") in (400, 1001)),
            str(body),
        )

        # ⑤ docx.write 走审批：invoke 只落单 → 同人 1001 → 复核员批准 → 文件真实落盘
        body = invoke(
            client,
            auth_admin,
            "office.docx.write",
            {
                "filename": "发布通知.docx",
                "title": "产品发布通知",
                "paragraphs": ["新版本将于周五发布", "请各团队提前完成回归验证"],
            },
        )
        approval_id = (body.get("data") or {}).get("approval_id")
        check(
            "⑤ docx.write invoke 只落审批单（不写盘）",
            body.get("ok") is True and approval_id,
            str(body),
        )
        check(
            "⑥ 同人批准被 1001 红线拦截且文件未落盘",
            decide(client, auth_admin, approval_id, True).get("error", {}).get("code") == 1001
            and not (docs / "发布通知.docx").exists(),
        )
        body = decide(client, auth_reviewer, approval_id, True)
        check(
            "⑦ 复核员批准后文件真实落盘",
            body.get("ok") is True and (docs / "发布通知.docx").exists(),
            str(body),
        )

        # ⑧ pptx.write 复核员驳回 → 文件不存在（写动作无直接生效通道）
        body = invoke(
            client,
            auth_admin,
            "office.pptx.write",
            {
                "filename": "周会汇报.pptx",
                "title": "周会汇报",
                "slides": [{"title": "本周进展", "bullets": ["审批闭环上线", "文档工具接入"]}],
            },
        )
        approval_id2 = (body.get("data") or {}).get("approval_id")
        body = decide(client, auth_reviewer, approval_id2, False)
        check(
            "⑧ pptx.write 被驳回后文件不存在（rejected 无直接生效）",
            body.get("ok") is True and not (docs / "周会汇报.pptx").exists(),
            str(body),
        )

    # 清理 smoke 产物（保持工作目录干净）
    for leftover in ("sample.docx", "sample.xlsx", "发布通知.docx"):
        (docs / leftover).unlink(missing_ok=True)
    if not any(docs.iterdir()):
        os.rmdir(docs)

    total, failed = 8, len(failures)
    print(
        f"RESULT: {total - failed}/{total} checks passed"
        + ("" if not failed else f"，失败：{failures}")
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
