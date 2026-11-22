#!/usr/bin/env bash
# 一键停止：backend → Neo4j → Qdrant → PostgreSQL 逆序停止。
# pid 文件统一记录「真实监听进程」的 Windows pid（start.sh 用 netstat -ano 归属取得）；
# 停止前做属主校验（pid 必须仍持有对应端口才杀）——旧版记 uv/包装进程导致「按 pid 杀不掉，
# 旧代码继续跑」（2026-11-02 实测事故），本版杜绝，且任何情形不误杀非本脚本进程。
#
# backend 段（NIGHT-013）：优雅先行 → 超时强停兜底。默认先向监听 pid 定向投递
# CTRL_BREAK（PowerShell FreeConsole+AttachConsole+GenerateConsoleCtrlEvent，按进程组
# 投递、不误伤同控制台其它进程），送达即触发 BidqaServer.shutdown() 入口的 SSE error
# 5031 广播（广播在 uvicorn 停机等待之前发出，与收尾窗口长短无关）；GRACE_TIMEOUT_S
# 内未退出或投递失败，再按强停阶梯兜底（taskkill //T //F → PowerShell Stop-Process）。
#
# NIGHT-028（2026-11-22）：真控制台下「脚本与服务同控制台」时，上面的 CTRL_BREAK 投递
# 实证为**整控制台广播**（backend_pid 并非 Windows 进程组 id，GenerateConsoleCtrlEvent
# 波及同控制台全部进程）：backend 优雅四步照常完成，但脚本自身 bash 被 CTRL_BREAK
# 击杀（MSYS bash 不可 trap 保护，v1/v2/v3 微缩复现实证）→「脚本卡住不再输出」；
# neo4j.bat 包装 cmd 弹「终止批处理操作吗(Y/N)?」等待输入；Neo4j JVM 仅打线程转储
# 幸存（=事故里需要人工强杀的两个 jdk21）。因此默认改为**双进程架构**：
#   壳进程（用户控制台）：预检 → 以隐藏控制台启动脱离的 worker → exec python waiter
#   有界跟踪 worker 日志（python 实证可幸存 CTRL_BREAK 广播）；
#   worker（隐藏控制台，不在任何服务控制台内）：原四段停机序列 + 每步硬超时 +
#   残留 JVM / Y/N 包装 cmd 清扫，全程写 stop-<ts>.log，结束写 STOP-DONE 标记。
# worker 与服务控制台隔离，投递照常可达（AttachConsole 跨控制台实证可用）；
# 优雅路径与 5031 广播口径不变（NIGHT-013）。--force / --help 语义不变。
#
# 用法：bash deploy/stop.sh [--force]
#   无参数   backend 优雅先行（CTRL_BREAK → 最多 30s 窗口 → 超时/失败强停兜底）
#   --force  backend 跳过优雅尝试，按旧强停路径直接停（数据服务段不受影响）
#   --help   显示本说明
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN="$ROOT/data/run"
GRACE_TIMEOUT_S=30   # 优雅窗口：uvicorn timeout_graceful_shutdown=10 的 SSE 收尾窗口 + 余量；
                     # 极端长尾（停机③在途 futures 排空上限 30s）由兜底强停收尾——5031 广播
                     # 在 BidqaServer.shutdown() 入口已发出，不受兜底时机影响。
WAIT_BOUND_S=240     # 壳进程 waiter 对 worker 的跟踪上限（各段硬超时合计的上界）。
FORCE_STOP=0

usage() {
  cat <<'USAGEEOF'
用法：bash deploy/stop.sh [--force]
  无参数   backend 优雅先行：定向投递 CTRL_BREAK 触发服务端 SSE 5031 广播，最多等 30s，
           超时或投递失败转强停兜底；Neo4j/Qdrant/PostgreSQL 照常停止。
           实际停机序列在脱离控制台的 worker 中执行（NIGHT-028：真控制台下 CTRL_BREAK
           广播会击杀脚本自身与 neo4j.bat 包装 cmd），本命令有界等待其完成（上限 240s），
           全程日志见 data/run/stop-<时间戳>.log。
  --force  backend 跳过优雅尝试，直接按旧强停路径停止；数据服务段不受影响。
  --help   显示本说明。
USAGEEOF
}

