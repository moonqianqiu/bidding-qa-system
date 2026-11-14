"""标的物批量提取管道（F6 数据修复）：PG 项目名 → 免费档 GLM-4.7-Flash → 写回 PG。

配方逐字对齐参考项目实证（参考资料-…/04_数据与知识库/批处理标的物.ipynb 与
batch_requests/results_10.jsonl 样例：单条 prompt ≈84 token、completion ≈15 token，
7,949 去重名全程 ≈79 万 token）。国际版 z.ai 无 Batch API（openapi.json 核实），
故走**同步并发**：requests.jsonl 仍是权威名单，run 阶段多线程直调 chat/completions、
结果按智谱 batch 结果行格式增量落盘（parse_results 零改动复用），按 custom_id 断点续跑。
统一试运行约定：fetch 默认只解析落盘，--write 才写库。提示词勿随意改动——
提取口径（只输出商品/服务名、优先服务、不含公司名）直接决定图谱/图表维度质量。
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable

# ── 提示词 v2（2026-11-08 实证）：few-shot 锚定 + reasoning_effort=low ──
# 背景：GLM-5.3-Flash 的思考流关不掉标准开关，reasoning_effort=low 实测可关（SF 文档两次失实）；
# 无思考在难名字上掉质量（1/4），加 3 条 few-shot 后回升（2/4，且失败多为轻度近似）。
# 思考版同样 1/4 且错得更狠——同价位 few-shot 无思考是最优解，还省 90% 输出 token。
SYSTEM_PROMPT = "你是一个标的物提取器。"
FEWSHOT = '''

示例（严格模仿输出粒度）：
项目名称：合肥监管集中区干警备勤区警训用房配套工程项目（楼宇智能设备采购安装）→ 输出：{ "标的物": "楼宇智能设备" }
项目名称：合肥国风先进基础材料科技有限公司新型柔性电子用聚酰亚胺膜材料生产车间三（5-7#线）公用设备采购 → 输出：{ "标的物": "新型柔性电子用聚酰亚胺膜材料生产车间三公用设备" }
项目名称：2023年长丰县义井镇杜岗行政村西街中心村省级和美乡村中心村建设公共服务设施建设项目 → 输出：{ "标的物": "和美乡村中心村建设公共服务设施" }'''
SYSTEM_PROMPT_FULL = SYSTEM_PROMPT + FEWSHOT
USER_TMPL = (
    "# 任务：请从以下项目名称中，提取一个字段：核心标的物（只输出具体的商品或服务名称，"
    "优先输出服务名称，不要包含公司名），# 项目名称：project_name = {name}。"
    "# 输出格式：{{ \"标的物\": \" \"}}"
)
_SM_RE = re.compile(r'"标的物"\s*[:：]\s*"([^"]{1,60})"')
# 裸文本兜底的拒答哨兵：模型表示提不出来时绝不把话术本身当成标的物
_BARE_REJECT = re.compile(r"无法|抱歉|不能|不出|失败|缺少|请提供")


def default_dir() -> Path:
    """批次工作目录（请求/状态/结果都落 data/run/subject_batch，gitignore）。"""
    return Path(__file__).resolve().parents[4] / "data" / "run" / "subject_batch"


def extract_subject(content: str) -> str | None:
    """从模型输出解析标的物（容忍中文冒号/尾缀标点/裸文本兜底）；解析不出 → None。

    裸文本兜底（2026-11-08 实证 1/7,949 触发）：模型偶尔不套 JSON 壳直接给答案——
    仅当正文短（≤30 字）且无格式残留时才收，防把长解释误当标的物。
    """
    if not content:
        return None
    m = _SM_RE.search(content)
    if m:
        value = m.group(1).strip().strip("。．.、；;，,\"\"“”")
        return value or None
    bare = content.strip().strip("`").strip()
    if (bare and len(bare) <= 30 and "{" not in bare and "}" not in bare
            and "`" not in bare and "\n" not in bare and not _BARE_REJECT.search(bare)):
        return bare.strip("。．.、；;，, ")
    return None


def build_requests(names: Iterable[str], model: str) -> list[dict[str, Any]]:
    """去重排序名单 → 批量请求行（custom_id=request-N，同名同标的物，稳定可复现）。"""
    lines: list[dict[str, Any]] = []
    for i, name in enumerate(sorted({n.strip() for n in names if n and n.strip()}), start=1):
        lines.append({
            "custom_id": f"request-{i}",
            "method": "POST",
            "url": "/v4/chat/completions",
            "body": {
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT_FULL},
                    {"role": "user", "content": USER_TMPL.format(name=name)},
                ],
                "temperature": 0.0,
            },
        })
    return lines


def write_requests(path: Path, requests: list[dict[str, Any]]) -> None:
    """请求 JSONL 落盘（UTF-8 保真）+ 名单映射 names.json（custom_id → 项目名）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in requests) + "\n",
        encoding="utf-8")
    id2name = {}
    prefix, suffix = "project_name = ", "。# 输出格式"
    for r in requests:
        user = r["body"]["messages"][1]["content"]
        start = user.index(prefix) + len(prefix)
        id2name[r["custom_id"]] = user[start:user.index(suffix, start)]
    path.with_name("names.json").write_text(
        json.dumps(id2name, ensure_ascii=False, indent=0), encoding="utf-8")


