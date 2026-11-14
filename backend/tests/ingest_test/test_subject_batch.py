"""标的物批量提取单元（build/parse/run_sync 全打桩，不触网）。

run_sync 用 fake chat_fn（异常向上抛由执行器捕获记失败行）；结果行与智谱 batch
格式同构（parse_results 零改动复用）；断点续跑靠已落盘 custom_id 跳过。
提示词口径与参考项目实证对齐，格式回归防字段漂移。
"""

from __future__ import annotations

import json

from bidqa.ingest.subject_batch import (
    build_requests, extract_subject, load_done_ids, load_id2name, parse_results,
    run_sync, write_requests)


def _result_line(cid: str, content: str, status: int = 200) -> str:
    return json.dumps({
        "response": {"status_code": status, "body": {"choices": [
            {"message": {"role": "assistant", "content": content}}]}},
        "custom_id": cid,
    }, ensure_ascii=False)


# ── build：去重/稳定 custom_id/请求行格式 ──

def test_build_requests_dedup_and_format() -> None:
    reqs = build_requests(["B 项目", "A 项目", "B 项目", ""], "glm-4.7-flash")
    assert [r["custom_id"] for r in reqs] == ["request-1", "request-2"]  # 去重+排序稳定
    for r in reqs:
        assert r["method"] == "POST" and r["url"] == "/v4/chat/completions"
        assert r["body"]["model"] == "glm-4.7-flash" and r["body"]["temperature"] == 0.0
        assert r["body"]["messages"][0]["role"] == "system"
        user = r["body"]["messages"][1]["content"]
        assert user.startswith("# 任务：请从以下项目名称中")
        assert user.endswith("# 输出格式：{ \"标的物\": \" \"}")


def test_build_requests_name_embedded() -> None:
    reqs = build_requests(["空调采购项目"], "glm-4.7-flash")
    assert "project_name = 空调采购项目。" in reqs[0]["body"]["messages"][1]["content"]


# ── 解析：五类结果 ──

def test_extract_subject_variants() -> None:
    assert extract_subject('{ "标的物": "超声波流量计检测服务" }') == "超声波流量计检测服务"
    assert extract_subject('{"标的物"："空调"}') == "空调"  # 中文冒号
    assert extract_subject('{"标的物": "物流服务。"}') == "物流服务"  # 尾缀标点
    assert extract_subject("") is None
    assert extract_subject("抱歉无法提取") is None
    assert extract_subject('{"标的物": ""}') is None


def test_extract_subject_bare_fallback() -> None:
    assert extract_subject("抽屉锁") == "抽屉锁"  # 模型偶尔不套 JSON 壳（1/7,949 实证）
    assert extract_subject('```\n抽屉锁\n```') == "抽屉锁"  # 围栏剥掉
    assert extract_subject("这是一段超过三十个字符的长解释文本，不该被当成标的物收进来" * 2) is None
    assert extract_subject('{"answer": "x"}') is None  # 含花括号的非标的物结构不收


def test_parse_results_success_and_failures() -> None:
    id2name = {"request-1": "A 项目", "request-2": "B 项目", "request-3": "C 项目"}
    lines = [
        _result_line("request-1", '{ "标的物": "空调" }'),
        "not-json{{{",                                                     # 坏行
        _result_line("request-2", "随便一段话", status=429),                 # 非 200
        _result_line("request-3", "解析不出的正文"),                          # 内容无标的物
        _result_line("request-9", '{ "标的物": "幽灵" }'),                   # 未知 custom_id
        "",                                                                # 空行跳过
    ]
    mapping, failures = parse_results(lines, id2name)
    assert mapping == {"A 项目": "空调"}
    reasons = [f["reason"] for f in failures]
    assert reasons == ["bad_json", "http_429", "unparseable_content", "unknown_custom_id"]


# ── 落盘/回读：requests.jsonl + names.json 往返 ──

def test_write_requests_and_reload(tmp_path) -> None:
    reqs = build_requests(["物业保洁服务采购", "电梯（三次）"], "glm-4.7-flash")
    path = tmp_path / "requests.jsonl"
    write_requests(path, reqs)
    assert path.read_text(encoding="utf-8").splitlines() == [
        json.dumps(r, ensure_ascii=False) for r in reqs]
    id2name = load_id2name(path)
    # sorted() 按码点：「物」U+7269 < 「电」U+7535
    assert id2name == {"request-1": "物业保洁服务采购", "request-2": "电梯（三次）"}