if [ "${1:-}" = "--inner" ]; then
  INNER=1
  shift
else
  INNER=0
fi

for _a in "$@"; do
  case "$_a" in
    --force) FORCE_STOP=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$_a" >&2; usage >&2; exit 2 ;;
  esac
done

port_up() { netstat -an | grep LISTENING | grep -qE "[:.]$1[[:space:]]"; }
port_owner() { netstat -ano | grep LISTENING | grep -E "[:.]$1[[:space:]]" | awk '{print $NF; exit}'; }
# 存活检测走 ps -W（无特权，沙箱/受控环境可用）；tasklist 会报「用户名或密码不正确」
win_alive() { ps -W | awk -v p="$1" 'BEGIN{f=0} $4==p{f=1} END{exit f?0:1}'; }
winpid_of() { ps -W | awk -v p="$1" '$1==p{print $4; exit}'; }
# worker 进度输出用外部 printf 进程（非 bash 内建）：内建输出到重定向文件有缓冲，
# 进程异常终止时缓冲丢失；外部进程每次调用即落盘，壳进程 waiter 才能实时跟踪。
msg() { /usr/bin/printf '%s\n' "$*"; }

# 定向投递 CTRL_BREAK（NIGHT-013 优雅先行）：PowerShell P/Invoke 附加目标进程控制台后，
# 以「目标 pid = 进程组 id」定向生成 CTRL_BREAK——仅在目标确为进程组首进程时组内送达；
# 目标非进程组 id 时本环境实证为整控制台广播（NIGHT-028），故投递方必须运行在与服务
# 控制台隔离的 worker 中（见文件头）。rc0=事件已生成（是否生效由后续存活轮询观察）；
# rc2=AttachConsole 失败（目标无控制台/已退出）；rc3=生成失败（目标非进程组首进程或
# 环境拦截）；rc124=死锁防御超时（NIGHT-002 第 3 轮实测过同步 Generate 死锁形态）。
# 任何非 0 均按「投递失败」处理 → 调用方转强停兜底。
grace_ctrl_break() { # $1 = 目标 winpid
  local target="$1" ps enc
  ps=$(cat <<'PSEOF'
$sig = @'
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool FreeConsole();
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool AttachConsole(uint pid);
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool SetConsoleCtrlHandler(IntPtr h, bool add);
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool GenerateConsoleCtrlEvent(uint evt, uint group);
'@
$k = Add-Type -MemberDefinition $sig -Name K -Namespace W13 -PassThru
$t = [uint32]__TARGET__
[void]$k::FreeConsole()
if (-not $k::AttachConsole($t)) { exit 2 }
[void]$k::SetConsoleCtrlHandler([IntPtr]::Zero, $true)
if ($k::GenerateConsoleCtrlEvent(1, $t)) { exit 0 } else { exit 3 }
PSEOF
) || return 1
  ps="${ps/__TARGET__/$target}"
  enc=$(printf '%s' "$ps" | iconv -f UTF-8 -t UTF-16LE | base64 -w0) || return 1
  timeout 10 powershell -NoProfile -EncodedCommand "$enc" >/dev/null 2>&1
}

force_stop_pid() { # $1 = 已过属主校验的 winpid；阶梯与 stop_pid_file 强停段一致（少了已无意义的温和 taskkill）
  local pid="$1"
  if ! win_alive "$pid"; then
    msg "backend: 已停止 (winpid $pid，兜底前复查已退出)"
    return
  fi
  if timeout 10 taskkill //PID "$pid" //T //F >/dev/null 2>&1; then
    msg "backend: 已停止 (winpid $pid，优雅窗口超时，强杀兜底)"
  elif timeout 15 powershell -NoProfile -Command "Stop-Process -Id $pid -Force" >/dev/null 2>&1; then
    # 受控环境（沙箱/加固策略）可能拦 tasklist/taskkill，PowerShell 兜底；失败不再假报成功
    msg "backend: 已停止 (winpid $pid，taskkill 被环境拦截，PowerShell 兜底)"
  else
    msg "backend: 停止失败 (winpid $pid)，请人工 taskkill //PID $pid //T //F"
  fi
}

