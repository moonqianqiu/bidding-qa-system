"""OpenTelemetry span 埋点收口（WP3；docs/01 §6 可观测性段）。

设计（对接说明见 docs/部署手册「接 Langfuse/Jaeger」）：
- 默认零开销 no-op：仅当 OTEL_EXPORTER_OTLP_ENDPOINT 非空才初始化 TracerProvider
  并设为全局；未配置时 span() 走 API 的 NoOpTracer（NonRecordingSpan），不改全局
  provider、不起线程、不产生网络与序列化开销。模块导入期只拉 opentelemetry API
  （SDK/OTLP exporter 在 setup_tracing 内惰性导入，未配置进程不加载）。
- 导出：BatchSpanProcessor + OTLPSpanExporter（OTLP/HTTP，端点自动补 /v1/traces；
  可对接 Langfuse 的 OTLP ingestion、Jaeger、任意 OTLP collector）。exporter 参数
  仅供测试注入（InMemorySpanExporter → SimpleSpanProcessor 同步导出，断言确定性）。
- 隐私白名单（AGENTS 硬约束 6 同源纪律）：span 属性只放结构化元数据——长度、
  工具名、模型名、耗时、tokens、rounds、outcome、degraded 等；问题原文、回答
  正文、密钥绝不入 span。白名单由调用点遵守，span() 对字符串属性统一截断
  （MAX_ATTR_LEN）作纵深防御。
- 生命周期：_lifespan 启动 setup_tracing / 停机 shutdown_tracing（幂等）；
  导出失败不影响业务（SDK 内部吞错记日志）。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

TRACER_NAME = "bidqa.obs"
TRACES_SUFFIX = "/v1/traces"
MAX_ATTR_LEN = 120  # 字符串属性截断上限（纵深防御，白名单外内容本就不该传入）

_provider: Any | None = None  # TracerProvider | None（SDK 惰性导入，运行期才确定）


def _endpoint_of(settings: Any) -> str:
    """OTLP 端点：显式传入的 Settings 字段优先（pydantic-settings 已合并 env>.env），
    否则只看进程环境变量；两者皆空 = 关闭。"""
    if settings is not None:
        endpoint = str(getattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "") or "").strip()
        if endpoint:
            return endpoint
    return os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()


def _service_name(settings: Any) -> str:
    if settings is not None:
        name = str(getattr(settings, "OTEL_SERVICE_NAME", "") or "").strip()
        if name:
            return name
    return os.environ.get("OTEL_SERVICE_NAME", "").strip() or "bidqa"


def _service_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("bidqa")
    except PackageNotFoundError:
        return "0.1.0"


def _traces_url(endpoint: str) -> str:
    """OTLPSpanExporter 显式 endpoint 用原值不补路径（SDK 语义），故此处统一补
    /v1/traces；已以该后缀结尾（如 Langfuse 完整 OTLP URL）则原样。"""
    return endpoint if endpoint.endswith(TRACES_SUFFIX) else endpoint + TRACES_SUFFIX


def _clean_attr(value: Any) -> Any:
    """属性归一：None 剔除（取不到的数值不记，绝不记假值）；字符串截断（纵深）。"""
    if value is None:
        return None
    if isinstance(value, str) and len(value) > MAX_ATTR_LEN:
        return value[:MAX_ATTR_LEN]
    return value


def setup_tracing(settings: Any = None, *, exporter: Any | None = None) -> Any | None:
    """按配置初始化全局 TracerProvider（OTLP/HTTP）。

    返回 TracerProvider；未配置端点返回 None 且不动全局 provider（no-op 语义）。
    exporter 仅供测试注入 InMemorySpanExporter（走 SimpleSpanProcessor 同步导出）。
    """
    endpoint = _endpoint_of(settings)
    if not endpoint:
        return None
    global _provider
    if _provider is not None:
        return _provider  # 幂等：进程内一次性初始化（重复调用返回现有 provider）
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    deployment_env = os.environ.get("DEPLOYMENT_ENVIRONMENT", "").strip()
    resource = Resource.create({
        "service.name": _service_name(settings),
        "service.version": _service_version(),
        **({"deployment.environment": deployment_env} if deployment_env else {}),
    })
    provider = TracerProvider(resource=resource)
    if exporter is not None:  # 测试注入：同步导出，断言无需等批量 flush
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=_traces_url(endpoint))))
    # 公开 API 仅首次生效；重复 set 会告警忽略——幂等由 _provider 挡在前面
    trace.set_tracer_provider(provider)
    _provider = provider
    return provider


def shutdown_tracing() -> None:
    """停机钩子（_lifespan ④ 与 close_pools 同段）：flush + 停导出，幂等。"""
    global _provider
    provider, _provider = _provider, None
    if provider is not None:
        provider.shutdown()  # SDK 侧自身幂等；此后新 span 走 NoOpTracer


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Any]:
    """span 上下文管理器（白名单纪律见模块 docstring）。

    异常路径：record_exception + 状态 ERROR（描述=异常类型名，不带异常文本，免得
    问题原文/密钥经异常 message 泄入 span）后原样上抛。未配置时全程 no-op。
    """
    tracer = trace.get_tracer(TRACER_NAME)
    with tracer.start_as_current_span(name, record_exception=False,
                                      set_status_on_exception=False) as recording:
        for key, value in attrs.items():
            cleaned = _clean_attr(value)
            if cleaned is not None:
                recording.set_attribute(key, cleaned)
        try:
            yield recording
        except Exception as exc:
            recording.record_exception(exc)
            recording.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise


def set_attr(current: Any, key: str, value: Any) -> None:
    """span() 体内补记属性（同白名单/截断规则）：outcome 等执行后才知道的值。"""
    cleaned = _clean_attr(value)
    if cleaned is not None:
        current.set_attribute(key, cleaned)


def current_trace_id() -> str:
    """当前 trace id（32 hex）；未启用/无效返回 ""（供日志与响应关联）。"""
    ctx = trace.get_current_span().get_span_context()
    if ctx is not None and ctx.is_valid:
        return f"{ctx.trace_id:032x}"
    return ""
