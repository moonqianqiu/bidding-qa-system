"""Agent 工具循环（docs/02 C3/C4/C5/C6；分层：调度≠生成≠执行）。

C3 ≤4 轮 ReAct：一轮内多工具并行（ToolRunner 整批 30s 墙钟）；超时/异常/坏参数
统一 tool 消息回填对话；触顶强制收口（hit_cap）。
C4 防泄漏：最终文本命中 4 类规则任一 → emit reset → 兜底解析真实执行（还有轮次时）
→ 否则重生成一次 → 仍泄漏则净化保底。
C5 澄清闸门：首轮 + 限定条件判据（agent/clarify.py）命中 → 仅追问不检索不生成；
模型不配合时用固定话术兜底（量化验收：无限定条件数字题 100% 触发）。
C6 最终生成：常规路径取循环自然产出的文本（单 token 事件下发，省一次调用）；
深度思考开启时用思考模型重新流式生成（thinking 与正文分流，正文空时思考兜底；
契约要求 thinking* 先于 token*，故思考增量先缓冲后统一下发）。
WP5a：Plan-and-Solve（复合问题取数前一次规划，计划入 system prompt；agent/plan.py）
+ Reflection 证据自检（工具取数后、最终作答前恰 1 次，不足且有剩余轮次注入
补查提示；agent/reflect.py）。两者开关可控（run 的 plan_enabled/reflect_enabled，
api 层自 AGENT_*_ENABLED 注入），关闭时行为与旧版逐字一致、零额外模型调用；
失败一律降级（无计划/不补查），绝不阻塞回答。

单进程约定（docs/01 §6）；事件回调 on_event(type, payload) 由 SSE 层转协议。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from bidqa.agent import leaks
from bidqa.agent.clarify import is_underqualified
from bidqa.agent import skills as skills_mod
from bidqa.agent.context import HIT_CAP_SECTION, build_system_prompt, mount_tools
from bidqa.agent.plan import build_plan, should_plan
from bidqa.agent.reflect import (
    REFLECT_HINT_SECTION,
    reflect_on_evidence,
    summarize_observations,
)
from bidqa.obs.tracing import set_attr, span

# 模型返回空正文且补发仍空时的诚实兜底（不是答案，只是告诉用户发生了什么）
EMPTY_ANSWER_TEXT = "本次没有拿到模型回答（上游返回空内容），请重试一次或换个问法。"
EMPTY_ANSWER_TEXT_EN = ("No answer was returned by the model (upstream returned "
                        "empty content). Please retry or rephrase.")

from bidqa.tools.base import ToolRegistry
from bidqa.tools.runner import ToolCall, ToolOutcome, ToolRunner

MAX_ROUNDS = 4  # ≤4 轮（docs/02 C3）

EventFn = Callable[[str, dict[str, Any]], None]

_GENERIC_CLARIFY = (
    "为准确回答，请补充以下信息：①地区（省/市）②时间范围 ③具体标的物（如空调、电梯）。"
)
_GENERIC_CLARIFY_EN = (
    "To answer accurately, please provide: (1) region (province/city) "
    "(2) time range (3) subject matter (e.g., air conditioners, elevators)."
)


@dataclass
class LoopResult:
    answer: str
    rounds: int = 0
    hit_cap: bool = False
    clarify: bool = False
    tool_called: bool = False
    first_tool: str = ""
    tool_trace: list[list[dict]] = field(default_factory=list)  # 逐轮 [{name, arguments}]
    sources: list[dict] = field(default_factory=list)
    charts: list[dict] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    phase_times: dict[str, int] = field(default_factory=dict)
    plan_steps: list[str] = field(default_factory=list)  # WP5a 计划（未规划/失败=[]）
    reflected: bool = False  # WP5a 反思触发补查是否发生（不含「判定证据足够」的未注入自检）
    conversation_id: str = ""  # 落库归属（worker 持久化后回填；""=未落库）


class AgentLoop:
    def __init__(self, chat: Any, registry: ToolRegistry, runner: ToolRunner,
                 *, temperature: float = 0.3):
        self.chat = chat
        self.registry = registry
        self.runner = runner
        self.temperature = temperature

    # ── 对外入口 ──
    def run(self, question: str, *, history: list[dict] | None = None,
            summary_text: str | None = None, web_enabled: bool = False,
            thinking: bool = False, thinking_model: str = "",
            first_round: bool = True, on_event: EventFn | None = None,
            lang: str = "zh", plan_enabled: bool = False,
            reflect_enabled: bool = False) -> LoopResult:
        events: EventFn = on_event or (lambda _t, _p: None)
        t0 = time.perf_counter()
        result = LoopResult(answer="")
        names = self.registry.names()

        mounted = mount_tools(self.registry, web_enabled=web_enabled, first_round=first_round)
        underq = first_round and is_underqualified(question)
        skill_text = skills_mod.skill_text_for(  # C9 零往返（一层点名/二层指代回看）
            question, skills_mod.get_skills(), history=history)
        # WP5a Plan-and-Solve：复合问题取数前一次规划（主模型槽位）。
        # 开关关/简单问题/澄清判据命中=零调用；规划失败=[] 降级为无计划，绝不阻塞回答。
        plan_steps: list[str] = []
        if plan_enabled and not underq and should_plan(question, history_turns=len(history or [])):
            plan_steps = build_plan(chat=self.chat, question=question)
            result.plan_steps = plan_steps
        messages: list[dict] = [{
            "role": "system",
            "content": build_system_prompt(
                tool_table=mounted.prompt_table(), first_round=first_round,
                qualified=not underq, summary_text=summary_text, lang=lang,
                skill_text=skill_text, plan_steps=plan_steps),
        }, *(history or []), {"role": "user", "content": question}]

        # ── C5 澄清闸门：首轮 + 无限定条件数字题 → 仅追问（不检索不生成）──
        if underq:
            return self._clarify_path(messages, mounted, result, events, t0, lang=lang)

        rounds = 0
        reflected = False  # WP5a：本轮（一次问答）最多 1 次反思触发
        while rounds < MAX_ROUNDS:
            rounds += 1
            with span("agent.round", round=rounds):  # WP3：每轮一个 span（子树见 runner/rag 各自埋点）
                if rounds >= MAX_ROUNDS - 1 and rounds > 1:
                    # 触顶预警（hit_cap 治理）：倒数第二轮起提示模型收口
                    messages.append({"role": "system", "content":
                        f"已进行 {rounds - 1} 轮取数，最多还剩 {MAX_ROUNDS - rounds} 轮。"
                        "除非缺少关键维度数据，否则请基于已有结果直接作答。"})
                events("status", {"stage": "分析中"})
                # 真流式轮次（C6）：content 增量实时下发（thinking 模式下草稿会被重写，抑制直播）；
                # 若流末出现 tool_calls，说明已流的是伴随文本 → reset 清空（前端契约已支持）。
                live = not (thinking and thinking_model)
                content_parts: list[str] = []
                tc_acc: dict[int, dict] = {}
                # WP3：LLM 调用 span（model/round 白名单元数据；outcome 流末回填）
                with span("llm.chat_stream", model=getattr(self.chat, "model", ""),
                          round=rounds) as llm_span:
                    for kind, payload in self.chat.chat_stream_round(
                            messages, mounted.openai_schemas(), temperature=self.temperature):
                        if kind == "content":
                            content_parts.append(payload)
                            if live:
                                events("token", {"text": payload})
                        else:  # ("tool", (index, id, name, args片段))
                            index, tc_id, name, frag = payload
                            entry = tc_acc.setdefault(index, {"id": tc_id or "", "name": name or "",
                                                              "arguments": ""})
                            if tc_id:
                                entry["id"] = tc_id
                            if name:
                                entry["name"] = name
                            if frag:
                                entry["arguments"] += frag
                    set_attr(llm_span, "outcome", "tool_calls" if tc_acc else "text")
                if tc_acc:
                    if live and any(content_parts):  # 伴随文本不是最终回答 → 清空已流正文
                        events("reset", {"reason": "tool_round_preamble"})
                    calls = []
                    for _, v in sorted(tc_acc.items()):
                        try:
                            args = json.loads(v["arguments"]) if v["arguments"] else {}
                        except json.JSONDecodeError:
                            args = {"_raw": v["arguments"]}
                        calls.append((v["id"], v["name"], args))
                    msg = {"role": "assistant",
                           "content": "".join(content_parts) or None,
                           "tool_calls": [{"id": cid, "type": "function",
                                           "function": {"name": n,
                                                        # OpenAI 协议：arguments 必须是 JSON 字符串
                                                        # （回传对象会被 SenseNova 400 invalid arguments）
                                                        "arguments": json.dumps(a, ensure_ascii=False)}}
                                          for cid, n, a in calls]}
                else:
                    text = "".join(content_parts)
                    return self._finalize(text, messages, names, result, rounds, events,
                                          t0, mounted=mounted,
                                          thinking=thinking, thinking_model=thinking_model,
                                          already_streamed=live, lang=lang)

                result.tool_called = True
                if not result.first_tool:
                    result.first_tool = calls[0][1]
                result.tool_trace.append([{"name": n, "arguments": a} for _, n, a in calls])

                events("status", {"stage": "取数中"})
                batch = [ToolCall(name=n, arguments=a) for _, n, a in calls]
                outcomes = self.runner.run_batch(batch, mounted)  # 挂载子集（P0-1/P1-9）
                self._collect(outcomes, result)

                for out in outcomes:  # D6 中途触发（工具表内被模型点名）→ 同样走澄清收口
                    if out.payload.get("clarify"):
                        return self._clarify_done(str(out.payload.get("text", "")),
                                                  result, rounds, events, t0)

                messages.append(msg)  # 带 tool_calls 的 assistant 原样回填
                for out, (call_id, _n, _a) in zip(outcomes, calls):
                    messages.append(out.as_tool_message(tool_call_id=call_id))

                # WP5a Reflection（证据自检）：工具取数后、最终作答前恰 1 次；
                # 不足且未触顶 → 注入补查提示继续循环（不做答案后重写）。
                # 开关关/无工具轮/已反思/触顶=零调用；自检失败=不补查，绝不阻塞。
                if reflect_enabled and not reflected and rounds < MAX_ROUNDS:
                    need_more, hint = reflect_on_evidence(
                        chat=self.chat, question=question,
                        observations_summary=summarize_observations(outcomes))
                    if need_more and hint:
                        reflected = True
                        result.reflected = True
                        messages.append({"role": "system", "content":
                                         REFLECT_HINT_SECTION.format(hint=hint)})

        # ── 触顶强制收口（C3：基于已有结果直接作答；真流式，thinking 下抑制直播）──
        result.hit_cap = True
        result.rounds = rounds
        messages.append({"role": "system", "content": HIT_CAP_SECTION})
        events("status", {"stage": "生成中"})
        if thinking and thinking_model:
            text = str(self.chat.chat(messages, temperature=self.temperature))
            already_streamed = False
        else:
            text = self._stream_plain(messages, events)
            already_streamed = True
        return self._finalize(text, messages, names, result, rounds, events, t0,
                              mounted=mounted,
                              thinking=thinking, thinking_model=thinking_model,
                              already_streamed=already_streamed, lang=lang)

    # ── 内部阶段 ──
    def _clarify_path(self, messages: list[dict], mounted: ToolRegistry,
                      result: LoopResult, events: EventFn, t0: float,
                      lang: str = "zh") -> LoopResult:
        events("status", {"stage": "向您确认"})
        result.rounds = 1
        try:
            msg = self.chat.chat_tools(messages, mounted.openai_schemas(),
                                       temperature=self.temperature)
        except Exception:
            msg = {}
        question = _GENERIC_CLARIFY_EN if lang == "en" else _GENERIC_CLARIFY
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            if fn.get("name") == "ask_clarification":
                try:
                    raw = fn.get("arguments", "{}")
                    args = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    text = str(args.get("question_for_user", "")).strip()
                    if len(text) >= 2:
                        question = text
                        # 动态追问走了工具：契约字段落位（docs/01 §3.4 tool_name；
                        # 审计 P0-2——原路径不可达，固定话术 100% 兜底）
                        result.tool_called = True
                        if not result.first_tool:
                            result.first_tool = "ask_clarification"
                        result.tool_trace.append([{
                            "name": "ask_clarification",
                            "arguments": args if isinstance(args, dict) else {}}])
                except json.JSONDecodeError:
                    pass
                break
        return self._clarify_done(question, result, 1, events, t0)

    def _clarify_done(self, question: str, result: LoopResult, rounds: int,
                      events: EventFn, t0: float) -> LoopResult:
        result.clarify = True
        result.answer = question
        result.phase_times = {"rounds_ms": self._ms(t0), "澄清提问": 0}
        events("token", {"text": question})
        return result

    def _stream_plain(self, messages: list[dict], events: EventFn) -> str:
        """无工具强制生成，真流式下发 token。

        断流即收（2026-11-11 演示日缺陷修复，C3 清债 #1）：流中异常不再丢弃已流
        正文、也不再整段重放非流式结果造成「半截+全文」拼接——已流部分即答案
        （与前端 A8 中断保留语义对齐）；一个 token 都没流出来才回退非流式一次。
        """
        parts: list[str] = []
        try:
            for chunk in self.chat.chat_stream(messages, temperature=self.temperature):
                parts.append(chunk)
                events("token", {"text": chunk})
        except Exception:
            pass  # 已流正文保留，不补发全文
        text = "".join(parts)
        if text.strip():
            return text
        text = str(self.chat.chat(messages, temperature=self.temperature))
        events("token", {"text": text})
        return text

    def _finalize(self, text: str, messages: list[dict], names: list[str],
                  result: LoopResult, rounds: int, events: EventFn, t0: float,
                  *, mounted: ToolRegistry, thinking: bool, thinking_model: str,
                  already_streamed: bool = False, lang: str = "zh") -> LoopResult:
        """C4 泄漏闸 + C6 最终产出。already_streamed=正文已随流下发（勿重复发）。
        mounted=本会话挂载子集（C2）：泄漏恢复的执行门与续写 tools 都以它为口径——
        未挂载工具（如 web 关时的 search_web）绝不因泄漏兜底而复活。"""
        result.rounds = rounds
        leaked_at = leaks.detect_leak(text, names)
        if leaked_at:
            events("reset", {"reason": "leak:" + leaked_at})
            events("status", {"stage": "重新整理"})
            inline = leaks.parse_inline_tool_call(text)
            if inline is not None and rounds < MAX_ROUNDS and mounted.get(inline.name):
                # 文本兜底：解析出真实执行，继续循环（消耗一个新轮次）；
                # 执行门=挂载子集（未挂载工具回落重生成，runner 不经手）
                result.tool_called = True
                if not result.first_tool:
                    result.first_tool = inline.name
                result.tool_trace.append([{"name": inline.name, "arguments": inline.arguments}])
                outcomes = self.runner.run_batch([inline], mounted)  # 挂载子集（P0-1/P1-9）
                self._collect(outcomes, result)
                # 协议合法的 FC 序列（审计 P1-12：assistant 无 tool_calls + tool 消息
                # tool_call_id="" 会被真实 OpenAI-compatible 端点 400 → 异常冒泡整轮降级）
                fake_id = f"inline_{self._ms(t0)}"
                messages.append({"role": "assistant", "content": text,
                                 "tool_calls": [{"id": fake_id, "type": "function",
                                                 "function": {"name": inline.name,
                                                              "arguments": json.dumps(
                                                                  inline.arguments,
                                                                  ensure_ascii=False)}}]})
                messages.append(outcomes[0].as_tool_message(fake_id))
                events("status", {"stage": "分析中"})
                msg = self.chat.chat_tools(messages, mounted.openai_schemas(),
                                           temperature=self.temperature)
                text = str(msg.get("content") or "")
                if leaks.detect_leak(text, names):
                    text = leaks.sanitize(text, names)
                    result.degraded.append("leak_sanitized")
            else:
                text, sanitized = self._regenerate(messages, names)
                if sanitized:
                    result.degraded.append("leak_sanitized")
        result.answer = text

        # C6 深度思考：思考模型重新流式生成（thinking* 先于 token*，缓冲后下发）
        if thinking and thinking_model:
            if already_streamed and not leaked_at:  # 深度思考会重写正文 → 先清空
                events("reset", {"reason": "thinking_regen"})
            events("status", {"stage": "深度思考"})
            thinking_buf: list[str] = []
            content_buf: list[str] = []
            try:
                for kind, chunk in self.chat.chat_stream_thinking(
                        messages, thinking_model, temperature=self.temperature):
                    if kind == "thinking":
                        thinking_buf.append(chunk)
                    else:
                        content_buf.append(chunk)
            except Exception as exc:
                result.degraded.append(f"thinking_fallback: {exc}")
            regenerated = bool(content_buf or thinking_buf)
            if content_buf:  # 正文空时思考兜底（C6）
                body = "".join(content_buf)
            elif thinking_buf:
                body = "".join(thinking_buf)
                result.degraded.append("thinking_as_body")
            else:
                body = text
                # P1-13：主循环正文在 thinking 模式下被抑制直播（live=False），思考流
                # 又为空 → 此刻正文从未下发，必须补发（原 body is text 跳过 → SSE 端
                # 零正文只落库）。口径与非思考分支一致（未流或泄漏 reset 后都补发）。
                if not already_streamed or leaked_at:
                    for chunk in _chunk(body):
                        events("token", {"text": chunk})
            if regenerated:
                for chunk in thinking_buf:
                    events("thinking", {"text": chunk})
                for chunk in _chunk(body):
                    events("token", {"text": chunk})
            result.answer = body
        elif not already_streamed or leaked_at:
            for chunk in _chunk(result.answer):
                events("token", {"text": chunk})

        if not (result.answer or "").strip():
            # 上游偶发返回空正文（根因=思考型上游先烧 reasoning 再出正文，输出预算不足时
            # content=None finish=length；2026-11-10 trace 图表正常、正文空；
            # 2026-11-11 trace 9a86dbb765a9 复现并定位，预算头寸见 clients/llm.py _budgeted）：
            # 先服务端补发一次无工具直答（前端提示的「重试一次」自动做），仍空才给诚实文案。
            # 补发文本同样过 C4 泄漏闸；失败不升级故障，走诚实文案兜底。
            events("status", {"stage": "重新生成"})
            retry = ""
            try:
                retry = self._stream_plain(messages, events)
            except Exception:
                retry = ""
            if retry.strip():
                result.degraded.append("empty_answer_retry")  # 补发发生=降级信号如实记录
                if leaks.detect_leak(retry, names):
                    retry = leaks.sanitize(retry, names)
                    result.degraded.append("leak_sanitized")
                result.answer = retry
            else:
                result.degraded.append("empty_answer")
                result.answer = EMPTY_ANSWER_TEXT_EN if lang == "en" else EMPTY_ANSWER_TEXT
                for chunk in _chunk(result.answer):
                    events("token", {"text": chunk})

        result.phase_times = {"rounds_ms": self._ms(t0), "generate_ms": 0}
        return result

    def _regenerate(self, messages: list[dict], names: list[str]) -> tuple[str, bool]:
        """重生成一次（附反泄漏指令）；仍泄漏则净化保底。返回 (文本, 是否净化)。"""
        nudged = messages + [{"role": "system", "content":
                              "注意：正文严禁出现任何工具调用语法（调用X(…)/tool_calls JSON/"
                              "```tool 栅栏/<tool_call> 标签/DSML 特殊 token（<｜DSML｜…））。"
                              "直接给出面向用户的回答。"}]
        text = str(self.chat.chat(nudged, temperature=self.temperature))
        if leaks.detect_leak(text, names):
            return leaks.sanitize(text, names), True
        return text, False

    def _collect(self, outcomes: list[ToolOutcome], result: LoopResult) -> None:
        for out in outcomes:
            if not out.ok:
                result.degraded.append(f"{out.call.name}: {out.payload.get('error')}")
                continue
            result.sources.extend(out.payload.get("sources") or [])
            result.charts.extend(out.payload.get("charts") or [])

    @staticmethod
    def _ms(t0: float) -> int:
        return int((time.perf_counter() - t0) * 1000)


def _chunk(text: str, size: int = 80) -> list[str]:
    if not text:
        return []
    return [text[i:i + size] for i in range(0, len(text), size)]