stop_pid_file() { # pidfile 名称 端口
  local f="$RUN/$1" pid wp
  if [ ! -f "$f" ]; then msg "$2: 无 pid 文件（可能未由 start.sh 启动）"; return; fi
  pid=$(tr -dc '0-9' < "$f" 2>/dev/null)
  rm -f "$f"
  if [ -z "$pid" ]; then msg "$2: 空 pid 文件，跳过"; return; fi
  if ! win_alive "$pid"; then
    # 兼容旧版记录的 MSYS 包装 pid：映射到 Windows pid 再试一次
    wp=$(winpid_of "$pid")
    if [ -n "${wp:-}" ] && win_alive "$wp"; then pid="$wp"; else
      msg "$2: pid $pid 已不存在"; return
    fi
  fi
  if [ "$(port_owner "$3")" != "$pid" ]; then
    msg "$2: pid $pid 存活但未持有 :$3（陈旧记录，不误杀；见残留检测）"; return
  fi
  # 优雅先行（docs/01 §6 四步；审计 P1-8）：不带 //F 先发温和终止（Linux=SIGTERM；
  # Windows 控制台进程常拒收 → 轮询窗口后强杀兜底）。应用侧 lifespan 四步
  # （SSE 广播 → cancel futures → 关连接池）在任何优雅信号送达时完整生效。
  timeout 5 taskkill //PID "$pid" >/dev/null 2>&1
  for _ in $(seq 1 8); do
    if ! win_alive "$pid"; then
      msg "$2: 已优雅停止 (winpid $pid，属主校验通过)"
      return
    fi
    sleep 1
  done
  if timeout 10 taskkill //PID "$pid" //T //F >/dev/null 2>&1; then
    msg "$2: 已停止 (winpid $pid，优雅窗口超时，强杀兜底)"
  elif timeout 15 powershell -NoProfile -Command "Stop-Process -Id $pid -Force" >/dev/null 2>&1; then
    # 受控环境（沙箱/加固策略）可能拦 tasklist/taskkill，PowerShell 兜底；失败不再假报成功
    msg "$2: 已停止 (winpid $pid，taskkill 被环境拦截，PowerShell 兜底)"
  else
    msg "$2: 停止失败 (winpid $pid)，请人工 taskkill //PID $pid //T //F"
  fi
}

stop_backend() { # backend 专属（NIGHT-013）：优雅先行 → 超时强停兜底。
  # pid 解析/属主校验口径与 stop_pid_file 一致；--force 时直接委托 stop_pid_file（旧行为原样）。
  local f="$RUN/backend.pid" pid wp
  if [ ! -f "$f" ]; then msg "backend: 无 pid 文件（可能未由 start.sh 启动）"; return; fi
  pid=$(tr -dc '0-9' < "$f" 2>/dev/null)
  rm -f "$f"
  if [ -z "$pid" ]; then msg "backend: 空 pid 文件，跳过"; return; fi
  if ! win_alive "$pid"; then
    wp=$(winpid_of "$pid")
    if [ -n "${wp:-}" ] && win_alive "$wp"; then pid="$wp"; else
      msg "backend: pid $pid 已不存在"; return
    fi
  fi
  if [ "$(port_owner 8000)" != "$pid" ]; then
    msg "backend: pid $pid 存活但未持有 :8000（陈旧记录，不误杀；见残留检测）"; return
  fi
  if grace_ctrl_break "$pid"; then
    msg "backend: 已定向投递 CTRL_BREAK (winpid $pid，属主校验通过)——等待优雅停机（上限 ${GRACE_TIMEOUT_S}s；广播入口 BidqaServer.shutdown → SSE 5031）"
    for _ in $(seq 1 "$GRACE_TIMEOUT_S"); do
      if ! win_alive "$pid" || ! port_up 8000; then
        msg "backend: 已优雅停止 (winpid $pid)——SSE 5031 广播已在停机入口发出"
        return
      fi
      sleep 1
    done
    msg "backend: 优雅窗口 ${GRACE_TIMEOUT_S}s 超时（事件未生效），转强停兜底"
  else
    msg "backend: CTRL_BREAK 不可投递（本环境限制；Git Bash PTY 下属预期），转强停兜底"
  fi
  force_stop_pid "$pid"
}

