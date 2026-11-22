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


def test_consumers_read_legacy_and_golden_segment_names(tmp_path, monkeypatch) -> None:
    """NIGHT-036：命名规则加 golden 段后，旧四段名快照保持可读——按 glob 读
    快照的消费方（列表 API / 滚动保留）不得因命名规则变化读到旧文件报错。"""
    import json

    from bidqa.api import eval_runs
    from bidqa.eval.ragas import rolling_retention

    legacy = tmp_path / "rag_20261122_da2329d_deepseek-flash.json"  # 旧四段名
    legacy.write_text(json.dumps({"kind": "rag", "summary": {"total": 5, "avg_score": 0.9}}),
                      encoding="utf-8")
    with_golden = tmp_path / "rag_20261122_rag_law_v1_da2329d_deepseek-flash.json"  # 新五段名
    with_golden.write_text(json.dumps({"kind": "rag", "summary": {"total": 7, "avg_score": 0.8}}),
                           encoding="utf-8")

    # 列表 API：glob *.json，新旧名同列、内容各自读出
    monkeypatch.setattr(eval_runs, "_results_dir", lambda: tmp_path)
    runs = {r["name"]: r for r in eval_runs.list_runs(settings=None)["runs"]}
    assert runs[legacy.name]["score"] == 0.9
    assert runs[with_golden.name]["score"] == 0.8

    # 滚动保留：glob *.json 按 mtime，不依赖文件名段数
    monkeypatch.setattr("bidqa.eval.ragas.RESULTS_DIR", tmp_path)
    assert rolling_retention(keep=2) == 0
    assert legacy.is_file() and with_golden.is_file()


def test_golden_slug_normalizes_unsafe_names() -> None:
    """NIGHT-036：golden 名段归一——仅折文件系统/命令行不安全字符为 _、大小写
    归一（Windows 不区分大小写，原样入名会重新引入同日互踩）、截 40 字符。"""
    from bidqa.eval.agent_eval import golden_slug

    assert golden_slug("eval/golden/agent_v0.jsonl") == "agent_v0"
    assert golden_slug("Rag Law V1.JSONL") == "rag_law_v1"
    assert golden_slug("my golden (v2).jsonl") == "my_golden_(v2)"  # 空白折 _，括号保留
    assert golden_slug("最终金集.jsonl") == "最终金集"  # 非 ASCII 金集名原样保留
    assert golden_slug(None) == "" and golden_slug("") == ""
    assert golden_slug("...") == ""  # 折完为空 → 退回旧四段名
    assert len(golden_slug("x" * 100)) == 40


def test_snapshot_path_scope_segment(tmp_path, monkeypatch) -> None:
    """NIGHT-046：scope 段（retrieval 集合+主通道）插在 golden 段之后——同日同
    金集跨集合/通道不互踩；scope 不可得省略段（旧命名）；agent 不传 scope 命名
    不变（回归锚）；段归一走 slug_segment 同款规则。"""
    import datetime

    from bidqa.eval.agent_eval import snapshot_path

    monkeypatch.setattr("bidqa.eval.agent_eval.git_short_hash", lambda: "abc1234")
    date_str = datetime.date.today().strftime("%Y%m%d")
    p = snapshot_path("retrieval", "nojudge", golden="rag_v0.jsonl",
                      scope="bidqa_knowledge_v2-hybrid")
    assert p.name == f"retrieval_{date_str}_rag_v0_bidqa_knowledge_v2-hybrid_abc1234_nojudge.json"
    # scope 不可得 → 无 scope 段（退回旧命名）
    assert snapshot_path("retrieval", "nojudge", golden="rag_v0.jsonl",
                         scope=None).name == f"retrieval_{date_str}_rag_v0_abc1234_nojudge.json"
    assert snapshot_path("retrieval", "nojudge", golden="rag_v0.jsonl",
                         scope="").name == f"retrieval_{date_str}_rag_v0_abc1234_nojudge.json"
    # scope 不安全字符归一（空白折 _、小写），golden 缺省时 scope 仍在 git 段前
    assert snapshot_path("retrieval", "nojudge", golden=None,
                         scope="Bidqa V2 (tmp)").name == \
        f"retrieval_{date_str}_bidqa_v2_(tmp)_abc1234_nojudge.json"
    # agent/rag 不传 scope → 命名与 NIGHT-036 逐字一致（既有字段不受破坏）
    assert snapshot_path("agent", "nojudge", golden="agent_v0.jsonl").name == \
        f"agent_{date_str}_agent_v0_abc1234_nojudge.json"
