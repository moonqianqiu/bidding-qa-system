"""回归钉子（NIGHT-014）：优雅停机 → 进程退出码 0；force_exit/真异常保持非零。

背景（uvicorn 0.54 server.py capture_signals）：优雅停机完成后，uvicorn 会把
捕获到的信号用**恢复后的原 handler** 重放（restore 后 signal.raise_signal）——
SIGBREAK/SIGTERM 落回默认处置直接终止进程：Windows rc=3、Unix rc=128+sig。
停机本体（含 BidqaServer.shutdown 的 SSE error 广播）早已完成，deploy/start.sh
等部署侧若按 exit 0 判定停机成功，会被误判为失败。

修复（cli/serve.py 两处）：
1. BidqaServer.capture_signals 包装父实现：离开前清空 _captured_signals 抑制
   重放（finally 清空兼管异常路径——带捕获信号的异常不再被重放掩盖成 rc=3，
   真实异常退出码与 traceback 得以保留）；
2. cmd_serve 显式区分退出码：优雅停机 0；force_exit（SIGINT 连发强杀）130
   （128+SIGINT 惯例——修复前靠信号重放天然非零，抑制后必须显式保持）；
   真异常（含 uvicorn 启动失败 SystemExit(1)、ConfigError rc=2）原样传播不吞码。

验证三层：
- 进程内单测：monkeypatch signal.raise_signal 观察重放调用（红/绿均不杀 runner）；
- cmd_serve 退出码映射单测（patch BidqaServer.run，不起真服务）；
- 真实子进程端到端（临时端口）：真信号（win32=CTRL_BREAK_EVENT /
  posix=SIGTERM）→ 退出码 0；端口占用启动失败 → 退出码非 0（真异常不吞码）。
子进程跑的是真实 cmd_serve（app/settings 用桩注入，零 .env 与外部服务依赖）。
"""

from __future__ import annotations

import signal
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
import uvicorn
from uvicorn.server import HANDLED_SIGNALS

from bidqa.cli.serve import BidqaServer, cmd_serve

_PLATFORM_SIGNAL = HANDLED_SIGNALS[-1]  # win32 → SIGBREAK（Ctrl+Break 生产主信号）；posix → SIGTERM
_FORCE_EXIT_RC = 130  # 128+SIGINT 惯例：SIGINT 连发强制中断 → 非零


def _make_server() -> BidqaServer:
    """轻量 Server：不 load/不运行（Config 不校验 app 形态），仅测信号捕获收尾。"""

    def _noop_asgi(scope, receive, send) -> None:  # pragma: no cover - 不应被调用
        raise AssertionError("Server 不应在信号捕获单测中真正运行")

    config = uvicorn.Config(app=_noop_asgi, host="127.0.0.1", port=0,
                            log_level="warning", lifespan="off")
    return BidqaServer(config)


def _stub_settings() -> SimpleNamespace:
    return SimpleNamespace(validate_core=lambda: None)


def _serve_args(port: int = 0) -> SimpleNamespace:
    return SimpleNamespace(host="127.0.0.1", port=port)


# ── 第一层：capture_signals 重放抑制（进程内，红/绿均不杀 runner）──────────


def test_capture_signals_suppresses_signal_replay(monkeypatch) -> None:
    """优雅停机收尾不得重放捕获信号（重放 = 进程被默认 handler 终止 → rc≠0）。"""
    replayed: list[int] = []
    monkeypatch.setattr(signal, "raise_signal", lambda sig: replayed.append(sig))
    server = _make_server()
    before = {sig: signal.getsignal(sig) for sig in HANDLED_SIGNALS}

    with server.capture_signals():
        server.handle_exit(_PLATFORM_SIGNAL, None)  # 与真实 Ctrl+Break/kill 同入口

    assert replayed == [], (
        f"停机完成后仍重放捕获信号 {replayed}：恢复后的默认 handler 将终止进程，"
        "优雅停机退出码≠0（NIGHT-014）")
    assert server._captured_signals == [], "捕获列表未清空"
    for sig, handler in before.items():  # handler 安装/恢复语义完全继承父实现
        assert signal.getsignal(sig) is handler, f"signal {sig} 原 handler 未恢复"


def test_capture_signals_exception_path_no_replay_and_propagates(monkeypatch) -> None:
    """异常路径同样不重放：真实异常退出码/traceback 不被重放掩盖成 rc=3。"""
    replayed: list[int] = []
    monkeypatch.setattr(signal, "raise_signal", lambda sig: replayed.append(sig))
    server = _make_server()

    with pytest.raises(RuntimeError, match="真实异常"):
        with server.capture_signals():
            server.handle_exit(_PLATFORM_SIGNAL, None)
            raise RuntimeError("真实异常")

    assert replayed == [], f"异常路径仍重放捕获信号 {replayed}：真异常退出码被掩盖"
    assert server._captured_signals == []


# ── 第二层：cmd_serve 退出码映射（patch run，不起真服务）───────────────────


