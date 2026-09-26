"""PRD §2.6 HTTP 级冒烟（知识库权限适配 / 跨源联合检索 / 图片问答，可重复执行）：

① 工具清单含 `office.kb.search_unified` 与 `office.image.ask`（office:read 免审）；
② `kb.ask` 新增制度条目可查（试用期→人事、泄密→合规），出参含 permission_filtered 与 visibility；
③ `kb.ask` 受限条目（薪酬保密，visibility=hr）对 admin 可见且如实标注；
④ `search_unified` 全源联查（报销→ merged 非空，knowledge 源命中，通道如实标注）；
⑤ `search_unified` sources 限定（data 源查官网改版，merged origin 全为 data）；
⑥ `search_unified` docs 源命中 DOCS_DIR 演示文件；
⑦ `office.image.ask` 真实图片元数据 + 引擎缺失如实降级（装了 Tesseract 则走识别分支断言另一组）；
⑧ 非法 sources 与穿越文件名被 1001 拒绝。

运行前提：`.venv\\Scripts\\python.exe -m office_agent_server`（8200）已在跑；
脚本会往 Settings.DOCS_DIR 写入 kb26_sop.txt / kb26_shot.png 两个演示文件。
"""

import sys
from pathlib import Path

import httpx
from PIL import Image

from office_agent_core.settings import settings

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
OFFICE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8200/api/v1"
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


DOCS = Path(settings.DOCS_DIR)
DOCS.mkdir(parents=True, exist_ok=True)
(DOCS / "kb26_sop.txt").write_text(
    "蓝牙配对流程\n长按耳机电源键五秒进入配对模式，指示灯红蓝交替闪烁。\n",
    encoding="utf-8",
)
Image.new("RGB", (120, 40), "white").save(DOCS / "kb26_shot.png")
print(f"DOCS_DIR = {DOCS}")

with httpx.Client(base_url=OFFICE, trust_env=False, timeout=120) as c:
    admin = login(c, "admin", "admin123")

    tools = {t["name"]: t for t in c.get("/agent/tools", headers=admin).json()["data"]["items"]}
    unified_spec = tools.get("office.kb.search_unified")
    image_spec = tools.get("office.image.ask")
    check(
        "① 工具清单含 search_unified 与 image.ask（office:read 免审）",
        unified_spec is not None
        and unified_spec["scope"] == "office:read"
        and unified_spec["requires_approval"] is False
        and image_spec is not None
        and image_spec["scope"] == "office:read"
        and image_spec["requires_approval"] is False,
        str({k: tools.get(k) for k in ("office.kb.search_unified", "office.image.ask")}),
    )

    r = c.post(
        "/agent/tools/kb.ask/invoke",
        headers=admin,
        json={"args": {"query": "试用期", "top_k": 3}},
    ).json()
    d = payload(r)
    top = (d.get("results") or [{}])[0]
    check(
        "② kb.ask 新增制度条目可查（试用期→人事，含 permission_filtered/visibility）",
        d.get("count", 0) >= 1
        and "人事" in str(top.get("title", ""))
        and isinstance(d.get("permission_filtered"), int)
        and "visibility" in top,
        str(d)[:300],
    )

    r = c.post(
        "/agent/tools/kb.ask/invoke",
        headers=admin,
        json={"args": {"query": "薪酬", "top_k": 5}},
    ).json()
    d = payload(r)
    hit = next((x for x in d.get("results") or [] if "薪酬" in str(x.get("title", ""))), {})
    check(
        "③ kb.ask 受限条目对 admin 可见且标注 visibility=hr",
        bool(hit) and hit.get("visibility") == "hr",
        str(d)[:300],
    )

    r = c.post(
        "/agent/tools/office.kb.search_unified/invoke",
        headers=admin,
        json={"args": {"query": "报销超过1000元需要谁审批"}},
    ).json()
    d = payload(r)
    check(
        "④ search_unified 全源联查（merged 非空 + knowledge 命中 + 通道标注）",
        d.get("merged_count", 0) >= 1
        and (d.get("per_source") or {}).get("knowledge", {}).get("count", 0) >= 1
        and d.get("retrieval_mode") in ("bigram", "embedding", "milvus")
        and isinstance(d.get("permission_filtered"), int),
        str({k: d.get(k) for k in ("merged_count", "retrieval_mode")}),
    )
    print(f"     mode={d.get('retrieval_mode')} merged_top={d.get('merged', [{}])[0]}")

    r = c.post(
        "/agent/tools/office.kb.search_unified/invoke",
        headers=admin,
        json={"args": {"query": "官网改版", "sources": ["data"]}},
    ).json()
    d = payload(r)
    merged = d.get("merged") or []
    check(
        "⑤ search_unified sources 限定 data（merged origin 全为 data）",
        d.get("merged_count", 0) >= 1
        and set(d.get("sources") or []) == {"data"}
        and all(m.get("origin") == "data" for m in merged),
        str(d)[:300],
    )

    r = c.post(
        "/agent/tools/office.kb.search_unified/invoke",
        headers=admin,
        json={"args": {"query": "蓝牙配对", "sources": ["docs"]}},
    ).json()
    d = payload(r)
    docs_top = ((d.get("per_source") or {}).get("docs") or {}).get("results") or [{}]
    check(
        "⑥ search_unified docs 源命中演示文件",
        "kb26_sop.txt" in str(docs_top[0].get("source", "")),
        str(d)[:300],
    )

    r = c.post(
        "/agent/tools/office.image.ask/invoke",
        headers=admin,
        json={"args": {"file_path": "kb26_shot.png", "query": "图里有什么"}},
    ).json()
    d = payload(r)
    meta = d.get("image") or {}
    if d.get("retrieval_mode") == "none":
        check(
            "⑦ image.ask 引擎缺失如实降级（真实元数据 + 可操作原因）",
            d.get("degraded") is True
            and meta.get("width") == 120
            and meta.get("height") == 40
            and bool(d.get("degraded_reason")),
            str(d)[:300],
        )
    else:
        frags = d.get("answer_fragments") or []
        check(
            "⑦ image.ask 引擎可用走识别问答（元数据真实 + 片段可溯源）",
            meta.get("width") == 120 and all("source" in f for f in frags),
            str(d)[:300],
        )
    print(f"     retrieval_mode={d.get('retrieval_mode')} degraded={d.get('degraded')}")

    r = c.post(
        "/agent/tools/office.kb.search_unified/invoke",
        headers=admin,
        json={"args": {"query": "报销", "sources": ["nope"]}},
    ).json()
    bad_source = r.get("code") == 1001
    r = c.post(
        "/agent/tools/office.image.ask/invoke",
        headers=admin,
        json={"args": {"file_path": "../evil.png", "query": "图里有什么"}},
    ).json()
    traversal = r.get("code") == 1001
    check(
        "⑧ 非法 sources 与穿越文件名被 1001 拒绝",
        bad_source and traversal,
        f"{bad_source}/{traversal}",
    )

print()
print(f"RESULT: {len(failures)} failed of 8 checks")
sys.exit(1 if failures else 0)
