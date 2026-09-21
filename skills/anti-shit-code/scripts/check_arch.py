"""架构健康检查（防屎山硬门禁，对齐 skill anti-shit-code §2/§5，office-agent 适配版）

链路：扫描后端分层违规（路由层触 DB、单文件超长）+ 前端越层（views/components 直写
fetch/axios）→ 与棘轮基线比对（只准减不准增）→ 输出 PASS/FAIL + RESULT → 退出码反映成败。

用法：python skills/anti-shit-code/scripts/check_arch.py [repo_root]
适配自电商仓库同名脚本（规则口径不变，扫描根改为本仓库多包结构；
路由判据放宽为父目录名 endpoints/api 均算）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Windows 控制台中文兜底
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined, union-attr]

# ---------------- 扫描根（office-agent 多包结构） ----------------
PY_ROOTS = ("office_agent", "packages")
FE_SRC = Path("apps") / "web" / "src"

MAX_FILE_LINES = 400

# 路由层内禁止出现的 DB 操作（import 行除外）
DB_OP_RE = re.compile(r"\bdb\.(execute|add|commit|flush|scalar|scalars)\b|\bselect\(")
# views/components 内禁止的前端直连
FRONT_FETCH_RE = re.compile(r"\bfetch\s*\(|from ['\"]axios['\"]")

# 棘轮基线：(相对路径, 规则名) -> 允许的最大违规数。清零后请删除对应条目并收紧。
# 初始化记录（v1.0）：packages/server/office_agent_server/api/* 为既有代码的路由层
# 直触 DB 债务，按「存量只准减不准增」入基线，触碰对应文件时优先抽 service 层偿还。
BASELINE: dict[tuple[str, str], int] = {
    ("packages/server/office_agent_server/api/approvals.py", "endpoint-db-op"): 6,
    ("packages/server/office_agent_server/api/auth.py", "endpoint-db-op"): 1,
    ("packages/server/office_agent_server/api/governance.py", "endpoint-db-op"): 2,
    ("packages/server/office_agent_server/api/tasks.py", "endpoint-db-op"): 2,
    ("packages/server/office_agent_server/api/tools.py", "endpoint-db-op"): 1,
}

# file-too-long 的棘轮语义：按「当前允许行数」记名——超预算即红，
# 文件拆小后提示把预算收到新行数，清零后删条目。
FILE_LINE_BUDGET: dict[str, int] = {}

RULE_FILE_TOO_LONG = "file-too-long"
RULE_ENDPOINT_DB = "endpoint-db-op"
RULE_FRONT_FETCH = "front-direct-fetch"


def find_repo_root(start: Path) -> Path | None:
    """从脚本目录向上找到仓库根（判据：office_agent 包或 packages/core 存在）。"""
    for candidate in [start, *start.parents]:
        if (candidate / "office_agent").is_dir() or (candidate / "packages" / "core").is_dir():
            return candidate
    return None


def _iter_backend_py(root: Path) -> list[Path]:
    """汇总全部后端包的 py 文件（排除缓存/虚拟环境/node_modules）。"""
    files: list[Path] = []
    for sub in PY_ROOTS:
        base = root / sub
        if not base.is_dir():
            continue
        files.extend(
            py
            for py in base.rglob("*.py")
            if not any(
                part in {"__pycache__", ".venv", "node_modules", "build", "dist", ".trae"}
                for part in py.parts
            )
        )
    return sorted(set(files))


def scan_py_files(root: Path) -> list[tuple[str, str, int]]:
    """扫描后端：超长文件 + 路由层触 DB。返回 (相对路径, 规则, 行号) 违规列表。"""
    violations: list[tuple[str, str, int]] = []
    for py in _iter_backend_py(root):
        rel = py.relative_to(root).as_posix()
        lines = py.read_text(encoding="utf-8").splitlines()
        if len(lines) > MAX_FILE_LINES:
            violations.append((rel, RULE_FILE_TOO_LONG, len(lines)))
        if py.parent.name in {"endpoints", "api"} or py.stem in {"main", "routes", "app"}:
            for lineno, line in enumerate(lines, 1):
                stripped = line.lstrip()
                if stripped.startswith(("import ", "from ")):
                    continue
                if DB_OP_RE.search(line):
                    violations.append((rel, RULE_ENDPOINT_DB, lineno))
    return violations


def scan_frontend(root: Path) -> list[tuple[str, str, int]]:
    """扫描前端 views/components：禁止直写 fetch / import axios（api 层是唯一入口）。"""
    violations: list[tuple[str, str, int]] = []
    src = root / FE_SRC
    if not src.is_dir():
        return violations
    for sub in ("views", "components"):
        base = src / sub
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            if f.suffix not in {".vue", ".ts"}:
                continue
            rel = f.relative_to(root).as_posix()
            for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.lstrip()
                if stripped.startswith(("//", "* ", "/*")):
                    continue
                if FRONT_FETCH_RE.search(line):
                    violations.append((rel, RULE_FRONT_FETCH, lineno))
    return violations


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    root = Path(arg).resolve() if arg else find_repo_root(Path(__file__).resolve().parent)
    if root is None or not root.is_dir():
        print("FAIL | 未找到仓库根（需含 office_agent 或 packages/core），可传路径参数")
        return 2

    violations = scan_py_files(root) + scan_frontend(root)

    # 按 (文件, 规则) 聚合后与棘轮基线比对
    counts: dict[tuple[str, str], list[int]] = {}
    for rel, rule, lineno in violations:
        counts.setdefault((rel, rule), []).append(lineno)

    failures: list[str] = []
    debt = 0
    for key, linenos in sorted(counts.items()):
        rel, rule = key
        if rule == RULE_FILE_TOO_LONG:
            budget = FILE_LINE_BUDGET.get(rel, MAX_FILE_LINES)
            if linenos[0] > budget:
                tag = f"超出基线预算 {budget}" if rel in FILE_LINE_BUDGET else "新增"
                failures.append(f"{rel} [{rule}] {tag}：行 {linenos}")
            else:
                debt += 1
            continue
        allowed = BASELINE.get(key, 0)
        if len(linenos) > allowed:
            tag = "新增" if allowed == 0 else f"超出基线 {allowed}"
            failures.append(f"{rel} [{rule}] {tag}：行 {linenos}")
        debt += min(len(linenos), allowed)

    for rel, budget in sorted(FILE_LINE_BUDGET.items()):
        current = counts.get((rel, RULE_FILE_TOO_LONG))
        if current and current[0] < budget:
            print(f"NOTE | 超长文件已拆小，可收紧预算：{rel}（{budget} → {current[0]}）")
        elif not current:
            print(f"NOTE | 已回到 {MAX_FILE_LINES} 行内，可删除预算条目：{rel}")
    for key, allowed in sorted(BASELINE.items()):
        if key not in counts:
            print(f"NOTE | 基线债务已清零，可收紧：{key[0]} [{key[1]}]（{allowed}）")

    for msg in failures:
        print(f"FAIL | {msg}")
    if not failures:
        print("PASS | 分层与体量检查全部通过（存量债务未增加）")
    print(f"RESULT: {len(violations) - debt} new violation(s), {debt} baseline debt, {len(failures)} failed check(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
