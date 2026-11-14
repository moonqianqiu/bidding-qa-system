"""落盘隔离哨兵（conftest 兜底逻辑的回归测试）。

背景：个别沙箱缺 TEMP/TMP 时 tempfile.gettempdir() 兜底到当前目录，
jieba 缓存 / pytest 临时根 / mkdtemp 产物全部落进 backend/（AGENTS 落盘红线）。
"""

import tempfile
from pathlib import Path


def test_tempdir_not_inside_repo() -> None:
    repo = Path(__file__).resolve().parents[2]
    temp_root = Path(tempfile.gettempdir()).resolve()
    assert not temp_root.is_relative_to(repo), f"tempfile 根落在仓库内：{temp_root}"
