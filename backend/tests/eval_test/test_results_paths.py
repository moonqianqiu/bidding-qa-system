"""评测产物路径契约（审计 P1-20/P1-33）：绝对锚定 + 原子写卫生。"""

from __future__ import annotations

import os

import pytest


def test_results_dirs_are_absolute() -> None:
    """P1-20/P1-33：RESULTS_DIR/_results_dir 曾相对 CWD——从仓库根启动时快照
    静默落到另一个目录（/api/eval/runs 与 CLI 各看各的）。统一锚定 backend/。"""
    from bidqa.api import eval_runs
    from bidqa.eval import agent_eval, online

    assert agent_eval.RESULTS_DIR.is_absolute()
    assert agent_eval.RESULTS_DIR.name == "results"  # backend/eval/results
    assert online.RESULTS_DIR.is_absolute() and online.RESULTS_DIR.name == "online"
    assert eval_runs._results_dir().is_absolute()


def test_atomic_write_json_no_tmp_residue_on_failure(tmp_path, monkeypatch) -> None:
    """P2-17（审计）：固定 .tmp 名且无 finally——并发写同名互相覆盖 + 失败残留。"""
    import bidqa.eval.agent_eval as mod

    target = tmp_path / "snap.json"

    def boom(_a, _b):  # noqa: ANN001
        raise RuntimeError("replace 失败")

    monkeypatch.setattr(mod.os, "replace", boom)
    with pytest.raises(RuntimeError):
        mod.atomic_write_json(target, {"x": 1})
    assert list(tmp_path.glob("*.tmp")) == []  # finally 清理（原残留=红）
    assert not target.exists()


def test_atomic_write_json_tmp_name_is_pid_unique(tmp_path, monkeypatch) -> None:
    """P1-20：tmp 名带 pid——并发写同名快照不互相踩临时文件。"""
    seen: list[str] = []
    real_write = type(__import__("pathlib").Path()).write_text

    def spy_write(self, data, *a, **k):  # noqa: ANN001
        seen.append(self.name)
        return real_write(self, data, *a, **k)

    monkeypatch.setattr(type(__import__("pathlib").Path()), "write_text", spy_write)
    mod = __import__("bidqa.eval.agent_eval", fromlist=["atomic_write_json"])
    mod.atomic_write_json(tmp_path / "snap.json", {"x": 1})
    assert seen and f".{os.getpid()}.tmp" in seen[0]