# ── run_sync：成功/失败隔离 + 断点续跑 ──

def test_run_sync_ok_and_failure_isolation(tmp_path) -> None:
    reqs = build_requests(["空调采购项目", "电梯采购项目"], "glm-4.7-flash")
    write_requests(tmp_path / "requests.jsonl", reqs)

    def chat_fn(messages, temperature):  # 电梯那条抛错 → 记失败行不拖垮整批
        if "电梯" in messages[1]["content"]:
            raise RuntimeError("boom")
        return '{ "标的物": "空调" }'

    # 显式短退避——默认退避序列真睡 122s，测试不能等它
    report = run_sync(reqs, chat_fn, tmp_path / "results_raw.jsonl", workers=2,
                      delay_seconds=0, retry_delays=(0.0, 0.0))
    assert report == {"total": 2, "done_before": 0, "ok": 1, "failed": 1, "retries": 2}
    id2name = load_id2name(tmp_path / "requests.jsonl")
    mapping, failures = parse_results(
        (tmp_path / "results_raw.jsonl").read_text(encoding="utf-8").splitlines(), id2name)
    assert mapping == {"空调采购项目": "空调"}
    # sorted() 按码点：「电」U+7535 < 「空」U+7A7A → 电梯是 request-1
    assert failures == [{"custom_id": "request-1", "reason": "http_0"}]


def test_run_sync_resume_skips_done(tmp_path) -> None:
    reqs = build_requests(["A 项目", "B 项目"], "glm-4.7-flash")
    path = tmp_path / "results_raw.jsonl"
    path.write_text(_result_line("request-1", '{ "标的物": "x" }') + "\n", encoding="utf-8")
    assert load_done_ids(path) == {"request-1"}

    seen: list[str] = []

    def chat_fn(messages, temperature):
        seen.append(messages[1]["content"])
        return '{ "标的物": "y" }'

    report = run_sync(reqs, chat_fn, path, workers=2, delay_seconds=0)
    assert report["done_before"] == 1 and report["total"] == 2 and report["ok"] == 1
    assert len(seen) == 1  # 只有未落盘的 request-2 被执行


def test_load_done_ids_only_counts_success(tmp_path) -> None:
    """失败行不算 done（2026-11-08 修正）：限流冷却后重跑自动重试旧失败项。"""
    path = tmp_path / "r.jsonl"
    path.write_text(
        _result_line("request-1", "x") + "\n" + _result_line("request-2", "y", status=0) + "\n",
        encoding="utf-8")
    assert load_done_ids(path) == {"request-1"}


# ── 退避重试（2026-11-08 用户批准的红线豁免，仅离线批量；每次尝试计数透明）──

def test_run_sync_retries_then_succeeds(tmp_path) -> None:
    reqs = build_requests(["A 项目"], "glm-4.7-flash")
    calls = {"n": 0}

    def chat_fn(messages, temperature):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("HTTP 429: overloaded")  # 前两次撞 429
        return '{ "标的物": "空调" }'

    report = run_sync(reqs, chat_fn, tmp_path / "r.jsonl", workers=1,
                      delay_seconds=0, retry_delays=(0.0, 0.0, 0.0))
    assert report["ok"] == 1 and report["failed"] == 0 and report["retries"] == 2
    assert calls["n"] == 3  # 第 3 次成功


def test_run_sync_retries_exhausted_records_failure(tmp_path) -> None:
    reqs = build_requests(["A 项目"], "glm-4.7-flash")

    def chat_fn(messages, temperature):
        raise RuntimeError("HTTP 429: overloaded")

    report = run_sync(reqs, chat_fn, tmp_path / "r.jsonl", workers=1,
                      delay_seconds=0, retry_delays=(0.0, 0.0))
    assert report == {"total": 1, "done_before": 0, "ok": 0, "failed": 1, "retries": 2}
    mapping, failures = parse_results(
        (tmp_path / "r.jsonl").read_text(encoding="utf-8").splitlines(), {"request-1": "A 项目"})
    assert mapping == {} and failures[0]["reason"] == "http_0"