def load_id2name(path: Path) -> dict[str, str]:
    """读取 build 阶段留下的 custom_id → 项目名映射。"""
    return dict(json.loads(path.with_name("names.json").read_text(encoding="utf-8")))


def parse_results(raw_lines: Iterable[str],
                  id2name: dict[str, str]) -> tuple[dict[str, str], list[dict[str, str]]]:
    """结果 JSONL → ({项目名: 标的物}, 失败明细)。坏行/非 200/解析不出逐条记录，不中断。"""
    mapping: dict[str, str] = {}
    failures: list[dict[str, str]] = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            failures.append({"custom_id": "?", "reason": "bad_json"})
            continue
        cid = str(obj.get("custom_id") or "?")
        name = id2name.get(cid)
        if name is None:
            failures.append({"custom_id": cid, "reason": "unknown_custom_id"})
            continue
        resp = obj.get("response") or {}
        if int(resp.get("status_code") or 0) != 200:
            failures.append({"custom_id": cid, "reason": f"http_{resp.get('status_code')}"})
            continue
        choices = (resp.get("body") or {}).get("choices") or []
        content = (choices[0].get("message") or {}).get("content") if choices else ""
        sm = extract_subject(content or "")
        if sm is None:
            failures.append({"custom_id": cid, "reason": "unparseable_content"})
        else:
            mapping[name] = sm
    return mapping, failures


# ── 同步并发执行（国际版 z.ai 无 Batch API 的替代路径）──

def load_done_ids(results_path: Path) -> set[str]:
    """**成功**结果的 custom_id 集合（断点续跑依据；坏行忽略）。

    只有 status_code=200 才算 done——失败行不算，重跑 run 会自动重试它们
    （2026-11-08 修正：曾把失败行也当已完成，限流冷却后重跑会被旧失败行挡住）。
    """
    if not results_path.exists():
        return set()
    done: set[str] = set()
    for line in results_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = obj.get("response") or {}
        try:
            status = int(resp.get("status_code") or 0)
        except (TypeError, ValueError):
            status = 0
        if status == 200:
            if cid := str(obj.get("custom_id") or ""):
                done.add(cid)
    return done


def _result_line(cid: str, status_code: int, body: dict | None) -> str:
    """与智谱 batch 结果行同构（parse_results 复用）；body 原样内嵌。"""
    return json.dumps({"response": {"status_code": status_code, "body": body or {}},
                       "custom_id": cid}, ensure_ascii=False)


