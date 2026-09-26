"""ADR-0005 阶段二 HTTP 级冒烟（Milvus 向量库接入与三级降级链，可重复执行）：

口径：服务端配了 `MILVUS_URI` 且 Milvus 在跑 → 检索走向量库（retrieval_mode=milvus）；
未配或对端不可用 → 逐级降级（milvus → embedding 全量重算 → 字符 bigram），**任何情况都 code 0**。
本脚本对「当前服务端配置」做一致性断言，不硬编码期望通道：

① 工具清单口径未变：kb.ask / office.kb.search_unified 仍 office:read 免审批（治理零改动）；
② kb.ask 正常出参（code 0、命中非空、retrieval_mode 在允许集合、含 fallback_reason 字段）；
③ 指定期望通道时（第三参数）断言 retrieval_mode 精确匹配；
④ 连查两次：命中来源序一致且分数无口径级漂移（容差 0.01，见下注）；
⑤ search_unified 五源联查仍 code 0 且 knowledge 源命中（origin 过滤与跨源合并协同不炸）；
⑥ 降级原因如实标注：通道落到 bigram 时 fallback_reason 必须非空（不静默降级）。

用法：
    .venv\\Scripts\\python.exe tests\\smoke_milvus.py                 # 对当前服务配置断言
    .venv\\Scripts\\python.exe tests\\smoke_milvus.py http://127.0.0.1:8200/api/v1 milvus
        # 第三个参数显式期望通道（起 Milvus 后跑 milvus，停掉后跑 embedding / bigram 验降级）

运行前提：`.venv\\Scripts\\python.exe -m office_agent_server`（8200）已在跑。
"""

import sys

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OFFICE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8200/api/v1"
EXPECT_MODE = (sys.argv[2] if len(sys.argv) > 2 else "").strip()
ALLOWED_MODES = ("milvus", "embedding", "bigram")
QUERY = "报销超过1000元需要谁审批"
failures: list[str] = []


def check(step: str, cond: bool, detail: str = "") -> None:
    print(
        ("PASS | " if cond else "FAIL | ") + step + (f"（{detail}）" if detail and not cond else "")
    )
    if not cond:
        failures.append(step)


def login(c: httpx.Client, user: str, password: str) -> dict[str, str]:
    resp = c.post("/auth/login", json={"username": user, "password": password}).json()
    return {"Authorization": f"Bearer {resp['data']['token']}"}


def payload(resp: dict) -> dict:
    """invoke 信封出参在 data.result（外层是 tool/status/args 元信息）。"""
    return (resp.get("data") or {}).get("result") or {}


def ask(c: httpx.Client, headers: dict[str, str]) -> tuple[dict, dict]:
    """调 kb.ask 一次，返回 (整个信封, 业务出参)。"""
    body = c.post(
        "/agent/tools/kb.ask/invoke", headers=headers, json={"args": {"query": QUERY, "top_k": 3}}
    ).json()
    return body, payload(body)


with httpx.Client(base_url=OFFICE, trust_env=False, timeout=120) as c:
    admin = login(c, "admin", "admin123")

    tools = {t["name"]: t for t in c.get("/agent/tools", headers=admin).json()["data"]["items"]}
    ask_spec = tools.get("kb.ask")
    unified = tools.get("office.kb.search_unified")
    check(
        "① 工具清单口径未变：kb.ask / search_unified 仍 office:read 免审批",
        ask_spec is not None
        and ask_spec["scope"] == "office:read"
        and ask_spec["requires_approval"] is False
        and unified is not None
        and unified["scope"] == "office:read"
        and unified["requires_approval"] is False,
        str({k: tools.get(k) for k in ("kb.ask", "office.kb.search_unified")})[:300],
    )

    envelope, first = ask(c, admin)
    mode = str(first.get("retrieval_mode") or "")
    check(
        "② kb.ask 正常出参（code 0 / 命中非空 / 通道在允许集合 / 含降级原因字段）",
        envelope.get("code") == 0
        and first.get("count", 0) >= 1
        and mode in ALLOWED_MODES
        and isinstance(first.get("retrieval_fallback_reason"), str),
        str({k: first.get(k) for k in ("count", "retrieval_mode", "retrieval_fallback_reason")})[
            :300
        ],
    )

    if EXPECT_MODE:
        check(
            f"③ 显式期望通道 retrieval_mode={EXPECT_MODE}",
            mode == EXPECT_MODE,
            f"实际 {mode}；fallback={str(first.get('retrieval_fallback_reason'))[:160]}",
        )
    print(f"INFO | 通道={mode}；降级原因={first.get('retrieval_fallback_reason') or '（无）'}")

    _, second = ask(c, admin)
    first_hits = [(x.get("source"), x.get("score")) for x in first.get("results") or []]
    second_hits = [(x.get("source"), x.get("score")) for x in second.get("results") or []]
    # 分数容差 0.01：embedding 通道每次现算，对端模型对不同批次返回的浮点并非逐位确定
    # （实测同输入两次 0.6445 / 0.6452），故只断言「来源序一致 + 分数无口径级漂移」。
    check(
        "④ 连查两次命中来源与分数稳定（增量灌库不破坏检索口径）",
        bool(first_hits)
        and [s for s, _ in first_hits] == [s for s, _ in second_hits]
        and all(abs(a[1] - b[1]) <= 0.01 for a, b in zip(first_hits, second_hits, strict=True)),
        f"first={first_hits} second={second_hits}",
    )

    body = c.post(
        "/agent/tools/office.kb.search_unified/invoke",
        headers=admin,
        json={"args": {"query": QUERY}},
    ).json()
    merged = payload(body)
    check(
        "⑤ search_unified 五源联查仍 code 0 且 knowledge 源命中（通道如实标注）",
        body.get("code") == 0
        and merged.get("merged_count", 0) >= 1
        and (merged.get("per_source") or {}).get("knowledge", {}).get("count", 0) >= 1
        and merged.get("retrieval_mode") in ALLOWED_MODES,
        str({k: merged.get(k) for k in ("merged_count", "retrieval_mode")})[:300],
    )

    reason = str(merged.get("retrieval_fallback_reason") or "")
    check(
        "⑥ 降级原因如实标注（落到 bigram 时非空，不静默降级）",
        merged.get("retrieval_mode") != "bigram" or bool(reason),
        f"mode={merged.get('retrieval_mode')} reason={reason[:200]}",
    )

print()
if failures:
    print(f"FAILED：{len(failures)} 项未通过 → {failures}")
    sys.exit(1)
print("ALL PASS：阶段二 Milvus 接入与三级降级链 HTTP 级冒烟通过")