def test_cmd_serve_graceful_returns_zero(monkeypatch) -> None:
    """优雅停机（含无信号主动停机）：run 正常返回 → exit 0。"""
    monkeypatch.setattr(BidqaServer, "run", lambda self: None)
    assert cmd_serve(_serve_args(), _stub_settings()) == 0


def test_cmd_serve_force_exit_returns_nonzero(monkeypatch) -> None:
    """force_exit（SIGINT 连发强杀，handle_exit 置位）：必须保持非零（130）。"""

    def _forced(self) -> None:
        self.force_exit = True

    monkeypatch.setattr(BidqaServer, "run", _forced)
    rc = cmd_serve(_serve_args(), _stub_settings())
    assert rc == _FORCE_EXIT_RC, f"force_exit 退出码 {rc}≠{_FORCE_EXIT_RC}（必须非零）"


def test_handle_exit_double_sigint_sets_force_exit() -> None:
    """真实 handle_exit 语义钉子：SIGINT 连发 → force_exit（uvicorn 唯一强杀入口）。

    第一信号（平台主停机信号）只优雅停机；第二信号是 SIGINT 才置 force_exit。
    Windows 无法向单个子进程定向投递第二次 Ctrl+C（CTRL_C_EVENT 只能发整个
    控制台，会误伤 pytest runner），故 force_exit 的端到端形态由本测试 +
    test_cmd_serve_force_exit_returns_nonzero（真实 handle_exit → 真实映射）拼合覆盖。
    """
    server = _make_server()
    server.handle_exit(_PLATFORM_SIGNAL, None)
    assert server.should_exit and not server.force_exit, "首信号应只优雅停机"
    server.handle_exit(signal.SIGINT, None)
    assert server.force_exit, "SIGINT 连发未置 force_exit（强杀入口回归）"


# ── 第三层：真实子进程端到端（临时端口；Windows 控制台信号需新进程组）────────


_CHILD_SCRIPT = '''\
"""NIGHT-014 子进程：跑真实 cmd_serve（app/settings 用桩，免依赖真实 .env/外部服务）。"""
import sys
from types import SimpleNamespace

from bidqa.cli.serve import cmd_serve


async def app(scope, receive, send):
    """最小 ASGI：完整 lifespan 应答 + HTTP 200，零外部依赖。"""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    elif scope["type"] == "http":
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})


args = SimpleNamespace(host="127.0.0.1", port=int(sys.argv[1]))
settings = SimpleNamespace(validate_core=lambda: None)
sys.exit(cmd_serve(args, settings, app=app))
'''


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _spawn_child(tmp_path, port: int) -> subprocess.Popen:
    script = tmp_path / "serve_child.py"
    script.write_text(_CHILD_SCRIPT, encoding="utf-8")
    kwargs: dict = {}
    if sys.platform == "win32":
        # CTRL_BREAK 只能定向投递给新进程组（组 id=子 pid），同时避免误伤 pytest 控制台
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen([sys.executable, str(script), str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)


def _wait_listening(proc: subprocess.Popen, port: int, timeout: float = 15.0) -> None:
    """端口可连 ⇒ capture_signals 的 handler 已就位（捕获包裹整个 serve 生命周期）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _out, err = proc.communicate()
            raise AssertionError(
                f"子进程提前退出 rc={proc.returncode}（未进入监听）: "
                f"{err.decode(errors='replace')[-1500:]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    proc.kill()
    proc.wait(timeout=10)
    raise AssertionError("子进程 15s 内未监听端口（startup 卡死）")


def test_graceful_signal_shutdown_process_exits_zero(tmp_path) -> None:
    """端到端：真信号 → 优雅停机 → 退出码 0。

    修复前形态：停机本体完成后 uvicorn 用恢复后的原 handler 重放信号 →
    Windows SIGBREAK 默认处置直接终止进程（rc=3），部署侧按 exit 0 判停机必误判。
    """
    port = _free_port()
    proc = _spawn_child(tmp_path, port)
    try:
        _wait_listening(proc, port)
        sig = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM
        proc.send_signal(sig)
        _out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:  # 兜底清理，不留孤儿服务进程
            proc.kill()
            proc.communicate()
    tail = err.decode(errors="replace")[-1500:]
    assert proc.returncode == 0, (
        f"优雅停机子进程退出码 {proc.returncode}≠0（信号已送达并完成停机）；"
        f"stderr 尾部: {tail}")


def test_startup_failure_process_exits_nonzero(tmp_path) -> None:
    """真异常保持非零：端口被占 → uvicorn startup 绑定失败 SystemExit(1) → rc≠0。"""
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        proc = _spawn_child(tmp_path, port)
        try:
            _out, err = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
    tail = err.decode(errors="replace")[-1500:]
    assert proc.returncode != 0, (
        f"启动失败（真异常）退出码 {proc.returncode}==0：真异常退出码被吞；"
        f"stderr 尾部: {tail}")
