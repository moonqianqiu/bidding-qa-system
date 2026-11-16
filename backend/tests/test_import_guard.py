"""导入重量级依赖守卫（AGENTS「测试要求」：此类断言必须在**子进程**里做）。

本进程跑 pytest 时 `fastapi`/`jieba` 往往已被别的用例导入，进程内断言「没导入」
恒绿——审计 P2-45 指出全库从无 `subprocess`/`sys.executable`，设施缺失。
子进程从零导入，才是真守卫；新增守卫照抄本样板。

样板覆盖两条既有设计承诺：
1. `bidqa.errors` 是各层共用纯契约——不得传递性依赖 FastAPI（v2.2 拆分口径，
   见 errors.py docstring）；
2. `bidqa.cli.main` 是运维入口——导入期不得拉起 ingest / 评测重依赖（重活
   必须在命令分支内惰性导入）。
"""

from __future__ import annotations

import json
import subprocess
import sys

_HEAVY = {"jieba", "mineru", "pdfplumber", "fitz", "ragas", "numpy"}


def _imported_top_levels(*modules: str) -> set[str]:
    code = (
        "import json, sys\n"
        f"for name in {modules!r}:\n"
        "    __import__(name)\n"
        "print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"子进程导入失败：{proc.stderr}"
    return set(json.loads(proc.stdout.strip().splitlines()[-1]))


def test_errors_module_does_not_pull_fastapi() -> None:
    top = _imported_top_levels("bidqa.errors")
    assert "bidqa" in top  # 子进程确实导入到了目标包（防路径错误导致的空集假绿）
    assert "fastapi" not in top, "bidqa.errors 传递性依赖了 FastAPI（分层契约破坏）"


def test_cli_main_stays_light() -> None:
    top = _imported_top_levels("bidqa.cli.main")
    assert "bidqa" in top
    leaked = _HEAVY & top
    assert not leaked, f"bidqa.cli.main 导入期拉起了重依赖：{sorted(leaked)}"