def run_sync(requests: list[dict[str, Any]], chat_fn: Callable[[list[dict], float], str],
             results_path: Path, workers: int = 2, progress_every: int = 200,
             delay_seconds: float = 0.5,
             retry_delays: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0, 32.0, 60.0)) -> dict[str, int]:
    """多线程执行提取并**增量落盘**结果行（append 模式 = 断点续跑安全）。

    chat_fn(messages, temperature) → 正文。限速三件套：
    - delay_seconds：全局调用起点间隔（pace 锁，防瞬时并发触发限流——排队池做不到这点）；
    - retry_delays：失败指数退避重试（2026-11-08 用户批准的红线豁免，**仅此离线批量**；
      每次尝试都打日志、retries 计数透明；服务路径的 429 红线不受影响）；
      z.ai 免费档实测 1305「临时过载」，官方建议即稍后重试；
    - 失败重试耗尽 → 记 http_0 行（重跑 run 跳过已落盘的，要重试失败项删对应行）。
    返回 {total, done_before, ok, failed, retries}。
    """
    results_path.parent.mkdir(parents=True, exist_ok=True)
    done_before = load_done_ids(results_path)
    pending = [r for r in requests if r["custom_id"] not in done_before]
    write_lock = threading.Lock()
    pace_lock = threading.Lock()
    next_start = [0.0]
    counters = {"ok": 0, "failed": 0, "retries": 0}
    processed = 0

    def one(req: dict[str, Any]) -> tuple[str, bool]:
        body = req["body"]
        with pace_lock:  # 全局起点间隔：实际调用开始时间互隔 delay_seconds
            wait = next_start[0] - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            next_start[0] = time.monotonic() + delay_seconds
        last_exc: Exception | None = None
        for attempt, backoff in enumerate((0.0, *retry_delays)):
            if backoff:
                time.sleep(backoff)
            try:
                content = str(chat_fn(body["messages"], float(body.get("temperature", 0.0))))
                return _result_line(req["custom_id"], 200, {"choices": [
                    {"message": {"role": "assistant", "content": content}}]}), True
            except Exception as exc:  # 单条失败不拖垮整批（runner 同款隔离约定）
                last_exc = exc
                if attempt < len(retry_delays):
                    counters["retries"] += 1
                    print(f"  [retry] {req['custom_id']} 第{attempt + 1}次失败 "
                          f"{type(exc).__name__}: {str(exc)[:80]} → {retry_delays[attempt]}s 后重试",
                          flush=True)
        return _result_line(req["custom_id"], 0,
                            {"error": f"{type(last_exc).__name__}: {str(last_exc)[:150]}"}), False

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="subject-batch") as pool:
        futures = [pool.submit(one, req) for req in pending]
        with results_path.open("a", encoding="utf-8") as fh:
            for fut in as_completed(futures):
                line, ok = fut.result()
                counters["ok" if ok else "failed"] += 1
                with write_lock:
                    fh.write(line + "\n")
                    fh.flush()  # 每条即刷：长跑中崩进程不丢已完成结果（断点续跑的耐久性根基）
                processed += 1
                if progress_every and processed % progress_every == 0:
                    print(f"  进度 {processed}/{len(pending)}"
                          f"（成功 {counters['ok']}，失败 {counters['failed']}，"
                          f"重试 {counters['retries']}）", flush=True)
    return {"total": len(requests), "done_before": len(done_before),
            "ok": counters["ok"], "failed": counters["failed"], "retries": counters["retries"]}


def pending_project_names(repo) -> list[str]:
    """待提取项目名（subject_matter 为空的去重名；F6 build 阶段输入，SQL 下沉至此）。"""
    rows = repo.connection().execute(
        "SELECT DISTINCT project_name FROM bidding_procurement "
        "WHERE subject_matter IS NULL AND length(btrim(project_name)) > 0 ORDER BY 1"
    ).fetchall()
    return [r["project_name"] for r in rows]
