"""问答编排用例（docs/01 §3.3）：装配 → 历史 → Agent 循环 → 落库 → 压缩延后。

从 chat_stream._worker 归位（重构 C3）：本模块不 import FastAPI——入参是纯
ChatJob（api/chat/stream 负责把请求模型转成它），业务流程可脱离 HTTP 单测。
工具表组装走 agent/assembly.build_registry（唯一实现，与评测共用）。
主路径失败 → 降级直答（degrade）；再败 → ChatJobError（API 层转 error 事件）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import time
from typing import Any, Callable

from bidqa.agent.assembly import build_done, build_registry
from bidqa.agent.loop import AgentLoop, LoopResult
from bidqa.application.chat.degrade import direct_rag_answer
from bidqa.application.chat.history import (
    defer_compress, guest_history, load_history, persist_result,
)
from bidqa.application.image.refs import image_context_block
from bidqa.clients.chat_factory import build_chat_for
from bidqa.clients.siliconflow import DataSourceError
from bidqa.database.factory import build_bp_repo, build_conversation_store, build_neo
from bidqa.obs.tracing import span
from bidqa.rag.factory import build_retriever, build_reranker
from bidqa.tools.runner import ToolRunner
from bidqa.clients.web_factory import build_web_clients

EventFn = Callable[[str, dict[str, Any]], None]

logger = logging.getLogger("bidqa.application.chat.worker")


@dataclass
class ChatJob:
    """一次问答的纯数据入参（api 层由 ChatStreamRequest + identity 转换而来）。"""

    message: str
    conversation_id: str | None = None
    web_enabled: bool = False
    thinking_enabled: bool = False
    image_refs: list[str] = field(default_factory=list)
    provider: str | None = None
    lang: str = "zh"
    thinking_model: str = ""  # 由 api 层从注入的 Settings 取（application 不读配置）
    plan_enabled: bool = False     # WP5a Plan-and-Solve 开关（同上：api 层自 AGENT_PLAN_ENABLED 注入）
    reflect_enabled: bool = False  # WP5a Reflection 证据自检开关（同上：AGENT_REFLECT_ENABLED）
    is_guest: bool = False    # guest=不落库、历史走进程内缓存（docs/01 §3.1）
    user_id: str | None = None  # 落库归属；None=机器域（机器主 token/legacy）
    trace_id: str = ""        # 落库收尾结构携带（与 SSE done 同 id，便于对账）


@dataclass
class JobOutcome:
    """编排产出：result 先行投递收尾事件；compress 延后执行（C7 5s 限时）。"""

    result: LoopResult
    compress: Callable[[], None] | None = None


class ChatJobError(RuntimeError):
    """主路径与降级路径皆败（API 层转 error 事件；message 面向错误体）。"""

    def __init__(self, cause: Exception):
        self.cause = cause
        super().__init__(f"数据源降级/不可用: {type(cause).__name__}: {cause}")


_shared_runner: ToolRunner | None = None


def shared_runner() -> ToolRunner:
    """ToolRunner 进程内单例（纯执行器：注册表由各请求按挂载子集显式传入，
    P0-1 收口后并发请求不再共享可变注册表；单进程约定 docs/01 §6）。"""
    global _shared_runner
    if _shared_runner is None:
        _shared_runner = ToolRunner()
    return _shared_runner


def run_chat_job(job: ChatJob, emit: EventFn) -> JobOutcome:
    """编排入口（绝不挂死）：span 树根（WP3）chat.turn 覆盖主路径与降级路径全程；
    属性按隐私白名单只放元数据（obs/tracing.py docstring），问题原文不入 span。"""
    t0 = time.perf_counter()  # 降级落库耗时口径起点（含主路径失败前的尝试时长，诚实全程）
    with span("chat.turn", provider=job.provider or "", is_guest=job.is_guest,
              question_len=len(job.message), lang=job.lang,
              web_enabled=job.web_enabled,
              has_conversation=bool(job.conversation_id)):
        return _run_chat_turn(job, emit, t0)


def _run_chat_turn(job: ChatJob, emit: EventFn, t0: float) -> JobOutcome:
    """主路径 → 失败降级直答 → 再败抛 ChatJobError（WP3 拆出：span 包裹层不改变
    原语句缩进，pyrefly baseline 列位不漂移）。"""
    try:
        return _run_main(job, emit)
    except Exception as exc:
        try:
            emit("reset", {"reason": "agent_fallback"})  # P2-32：清空已流半截正文再降级（与 _stream_plain 断流即收口径对齐）
            result = direct_rag_answer(job.message, job.lang, job.image_refs,
                                       emit, str(exc), provider=job.provider)
            result.conversation_id = job.conversation_id
            _persist_degrade(job, result, t0)  # 与主路径对称：降级回答同样可回放
            return JobOutcome(result=result)
        except Exception:
            raise ChatJobError(exc) from exc


def _persist_degrade(job: ChatJob, result: LoopResult, t0: float) -> None:
    """降级直答落库（与主路径同构，docs/01 §3.1）：游客进进程内缓存不落库；
    普通用户落 PG，assistant item 附 build_done 收尾结构（刷新后可回放）。
    持久化失败不抛（能力型）——降级回答已投递，落库尽力而为。"""
    try:
        if job.is_guest:
            guest_history.append(job.conversation_id, job.message, result.answer)
            return
        stored_done = build_done(result, elapsed_ms=int((time.perf_counter() - t0) * 1000),
                                 trace_id=job.trace_id, provider=job.provider or "")
        store = build_conversation_store()
        _, _, prior_summary = load_history(store, job.conversation_id)  # P2-31：降级落库保留原滚动摘要（原硬编码 "" 会抹掉 C7 摘要）
        persist_result(store, job.conversation_id, job.message,
                       result, job.provider or "", prior_summary,
                       user_id=job.user_id, done=stored_done)
    except Exception as exc:
        logger.warning("降级回答落库失败 conv=%s: %s: %s",
                       (job.conversation_id or "")[:8], type(exc).__name__, str(exc)[:160])


def _run_main(job: ChatJob, emit: EventFn) -> JobOutcome:
    t_job = time.perf_counter()  # 落库收尾结构的耗时口径起点（编排全程）
    chat = build_chat_for(job.provider)  # C8/A12 profile 路由
    retriever = build_retriever()
    store = None if job.is_guest else build_conversation_store()
    registry = build_registry(retriever, chat, build_bp_repo(), build_neo(),
                              build_web_clients(), build_reranker())
    if chat is None or not registry.names():
        raise DataSourceError("chat", "核心槽位/检索不可用")
    if job.is_guest:
        conv_id = job.conversation_id
        history = guest_history.load(conv_id)
        prior_summary = ""
    else:
        # 读侧可见域下推（审计 P1-1 纵深）：登录用户只读本人会话；机器 token/
        # legacy（user_id None）不过滤（管理员级 + 无用户域）。fake store 桩是
        # 单参签名，仅 user_id 存在时才传 viewer。
        viewer = (job.user_id, False) if job.user_id else None
        conv_id, history, prior_summary = load_history(store, job.conversation_id,
                                                       viewer=viewer)
    loop = AgentLoop(chat, registry, shared_runner(), temperature=0.3)
    # 图片上下文只进模型、不落库（描述是 TTL 10min 的易逝旁路；会话存原始问题）
    question = job.message
    image_block = image_context_block(job.image_refs)
    if image_block:
        question = f"{question}\n\n{image_block}"
    result = loop.run(
        question, history=history,
        summary_text=prior_summary or None,
        web_enabled=job.web_enabled,
        thinking=job.thinking_enabled,
        thinking_model=job.thinking_model,
        first_round=not bool(history),  # 有历史=非首轮（澄清仅首轮，C2）
        on_event=emit,
        lang=job.lang,
        plan_enabled=job.plan_enabled,      # WP5a（api 层自 AGENT_*_ENABLED 注入）
        reflect_enabled=job.reflect_enabled,
    )
    if job.is_guest:
        guest_history.append(conv_id, job.message, result.answer)
        result.conversation_id = conv_id
        return JobOutcome(result=result)
    # 落库收尾结构与 SSE done 同一 build_done 产物（刷新后可回放来源/工具/图表）；
    # elapsed 为编排耗时，与 done 事件端到端耗时同源不同刻（差值为收尾组装的毫秒级）
    stored_done = build_done(result, elapsed_ms=int((time.perf_counter() - t_job) * 1000),
                             trace_id=job.trace_id, provider=job.provider or "")
    saved_id = persist_result(store, conv_id or job.conversation_id, job.message,
                              result, job.provider or "", prior_summary,
                              user_id=job.user_id, done=stored_done)
    # C7 压缩延后：调用方在收尾事件投递之后执行（保持原时序，不阻塞 done）
    return JobOutcome(result=result,
                      compress=defer_compress(store, saved_id, chat, job.message,
                                              result, prior_summary, history,
                                              user_id=job.user_id))