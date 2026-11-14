"""AST 分层守卫（重构方案 §3.1 边界验收清单）：静态扫描 src/bidqa 的 import 图钉死依赖边界。

模块化不靠自觉：本测试把「谁可以依赖谁」变成可执行断言。两条保障——
1. 实际违规必须 ⊆ ALLOWLIST（新增未登记的依赖 → 红）；
2. ALLOWLIST 必须与实际违规完全一致（修好了却忘删条目 → 红，防 allowlist 腐化）。
每条 allowlist 条目注明 owner 与移除步骤；全部清零后过渡规则保留为永久护栏。

实现口径：
- 模块名统一用**去 bidqa 前缀的短名**（api.system / agent.loop），import 目标同样归一；
- `from pkg import attr` 会产生「属性并非子模块」的幻影目标（如 api.system.note_request），
  违规键按规则的**匹配前缀归一**（统一记成 api.system），不会一条违规记两条；
- 相对导入按 Python 语义解析：1 点=当前包，每多 1 点再上跳一级（自检覆盖三级上跳）；
- 规则自检用合成代码片段验证探测器「能红」（纯内存 AST，不触碰真实源码）。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Callable

SRC = Path(__file__).resolve().parents[1] / "src" / "bidqa"

# ── 模块分组（短名前缀 → 角色）───────────────────────────────────────
BUSINESS_PREFIXES = ("agent", "tools", "rag", "clients", "database",
                     "ingest", "eval", "obs", "auth")  # 业务/适配层
ENTRY_PREFIXES = ("main", "cli")  # 入口：允许 import 一切
API_PREFIX = "api"


def _module_name(path: Path) -> str:
    """文件路径 → 去 bidqa 前缀的短模块名（包根 __init__ → ""，跳过）。"""
    rel = path.relative_to(SRC.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if parts == ["bidqa"]:
        return ""
    assert parts[0] == "bidqa"
    return ".".join(parts[1:])


def _short(name: str) -> str:
    """bidqa.x.y → x.y；裸 bidqa → ""。"""
    if name.startswith("bidqa."):
        return name[len("bidqa."):]
    return "" if name == "bidqa" else name


def _collect_imports(tree: ast.AST, module: str) -> set[str]:
    """AST 提取全部 bidqa 导入目标（短名；含函数内延迟导入，如 note_request）。"""
    found: set[str] = set()
    pkg_parts = module.split(".")[:-1] if module else []
    for node in ast.walk(tree):
        target: str | None = None
        if isinstance(node, ast.Import):
            for alias in node.names:
                short = _short(alias.name)
                if short:
                    found.add(short)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:  # 相对导入：1 点=当前包，每多 1 点再上跳一级
            if node.level <= len(pkg_parts) + 1:
                prefix = ".".join(pkg_parts[:len(pkg_parts) - (node.level - 1)])
                target = f"{prefix}.{node.module}" if prefix and node.module else (
                    prefix or node.module or None)
            else:  # 上跳超出包根：非法导入，交给运行时报错，这里忽略
                target = None
        elif node.module:
            target = _short(node.module)
        if not target:
            continue
        found.add(target)
        for alias in node.names:  # from api import health → api.health（子模块展开）
            if alias.name != "*":
                found.add(f"{target}.{alias.name}")
    return found


def _load_imports() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path in SRC.rglob("*.py"):
        module = _module_name(path)
        if not module:
            continue
        out[module] = _collect_imports(ast.parse(path.read_text(encoding="utf-8"),
                                                filename=str(path)), module)
    return out


def _in(pkg: str, name: str) -> bool:
    """name 是否属于包 pkg（含 pkg 自身与其子模块）。"""
    return name == pkg or name.startswith(pkg + ".")


def _prefix_matcher(pkg: str) -> Callable[[str], str | None]:
    """命中则返回归一化违规键（= 包前缀本身），未命中返回 None。"""
    return lambda t: pkg if _in(pkg, t) else None


# ── 规则与 allowlist（条目须注明 owner/移除步骤；双向一致由 test_allowlist_matches 断言）──
ALLOWLIST: dict[str, set[tuple[str, str]]] = {
    # 过渡：bidqa.service 组合根按职责拆入窄 factory（重构方案 §4.1）
    "R6-api不得import组合根service": {
        ("api.ask", "service"),            # C3: application/ask + rag/clients factory
        ("api.chat_stream", "service"),    # C3: application/chat + database/rag factory
        ("api.vision", "service"),         # C3: application/vision + clients/chat_factory
        ("api.auth", "service"),           # C4: auth/factory
        ("api.middleware", "service"),     # C4: auth/factory
        ("api.conversations", "service"),  # C4: database/factory（会话存储）
        ("api.health", "service"),         # C4: database/rag factory
        ("api.system.router", "service"),  # C4: application/admin
    },
    # 过渡：图片 ref store 归 application/vision（C3 迁移后清零）
    "R5-chat不得依赖api.vision": {
        ("api.chat_stream", "api.vision"),  # C3: image_ref 读取改 application 接口
    },
    # 业务路由 import 观测路由（note_request）：C1 pilot 已收口至 obs——空集存档
    "R4-业务路由不得import观测路由": set(),
    # 业务层反向依赖 HTTP/CLI/组合根：现状为零违规——永久护栏
    "R1-业务层不得依赖api-cli-container-service": set(),
    "R2-obs不得依赖api": set(),
    "R3-api不得依赖cli": set(),
}

_RULES: list[tuple[str, Callable[[str], bool], Callable[[str], str | None]]] = [
    (r"R1-业务层不得依赖api-cli-container-service",
     lambda m: any(_in(p, m) for p in BUSINESS_PREFIXES),
     lambda t: next((x for x in ("api", "cli", "container", "service") if _in(x, t)), None)),
    ("R2-obs不得依赖api",
     lambda m: _in("obs", m), _prefix_matcher("api")),
    ("R3-api不得依赖cli",
     lambda m: _in(API_PREFIX, m), _prefix_matcher("cli")),
    ("R4-业务路由不得import观测路由",
     lambda m: _in(API_PREFIX, m) and not _in("api.system", m),
     _prefix_matcher("api.system")),
    ("R5-chat不得依赖api.vision",
     # 过渡期：chat 运行时还在 api/chat_stream.py（C3 起为 api/chat/ 包），两者都管
     lambda m: _in("api.chat", m) or m == "api.chat_stream",
     _prefix_matcher("api.vision")),
    ("R6-api不得import组合根service",
     lambda m: _in(API_PREFIX, m), lambda t: "service" if t == "service" else None),
]


def _raw_violations(imports: dict[str, set[str]]) -> dict[str, set[tuple[str, str]]]:
    bad: dict[str, set[tuple[str, str]]] = {rule: set() for rule, _, _ in _RULES}
    for module, targets in imports.items():
        for rule, source_ok, match in _RULES:
            if not source_ok(module):
                continue
            for target in targets:
                key = match(target)
                if key is not None:
                    bad[rule].add((module, key))
    return bad


def test_no_undocumented_dependency_violations() -> None:
    """实际违规必须逐条登记在 ALLOWLIST（新增依赖未登记 → 红）。"""
    raw = _raw_violations(_load_imports())
    unexpected = {rule: bad - ALLOWLIST.get(rule, set())
                  for rule, bad in raw.items() if bad - ALLOWLIST.get(rule, set())}
    assert not unexpected, f"存在未登记的分层违规（先改结构或登记 allowlist）: {unexpected}"


def test_allowlist_matches_reality_exactly() -> None:
    """ALLOWLIST 与实际违规完全一致：修好违规须同步删条目（防 allowlist 腐化）。"""
    raw = _raw_violations(_load_imports())
    stale = {rule: sorted(entries - raw.get(rule, set()))
             for rule, entries in ALLOWLIST.items() if entries - raw.get(rule, set())}
    assert not stale, f"allowlist 存在已失效条目（违规已修复，请删除登记）: {stale}"


# ── 探测器自检：合成片段验证每条规则「能红」（纯内存 AST，不触碰真实源码）──
def _detect(source: str, module: str, rule: str) -> bool:
    raw = _raw_violations({module: _collect_imports(ast.parse(source), module)})
    return bool(raw.get(rule))


def test_detector_catches() -> None:
    biz_rule = "R1-业务层不得依赖api-cli-container-service"
    assert _detect("from bidqa.api.system import note_request", "rag.foo", biz_rule)
    assert _detect("from bidqa.service import build_pg", "agent.loop", biz_rule)
    assert _detect("import bidqa.cli", "clients.llm", biz_rule)
    assert _detect("from bidqa.api import health", "database.factory", biz_rule)
    assert _detect("from bidqa.container import build_x", "tools.base", biz_rule)
    assert _detect("from bidqa.api.metrics import x", "obs.logs", "R2-obs不得依赖api")
    assert _detect("from bidqa.cli import main", "api.auth", "R3-api不得依赖cli")
    assert _detect("from bidqa.api.system import note_request",
                   "api.chat_stream", "R4-业务路由不得import观测路由")
    assert not _detect("from bidqa.api.system import router",
                       "api.system", "R4-业务路由不得import观测路由")  # 自身豁免
    assert not _detect("from bidqa.api.system import router",
                       "main", "R4-业务路由不得import观测路由")  # 入口豁免
    assert _detect("from bidqa.api.vision import get_ref_store",
                   "api.chat.worker", "R5-chat不得依赖api.vision")
    assert _detect("from bidqa.service import build_chat",
                   "api.chat.stream", "R6-api不得import组合根service")
    # 相对导入按 Python 语义：api.chat.stream 里 1 点=api.chat、2 点=api、3 点=包根
    assert not _detect("from ..service import x", "api.chat.stream",
                       "R6-api不得import组合根service")  # ..service=api.service（api 内部，不归 R6）
    assert _detect("from ...service import build_pg", "api.chat.stream",
                   "R6-api不得import组合根service")  # 三级上跳到包根 service
    assert _detect("from ..vision import get_ref_store", "api.chat.worker",
                   "R5-chat不得依赖api.vision")  # 两级上跳 = api.vision
    assert not _detect("from .vision import get_ref_store", "api.chat.worker",
                       "R5-chat不得依赖api.vision")  # 一级=api.chat.vision（chat 包内部）
    # 幻影目标归一：函数导入不得把同一违规记成两条（api.system 与 api.system.note_request 同键）
    raw = _raw_violations({"api.ask": _collect_imports(
        ast.parse("from bidqa.api.system import note_request"), "api.ask")})
    assert raw["R4-业务路由不得import观测路由"] == {("api.ask", "api.system")}
