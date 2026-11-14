"""问答编排用例（docs/01 §3.3）：装配 → 历史 → Agent 循环 → 落库 → 压缩延后。

从 chat_stream._worker 归位（重构 C3）：本模块不 import FastAPI——入参是纯
ChatJob（api/chat/stream 负责把请求模型转成它），业务流程可脱离 HTTP 单测。
工具表组装走 agent/assembly.build_registry（唯一实现，与评测共用）。
主路径失败 → 降级直答（degrade）；再败 → ChatJobError（API 层转 error 事件）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from bidqa.agent.assembly import build_registry
from bidqa.agent.loop import AgentLoop, LoopResult
from bidqa.application.chat.degrade import direct_rag_answer
from bidqa.application.chat.history import (
    defer_compress, guest_history, load_history, persist_result,
)
from bidqa.application.vision import image_context_block
from bidqa.clients.chat_factory import build_chat_for
from bidqa.clients.siliconflow import DataSourceError
from bidqa.database.factory import build_bp_repo, build_conversation_store, build_neo
from bidqa.rag.factory import build_retriever, build_reranker
from bidqa.tools.runner import ToolRunner
from bidqa.clients.web_factory import build_web_clients

EventFn = Callable[[str, dict[str, Any]], None]


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
    is_guest: bool = False    # guest=不落库、历史走进程内缓存（docs/01 §3.1）
    user_id: str | None = None  # 落库归属；None=机器域（机器主 token/legacy）


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
    """ToolRunner 进程内单例（工具在执行期按注册表查找；单进程约定 docs/01 §6）。"""
    global _shared_runner
    if _shared_runner is None:
        from bidqa.tools.base import ToolRegistry

        _shared_runner = ToolRunner(ToolRegistry())
    return _shared_runner


def run_chat_job(job: ChatJob, emit: EventFn) -> JobOutcome:
    """编排入口：主路径 → 失败降级直答 → 再败抛 ChatJobError（绝不挂死）。"""
    try:
        return _run_main(job, emit)
    except Exception as exc:
        try:
            result = direct_rag_answer(job.message, job.lang, job.image_refs,
                                       emit, str(exc))
            result.conversation_id = job.conversation_id
            return JobOutcome(result=result)
        except Exception:
            raise ChatJobError(exc) from exc


def _run_main(job: ChatJob, emit: EventFn) -> JobOutcome:
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
        conv_id, history, prior_summary = load_history(store, job.conversation_id)
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
    )
    if job.is_guest:
        guest_history.append(conv_id, job.message, result.answer)
        result.conversation_id = conv_id
        return JobOutcome(result=result)
    saved_id = persist_result(store, conv_id or job.conversation_id, job.message,
                              result, job.provider or "", prior_summary,
                              user_id=job.user_id)
    # C7 压缩延后：调用方在收尾事件投递之后执行（保持原时序，不阻塞 done）
    return JobOutcome(result=result,
                      compress=defer_compress(store, saved_id, chat, job.message,
                                              result, prior_summary, history))