# Neo4j 残留 JVM 清扫（NIGHT-028）：CTRL_BREAK 广播下 neo4j-admin 启动器 JVM 仅打
# 线程转储幸存；pid 文件只记监听 JVM，启动器/残片按可执行路径匹配（仅 services 下
# jdk*/bin/java.exe，不误伤其它 java）逐个兜底强停，每步硬超时。
sweep_neo4j_jvm() {
  local p n=0
  for p in $(ps -W | awk 'tolower($NF) ~ /services.*jdk.*bin.*java\.exe/ {print $4}'); do
    if timeout 10 powershell -NoProfile -Command "Stop-Process -Id $p -Force" >/dev/null 2>&1; then
      msg "Neo4j: 已清理残留 JVM (winpid $p)"; n=$((n + 1))
    fi
  done
  [ "$n" -eq 0 ] && msg "Neo4j: 无残留 JVM"
}

# neo4j.bat 包装 cmd 清扫（NIGHT-028）：该 cmd 收到广播后弹「终止批处理操作吗(Y/N)?」
# 永久等待输入；按「cmd.exe 且命令行含 neo4j」精确匹配兜底强停。沙箱/加固环境枚举
# 命令行可能被拒（诚实跳过并提示），用户终端手动按 Y 亦可。
sweep_neo4j_wrapper() {
  if timeout 12 powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='cmd.exe'\" | Where-Object { \$_.CommandLine -match 'neo4j' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force }" >/dev/null 2>&1; then
    msg "Neo4j: 已清理 neo4j.bat 包装 cmd（Y/N 提示僵尸）"
  else
    msg "Neo4j: 跳过包装 cmd 清理（本环境无法枚举命令行；控制台若见「终止批处理操作吗(Y/N)?」按 Y 即可）"
  fi
}

stop_pg() {
  if port_up 5432; then
    timeout 20 "$ROOT/services/postgresql/bin/pg_ctl.exe" stop -D "$ROOT/data/db/postgres" -m fast >/dev/null 2>&1
    msg "PostgreSQL: 已停止"
  else
    msg "PostgreSQL: 未在运行"
  fi
}

run_inner() {
  if [ "$FORCE_STOP" -eq 1 ]; then
    msg "backend: --force 指定——跳过优雅投递，直接按旧强停路径"
    stop_pid_file backend.pid backend 8000   # --force：旧强停路径原样（温和 taskkill → 8s → 强杀）
  else
    stop_backend                              # 默认：优雅先行 → 超时强停兜底（NIGHT-013）
  fi
  stop_pid_file neo4j.pid "Neo4j" 7474
  sweep_neo4j_jvm
  sweep_neo4j_wrapper
  stop_pid_file qdrant.pid Qdrant 6333
  stop_pg
  sleep 2
  msg "── 残留检测 ──"
  leftover=0
  for p in 8000 7474 7687 6333 5432; do
    if port_up "$p"; then msg "  警告：端口 $p 仍有监听（属主 pid $(port_owner "$p")，可 taskkill //PID 处理）"; leftover=1; fi
  done
  [ "$leftover" -eq 0 ] && msg "  无残留，端口已全部释放"
}

if [ "$INNER" -eq 1 ]; then
  msg "WORKER-PID $(winpid_of "$$")"   # waiter 据此检测 worker 是否已消失（提前返回，不死等）
  run_inner
  msg "STOP-DONE rc=0"
  exit 0
fi

# ─────────────────────────────────────────────────────────────
# 壳进程（用户控制台）：预检 → 隐藏控制台 worker → python waiter 有界跟踪。
# 壳进程自身绝不下场投递：真控制台同控制台拓扑下 CTRL_BREAK 广播会击杀 bash
# （NIGHT-028 实证），因此投递与四段停机全部由隔离 worker 完成。
# ─────────────────────────────────────────────────────────────
_ts=$(date +%Y%m%d-%H%M%S)
LOG="$RUN/stop-$_ts.log"
ERRW="$RUN/stop-$_ts.spawn.err"
LOGW=$(cygpath -w "$LOG" 2>/dev/null || echo "$LOG")
: > "$LOG"

