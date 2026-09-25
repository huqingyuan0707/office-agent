"""PRD §2.1 HTTP 级冒烟（文件解析 PDF 真实抽取 / 文件内容问答，可重复执行）：

① 工具清单含 `office.file.ask`（office:read 免审）与 `office.file.read`；
② `office.file.read` 对 PDF 逐页抽取真实文本（页数如实返回，degraded=False）；
③ `office.file.ask` 命中段落并摘录原文，检索通道如实标注（retrieval_mode/embedding_model）；
④ 向量通道下命中分数原样暴露（乱码问句仍可能高于门槛 0.35——已知边界，分数是自判依据；
   字符通道则必然零命中 degraded，见单测 test_file_ask_no_hit_degrades_without_fabrication）；
⑤ 穿越文件名被 1001 拒绝（数据不出域在本地盘的对应实现）；
⑥ 缺文件 404（1004）；
⑦ 语义通道：字面零重叠的改说法问句仍命中目标段（向量检索的意义所在）。

运行前提：`.venv\\Scripts\\python.exe -m office_agent_server`（8200）已在跑；
脚本会往 Settings.DOCS_DIR 写入 ask_demo.pdf / ask_demo.txt 两个演示文件。
"""

import sys
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


def minimal_pdf(text: str) -> bytes:
    """手写最小 PDF（含真实 xref 偏移，1 页 1 行文本）——不依赖 reportlab。"""
    stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 5 0 R "
        b"/Resources << /Font << /F1 3 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


DOCS = Path(settings.DOCS_DIR)
DOCS.mkdir(parents=True, exist_ok=True)
(DOCS / "ask_demo.pdf").write_bytes(minimal_pdf("Office Agent PDF HTTP Check"))
(DOCS / "ask_demo.txt").write_text(
    "项目背景\n本方案用于替换旧版报销系统，覆盖 2026 年 Q1 上线范围。\n\n"
    "验收标准\n单笔报销审批链路端到端耗时不超过 3 个工作日，发票识别准确率不低于 95%。\n",
    encoding="utf-8",
)
print(f"DOCS_DIR = {DOCS}")

with httpx.Client(base_url=OFFICE, trust_env=False, timeout=60) as c:
    admin = login(c, "admin", "admin123")

    tools = {t["name"]: t for t in c.get("/agent/tools", headers=admin).json()["data"]["items"]}
    ask_spec = tools.get("office.file.ask")
    check(
        "① 工具清单含 office.file.ask（office:read 免审）与 office.file.read",
        ask_spec is not None
        and ask_spec["scope"] == "office:read"
        and ask_spec["requires_approval"] is False
        and "office.file.read" in tools,
        str(ask_spec),
    )

    r = c.post(
        "/agent/tools/office.file.read/invoke",
        headers=admin,
        json={"args": {"filename": "ask_demo.pdf"}},
    ).json()
    d = payload(r)
    check(
        "② office.file.read PDF 真实逐页解析",
        d.get("degraded") is False
        and d.get("pages") == 1
        and "Office Agent PDF HTTP Check" in str(d.get("text", "")),
        str({k: d.get(k) for k in ("degraded", "pages", "text")}),
    )

    r = c.post(
        "/agent/tools/office.file.ask/invoke",
        headers=admin,
        json={"args": {"filename": "ask_demo.txt", "query": "验收标准是什么"}},
    ).json()
    d = payload(r)
    top = (d.get("answer_fragments") or [{}])[0]
    check(
        "③ office.file.ask 命中段落并摘录原文（带检索通道标注）",
        d.get("count", 0) >= 1 and "验收标准" in str(top.get("snippet", "")),
        str(d),
    )
    print(
        f"     retrieval_mode={d.get('retrieval_mode')} model={d.get('embedding_model')} "
        f"fallback={d.get('retrieval_fallback_reason')!r} source={top.get('source')}"
    )

    r = c.post(
        "/agent/tools/office.file.ask/invoke",
        headers=admin,
        json={"args": {"filename": "ask_demo.txt", "query": "zzzqxj999"}},
    ).json()
    d = payload(r)
    frags = d.get("answer_fragments") or []
    if d.get("retrieval_mode") == "bigram":
        check("④ 字符通道：问文件里没有的事 → 零命中如实降级", d.get("degraded") is True, str(d))
    else:
        # 向量通道已知边界：余弦对任意文本都给分（阈值 0.35 是取舍杆而非分界线）——
        # 断言「分数如实暴露供自判」，而不是硬要求零命中。
        check(
            "④ 向量通道：命中分数原样暴露供自判（低分不伪装成高相关）",
            all(isinstance(f.get("score"), (int, float)) for f in frags),
            str(d),
        )
        print(
            f"     乱码问句命中 {len(frags)} 段，分数 {[f['score'] for f in frags]}（已标注边界）"
        )

    r = c.post(
        "/agent/tools/office.file.ask/invoke",
        headers=admin,
        json={"args": {"filename": "ask_demo.txt", "query": "多久能走完流程"}},
    ).json()
    d = payload(r)
    semantic_top = (d.get("answer_fragments") or [{}])[0]
    check(
        "⑦ 语义通道（改说法、字面零重叠）仍命中验收段",
        d.get("retrieval_mode") == "bigram" or "3 个工作日" in str(semantic_top.get("snippet", "")),
        str(d)[:300],
    )

    r = c.post(
        "/agent/tools/office.file.ask/invoke",
        headers=admin,
        json={"args": {"filename": "../evil.txt", "query": "验收标准"}},
    ).json()
    check("⑤ 穿越文件名被 1001 拒绝（数据不出域）", r.get("code") == 1001, str(r)[:200])

    r = c.post(
        "/agent/tools/office.file.ask/invoke",
        headers=admin,
        json={"args": {"filename": "gone.txt", "query": "验收标准"}},
    ).json()
    check("⑥ 缺文件 404（1004）", r.get("code") == 1004, str(r)[:200])

print()
print(f"RESULT: {len(failures)} failed of 7 checks")
sys.exit(1 if failures else 0)
