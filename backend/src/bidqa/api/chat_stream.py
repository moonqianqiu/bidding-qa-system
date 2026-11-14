"""POST /api/chat/stream（docs/01 §3.3/§3.4 B2；M2-5/M2-12）：Agent 问答 SSE 主端点。

事件协议：每行 data: JSON，seq 单调递增；ping 15s 心跳；契约序 status*→thinking*→token*→done|error，
reset 后必现 status。幂等 4091（client_msg_id 60s 窗口）；限流 429 带 Retry-After；
Agent 基建失败降级直答（C6 降级路径，段名 retrieve_ms/degrade_ms 两套不合并）。

进程内状态（幂等窗口/限流/连接）假定单进程（docs/01 §6，workers=1 硬约束）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from queue import Empty, Queue
from typing import Any, Iterator, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("bidqa.api.chat_stream")

from bidqa.agent.assembly import build_done
from bidqa.agent.loop import AgentLoop, LoopResult
from bidqa.api.ratelimit import rate_identity
from bidqa.auth_core import ROLE_GUEST
from bidqa.clients.llm import ChatClient
from bidqa.clients.siliconflow import DataSourceError
from bidqa.config import get_settings
from bidqa.errors import EC_CONFLICT, EC_RATE_LIMITED, EC_SOURCE_DEGRADED, error_body
from bidqa.rag.retrieve import RetrievalResult
from bidqa.service import (
    build_chat, build_chat_for, build_neo, build_pg, build_reranker, build_retriever,
    build_web_clients,
)
from bidqa.clients.tavily import web_cache
from bidqa.tools.base import ToolRegistry
from bidqa.tools.db_tool import SearchDatabaseTool
from bidqa.tools.graph_tool import SearchGraphTool
from bidqa.tools.knowledge import SearchKnowledgeTool
from bidqa.tools.runner import ToolRunner
from bidqa.tools.web import SearchWebTool

router = APIRouter()

PING_INTERVAL = 15.0
IDEMPOTENCY_WINDOW = 60.0
HISTORY_TURNS = 6

_client_msg_seen: dict[str, float] = {}  # client_msg_id -> monotonic ts（单进程）


class _GuestHistory:
    """游客会话的进程内历史（docs/01 §3.1 游客语义：不落库，多轮上下文仍可用）。

    TTL 1h、封顶 HISTORY_TURNS 轮、条目数有界、重启即失——与「关页即失」一致的
    诚实内存态（workers=1 允许进程内状态）。admin/user 会话不走这里（落 PG）。
    """

    TTL_SECONDS = 3600.0
    MAX_CONVERSATIONS = 256

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, list[dict]]] = {}
        self._lock = threading.Lock()

    def load(self, conv_id: str | None) -> list[dict]:
        if not conv_id:
            return []
        with self._lock:
            entry = self._data.get(conv_id)
            if entry is None:
                return []
            ts, items = entry
            if time.monotonic() - ts > self.TTL_SECONDS:
                self._data.pop(conv_id, None)
                return []
            return list(items)

    def append(self, conv_id: str | None, question: str, answer: str) -> None:
        if not conv_id:
            return
        with self._lock:
            now = time.monotonic()
            ts, items = self._data.get(conv_id, (now, []))
            items = (items + [{"role": "user", "content": question},
                              {"role": "assistant", "content": answer}])[-HISTORY_TURNS:]
            self._data[conv_id] = (now, items)
            if len(self._data) > self.MAX_CONVERSATIONS:  # 有界防泄漏：丢最旧一半
                for key in sorted(self._data, key=lambda k: self._data[k][0])[
                        :self.MAX_CONVERSATIONS // 2]:
                    self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


guest_history = _GuestHistory()


class ChatStreamRequest(BaseModel):
    conversation_id: str | None = None
    message: str = Field(min_length=1, max_length=4000)
    web_enabled: bool = False
    thinking_enabled: bool = False
    image_refs: list[str] = Field(default_factory=list, max_length=4)
    provider: str | None = Field(default=None, max_length=64)
    client_msg_id: str | None = Field(default=None, max_length=64)
    lang: Literal["zh", "en"] = "zh"  # 界面语言 → 回答语言（P1；旧 payload 缺省 zh 不受影响）


_shared_runner: ToolRunner | None = None


def _runner() -> ToolRunner:
    global _shared_runner
    if _shared_runner is None:
        _shared_runner = ToolRunner(ToolRegistry())  # 工具在执行期按注册表查找
    return _shared_runner


def _sse_line(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


def _load_history(pg: Any, conversation_id: str | None) -> tuple[str | None, list[dict], str]:
    """加载会话（最近 N 轮 + 滚动摘要）；无 PG/无会话返回 (None, [], "")。"""
    if not pg or not conversation_id:
        return conversation_id, [], ""
    try:
        row = pg.get_conversation(conversation_id)
    except Exception:
        return conversation_id, [], ""
    if not row:
        return conversation_id, [], ""
    messages = row.get("messages") or {}
    items = [m for m in messages.get("items", [])
             if m.get("role") in ("user", "assistant")][-HISTORY_TURNS:]
    return conversation_id, items, str(messages.get("summary", "") or "")


def _persist(pg: Any, conversation_id: str | None, question: str, result: LoopResult,
             provider: str, prior_summary: str, user_id: str | None = None) -> str:
    """会话落库（messages JSONB 内含 schema_v）；失败静默（能力型）。
    user_id=归属（docs/01 §3.1 会话隔离）：机器主 token/legacy 为 None（机器域）。"""
    if not pg:
        return conversation_id or ""
    conv_id = conversation_id or str(uuid.uuid4())
    try:
        row = pg.get_conversation(conv_id)
        items = (row.get("messages") or {}).get("items", []) if row else []
        new_items = items + [{"role": "user", "content": question},
                             {"role": "assistant", "content": result.answer}]
        title = (row or {}).get("title") or question[:30]
        pg.upsert_conversation(conv_id, title,
                               {"schema_v": 1, "items": new_items[-200:],
                                "summary": prior_summary}, provider, user_id=user_id)
        result.conversation_id = conv_id
    except Exception as exc:
        # 落库失败不再纯静默：类型+简述进日志（脱敏原则：只记结构性错误信息）
        logger.warning("会话落库失败 conv=%s: %s: %s", conv_id[:8], type(exc).__name__, str(exc)[:160])
    return conv_id


def _compress_after_done(pg: Any, conv_id: str, chat: Any, question: str,
                         result: LoopResult, prior_summary: str,
                         history_items: list[dict]) -> None:
    """C7 压缩：在 _result 投递之后执行（5s 限时），不拖慢收尾事件；
    失败=等同不开启（契约）。"""
    if pg is None or chat is None:
        return
    try:
        from bidqa.agent.compress import compress_history
        summary = compress_history(
            chat, history_items + [{"role": "user", "content": question},
                                    {"role": "assistant", "content": result.answer}],
            prior_summary=prior_summary, conversation_id=conv_id)
        if summary:
            row2 = pg.get_conversation(conv_id)
            items2 = (row2.get("messages") or {}).get("items", [])
            title = (row2 or {}).get("title") or question[:30]
            pg.upsert_conversation(conv_id, title,
                                   {"schema_v": 1, "items": items2, "summary": summary},
                                   "")
    except Exception:
        pass


def _image_context_block(refs: list[str]) -> str:
    """image_refs → 【图片内容】上下文块（§3.3：描述由 /api/vision 预解析缓存）。

    过期/未知 ref 静默跳过——图片上下文是尽力而为的补充，TTL 过了就没了，
    不值得为它让整轮问答失败。
    """
    if not refs:
        return ""
    from bidqa.api.vision import get_ref_store
    store = get_ref_store()
    descs = [d for d in (store.get(ref) for ref in refs) if d]
    if not descs:
        return ""
    return "\n".join(f"【图片内容 {i}】{d}" for i, d in enumerate(descs, 1))


def _worker(payload: ChatStreamRequest, queue: Queue, trace_id: str, t0: float,
            settings: Any, identity: Any = None) -> None:
    """工作线程：Agent 循环 → _result；异常走降级直答 → 仍败 → _error。

    identity（docs/01 §3.1）：guest=不落库、历史走进程内缓存；admin/user=落库带归属；
    None（legacy 开发模式）=落库进机器域（NULL user_id），与旧行为一致。
    """
    emit = lambda kind, p: queue.put((kind, p))  # noqa: E731
    is_guest = bool(identity and identity.role == ROLE_GUEST)
    try:
        chat: ChatClient | None = build_chat_for(payload.provider)  # C8/A12 profile 路由
        retriever = build_retriever()
        pg = build_pg()
        neo = build_neo()
        web_clients = build_web_clients()
        registry = ToolRegistry()
        if retriever is not None:
            registry.register(SearchKnowledgeTool(retriever, chat))
        if pg is not None:
            registry.register(SearchDatabaseTool(pg))
        if neo is not None:
            registry.register(SearchGraphTool(neo))
        if web_clients:
            registry.register(SearchWebTool(web_clients, web_cache(),
                                            reranker=build_reranker()))
        if chat is None or not registry.names():
            raise DataSourceError("chat", "核心槽位/检索不可用")
        if is_guest:
            conv_id = payload.conversation_id
            history = guest_history.load(conv_id)
            prior_summary = ""
        else:
            conv_id, history, prior_summary = _load_history(pg, payload.conversation_id)
        loop = AgentLoop(chat, registry, _runner(), temperature=0.3)
        # 图片上下文只进模型、不落库（描述是 TTL 10min 的易逝旁路；会话存原始问题）
        question = payload.message
        image_block = _image_context_block(payload.image_refs)
        if image_block:
            question = f"{question}\n\n{image_block}"
        result = loop.run(
            question, history=history,
            summary_text=prior_summary or None,
            web_enabled=payload.web_enabled,
            thinking=payload.thinking_enabled,
            thinking_model=settings.CHAT_THINKING_MODEL,
            first_round=not bool(history),  # 有历史=非首轮（澄清仅首轮，C2）
            on_event=emit,
            lang=payload.lang,
        )
        if is_guest:
            guest_history.append(conv_id, payload.message, result.answer)
            result.conversation_id = conv_id
        else:
            saved_id = _persist(pg, conv_id or payload.conversation_id, payload.message,
                                result, payload.provider or "", prior_summary,
                                user_id=(identity.user_id if identity else None))
        queue.put(("_result", result))  # 收尾事件优先
        if not is_guest:
            _compress_after_done(pg, saved_id, chat, payload.message, result,
                                 prior_summary, history)  # C7：5s 限时，不阻塞收尾
    except Exception as exc:
        try:
            result = _direct_rag(payload, emit, str(exc))
            result.conversation_id = payload.conversation_id
            queue.put(("_result", result))
        except Exception:
            queue.put(("_error", {"error_code": EC_SOURCE_DEGRADED,
                                  "message": f"数据源降级/不可用: {type(exc).__name__}: {exc}"}))


def _direct_rag(payload: ChatStreamRequest, emit: Any, reason: str) -> LoopResult:
    """降级直答（C6）：无工具 RAG 直连；段名与主路径两套不合并。"""
    emit("status", {"stage": "降级直答"})
    t0 = time.perf_counter()
    retriever = build_retriever()
    chat = build_chat()
    if retriever is None or chat is None:
        raise DataSourceError("chat", "降级路径不可用")
    result = retriever.retrieve(payload.message)
    retrieve_ms = int((time.perf_counter() - t0) * 1000)
    blocks = "\n\n".join(f"[{i + 1}] ({h.source_type}) {h.title}\n{h.text}"
                         for i, h in enumerate(result.hits))
    image_block = _image_context_block(payload.image_refs)  # 降级路径同样带图片上下文
    answer = ("The knowledge base does not cover this content." if payload.lang == "en"
              else "知识库未包含相关内容。")
    if result.hits:
        user_content = f"【检索材料】\n{blocks}\n\n【问题】{payload.message}"
        if image_block:
            user_content = f"{user_content}\n\n{image_block}"
        system_prompt = ("你是招投标领域问答助手。只依据给定【检索材料】作答；"
                         "未覆盖回答「知识库未包含」。引用标注 [n]。")
        if payload.lang == "en":
            system_prompt += " 回答正文必须使用英文（English）；引用标注 [n] 保留。"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        parts: list[str] = []
        try:
            for chunk in chat.chat_stream(messages):
                parts.append(chunk)
                emit("token", {"text": chunk})  # 降级路径同样真流式
            answer = "".join(parts) or answer
        except Exception:
            answer = chat.chat(messages)
            for i in range(0, len(answer), 80):
                emit("token", {"text": answer[i:i + 80]})
    else:
        emit("token", {"text": answer})
    out = LoopResult(answer=answer, rounds=0,
                     sources=[h.as_source() for h in result.hits])
    out.phase_times = {"retrieve_ms": retrieve_ms,
                       "degrade_ms": int((time.perf_counter() - t0) * 1000) - retrieve_ms}
    out.degraded.append(f"agent_fallback: {reason[:120]}")
    return out


@router.post("/api/chat/stream")
def chat_stream(payload: ChatStreamRequest, request: Request,
                settings: Any = Depends(get_settings)) -> Any:
    """SSE 主端点。settings 经 DI 注入（应用工厂 override 可测试）。"""
    from bidqa.api.system import note_request
    note_request("/api/chat/stream", 200)  # H2 行为计数（限流/4091 提前返回不计入）
    # 429 限流（B4；被限流请求不进入任何评测埋点；游客走独立更严配额桶）
    limiter, bucket = rate_identity(request, settings)
    allowed, retry_after = limiter.check(bucket)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content=error_body(EC_RATE_LIMITED, "请求过于频繁，请稍后再试",
                               detail={"retry_after": retry_after}),
            headers={"Retry-After": str(retry_after)},
        )
    # 4091 幂等（client_msg_id 60s 去重窗口；重试必须换新键）
    now = time.monotonic()
    if payload.client_msg_id:
        seen_at = _client_msg_seen.get(payload.client_msg_id)
        if seen_at is not None and now - seen_at < IDEMPOTENCY_WINDOW:
            return JSONResponse(
                status_code=409,
                content=error_body(EC_CONFLICT, "重复提交（60s 内相同 client_msg_id）",
                                   detail={"client_msg_id": payload.client_msg_id}),
            )
        _client_msg_seen[payload.client_msg_id] = now
        if len(_client_msg_seen) > 512:  # 有界防泄漏
            for key in list(_client_msg_seen)[:256]:
                _client_msg_seen.pop(key, None)

    # 前置可用性检查：主模型缺失 → HTTP 5031（流未开始，错误体语义明确）
    if build_chat() is None:
        return JSONResponse(
            status_code=503,
            content=error_body(EC_SOURCE_DEGRADED, "数据源降级/不可用",
                               detail={"source": "chat: 核心槽位未配置"}),
        )

    queue: Queue = Queue()
    trace_id = uuid.uuid4().hex[:12]
    t0 = time.perf_counter()
    identity = getattr(request.state, "identity", None)
    threading.Thread(target=_worker,
                     args=(payload, queue, trace_id, t0, settings, identity),
                     daemon=True, name="sse-worker").start()

    def generate() -> Iterator[str]:
        seq = 0

        def next_seq() -> int:
            nonlocal seq
            seq += 1
            return seq

        while True:
            try:
                kind, body = queue.get(timeout=PING_INTERVAL)
            except Empty:  # ping 心跳（docs/01 §3.4，防反代空闲断连）
                yield _sse_line({"type": "ping", "seq": next_seq()})
                continue
            if kind == "_result":
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                done = build_done(body, elapsed_ms=elapsed_ms, trace_id=trace_id,
                                  provider=payload.provider or "")
                _online_record(payload, body, trace_id, elapsed_ms)  # G4 行为埋点（旁路）
                yield _sse_line({"type": "done", "seq": next_seq(), **done})
                return
            if kind == "_error":
                yield _sse_line({"type": "error", "seq": next_seq(), **body})
                return
            yield _sse_line({"type": kind, "seq": next_seq(), **body})

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _online_record(payload: ChatStreamRequest, result: Any, trace_id: str,
                   elapsed_ms: int) -> None:
    """G4 在线评测埋点：行为全量免费；采样率>0 时抽中轮附带正文判分（旁路绝不抛错）。"""
    try:
        from bidqa.eval.online import get_online, pick_sample
        settings = get_settings()
        sample = None
        if pick_sample(settings.ONLINE_EVAL_SAMPLE_RATE):
            sample = {"question": payload.message, "answer": result.answer,
                      "sources": list(result.sources or []), "trace_id": trace_id}
        get_online().record_done(
            provider=payload.provider or "", web_enabled=payload.web_enabled,
            thinking_enabled=payload.thinking_enabled, rounds=int(result.rounds or 0),
            tool_name=str(result.first_tool or ""), hit_cap=bool(result.hit_cap),
            clarify=bool(result.clarify), degraded=bool(result.degraded),
            elapsed_ms=elapsed_ms, sample=sample)
    except Exception:
        pass  # 评测旁路失败不影响问答（docs/01：失败=等同不开启）


def parse_sse_lines(text: str) -> list[dict]:
    """测试辅助：SSE 文本 → 事件列表。"""
    events = []
    for line in text.splitlines():
        if line.startswith("data:"):
            events.append(json.loads(line[5:].strip()))
    return events