PY=""
for _c in "$ROOT/backend/.venv/Scripts/python.exe" "$ROOT/backend/.venv/bin/python"; do
  [ -f "$_c" ] && PY="$_c" && break
done

BASHW=""
[ -n "${BASH:-}" ] && BASHW=$(cygpath -w "$BASH" 2>/dev/null)
[ -z "$BASHW" ] && BASHW='C:\Program Files\Git\usr\bin\bash.exe'
INNERW=$(cygpath -w "$0" 2>/dev/null || echo "$0")
INNERARGS="--inner"
[ "$FORCE_STOP" -eq 1 ] && INNERARGS="--inner --force"

echo "backend 优雅停机在隔离 worker 中执行（NIGHT-028：真控制台下 CTRL_BREAK 广播会波及脚本自身）"
echo "worker 日志：$LOG（若本命令未等待到底即返回，属广播波及可见进程的正常现象；"
echo "完成判定以 worker 日志末尾 STOP-DONE 与 bash deploy/status.sh 为准）"
if ! powershell -NoProfile -Command "Start-Process -FilePath '$BASHW' -ArgumentList '\"$INNERW\" $INNERARGS' -WindowStyle Hidden -RedirectStandardOutput '$LOGW' -RedirectStandardError '$ERRW'" ; then
  echo "worker 启动失败：请在 Git Bash 中运行本脚本，或按 data/run/stop-*.log 排障" >&2
  exit 1
fi

if [ -n "$PY" ]; then
  # waiter（python）在用户控制台内幸存 CTRL_BREAK 广播（微缩复现实证），负责把 worker
  # 进度实时回显给用户并给出最终判定；上限 WAIT_BOUND_S，绝不无限等。
  exec "$PY" -c '
import sys, time, signal, subprocess
try:
    signal.signal(signal.SIGBREAK, signal.SIG_IGN)  # CTRL_BREAK 广播幸存尝试（NIGHT-028）
except Exception:
    pass
log, bound = sys.argv[1], float(sys.argv[2])
f = open(log, "r", encoding="utf-8", errors="replace")
buf = ""
seen = False
end = time.time() + bound
wpid = None
tick = 0
miss = 0
while time.time() < end:
    c = f.read()
    if c:
        sys.stdout.write(c)
        sys.stdout.flush()
        buf += c
        if "STOP-DONE" in buf:
            seen = True
            break
        if wpid is None and "WORKER-PID" in buf:
            toks = buf.split("WORKER-PID", 1)[1].split()
            wpid = toks[0] if toks and toks[0].isdigit() else None
    else:
        time.sleep(0.4)
        tick += 1
        if wpid and tick % 5 == 0:  # ~2s 一次：worker 进程已消失且无 STOP-DONE → 提前返回
            try:
                out = subprocess.run(["ps", "-W"], capture_output=True, text=True, timeout=10).stdout
            except Exception:
                continue
            miss = miss + 1 if (" " + wpid + " ") not in out.replace("\t", " ") else 0
            if miss >= 3:
                print()
                print(f"worker 进程 (winpid {wpid}) 已退出但日志未见 STOP-DONE——异常终止，提前返回；日志：{log}")
                sys.exit(1)
if seen:
    sys.exit(0)
print()
print(f"等待超时（{bound:.0f}s 未见 STOP-DONE）——worker 仍在后台执行，日志：{log}")
print("可用 bash deploy/status.sh 复核；必要时按日志内提示人工 taskkill //PID")
sys.exit(124)
' "$(cygpath -w "$LOG")" "$WAIT_BOUND_S"
fi

# 无 python（罕见）：退化为异步模式，worker 仍在后台有界完成。
echo "未找到 backend/.venv python——worker 已在后台执行，完成后查看日志并运行 bash deploy/status.sh 验证"
exit 0
