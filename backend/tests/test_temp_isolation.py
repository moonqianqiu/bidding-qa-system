"""落盘隔离哨兵（conftest 兜底逻辑的回归测试）。

背景：个别沙箱缺 TEMP/TMP 时 tempfile.gettempdir() 兜底到当前目录，
jieba 缓存 / pytest 临时根 / mkdtemp 产物全部落进 backend/（AGENTS 落盘红线）。

审计 P2-1：旧实现用 `os.access(W_OK)` 挑候选目录——Windows 只查只读属性不查
ACL，`C:\\Windows\\Temp` 这类对当前用户不可写的目录被误判可写并钉成 tempfile 根
（34 个用例 error 的根因）；显式 TMPDIR 也不参与候选。现改为「真实写探测」。
"""

import os
import tempfile
from pathlib import Path

from conftest import _pick_tempdir, _pin_tempdir_outside_repo


def test_tempdir_not_inside_repo() -> None:
    repo = Path(__file__).resolve().parents[2]
    temp_root = Path(tempfile.gettempdir()).resolve()
    assert not temp_root.is_relative_to(repo), f"tempfile 根落在仓库内：{temp_root}"


def test_pick_skips_acl_denied_candidate(monkeypatch, tmp_path) -> None:
    """ACL 拒绝写入的候选必须被跳过（写探测，而非 os.access 的假阳性）。"""
    bad = tmp_path / "windows_temp"
    good = tmp_path / "localappdata_temp"
    bad.mkdir()
    good.mkdir()
    bad_abs = os.path.abspath(bad)
    real_mkstemp = tempfile.mkstemp

    def fake_mkstemp(*, dir=None, prefix=""):
        if dir and os.path.abspath(dir) == bad_abs:
            raise PermissionError(13, "ACL denies write")
        return real_mkstemp(dir=dir, prefix=prefix)

    monkeypatch.setattr(tempfile, "mkstemp", fake_mkstemp)
    monkeypatch.setattr(os, "access", lambda *a, **k: True)  # 模拟 Windows 恒真
    # 仓库根取 tmp_path 之外的假路径，候选目录本身都在「仓库外」
    assert _pick_tempdir([str(bad), str(good)], repo=str(tmp_path / "repo")) == os.path.abspath(good)


def test_pick_never_returns_repo_candidate(tmp_path) -> None:
    """仓库内候选一律不选（落盘红线）；跳过仓库内后可继续选后续候选。"""
    repo = tmp_path / "repo"
    inside = repo / "backend"
    outside = tmp_path / "outside"
    inside.mkdir(parents=True)
    outside.mkdir()
    assert _pick_tempdir([str(inside)], repo=str(repo)) is None
    assert _pick_tempdir([str(inside), str(outside)], repo=str(repo)) == os.path.abspath(outside)


def test_pin_honors_explicit_tmpdir(monkeypatch, tmp_path) -> None:
    """gettempdir 兜底到仓库内时，显式 TMPDIR 优先成为 tempfile 根。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    explicit = tmp_path / "explicit_tmp"
    explicit.mkdir()
    monkeypatch.setenv("TMPDIR", str(explicit))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(repo))
    monkeypatch.setattr(tempfile, "tempdir", str(repo))
    _pin_tempdir_outside_repo(repo=str(repo))
    assert os.path.abspath(tempfile.tempdir) == os.path.abspath(explicit)
