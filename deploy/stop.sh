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
# Git Bash PTY 下外部优雅信号不可投递（NIGHT-002/011 实证），此时自动走兜底属预期；
# 真控制台（原生 cmd/PowerShell）下运行本脚本时可送达。数据服务段行为不变。
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
FORCE_STOP=0

usage() {
  cat <<'USAGEEOF'
用法：bash deploy/stop.sh [--force]
  无参数   backend 优雅先行：定向投递 CTRL_BREAK 触发服务端 SSE 5031 广播，最多等 30s，
           超时或投递失败转强停兜底；Neo4j/Qdrant/PostgreSQL 照常停止。
           注：Git Bash PTY 下优雅信号不可投递（自动走兜底，属预期）；真控制台可送达。
  --force  backend 跳过优雅尝试，直接按旧强停路径停止；数据服务段不受影响。
  --help   显示本说明。
USAGEEOF
}

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

# 定向投递 CTRL_BREAK（NIGHT-013 优雅先行）：PowerShell P/Invoke 附加目标进程控制台后，
# 以「目标 pid = 进程组 id」定向生成 CTRL_BREAK——仅目标组收到，不误伤同控制台其它进程
# （对比 group=0 广播会殃及同控制台 shell，禁用）。rc0=事件已生成（是否生效由后续存活
# 轮询观察）；rc2=AttachConsole 失败（目标无控制台/已退出）；rc3=生成失败（目标非进程组
# 首进程或环境拦截）；rc124=死锁防御超时（NIGHT-002 第 3 轮实测过同步 Generate 死锁形态）。
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
    echo "backend: 已停止 (winpid $pid，兜底前复查已退出)"
    return
  fi
  if taskkill //PID "$pid" //T //F >/dev/null 2>&1; then
    echo "backend: 已停止 (winpid $pid，优雅窗口超时，强杀兜底)"
  elif powershell -NoProfile -Command "Stop-Process -Id $pid -Force" >/dev/null 2>&1; then
    # 受控环境（沙箱/加固策略）可能拦 tasklist/taskkill，PowerShell 兜底；失败不再假报成功
    echo "backend: 已停止 (winpid $pid，taskkill 被环境拦截，PowerShell 兜底)"
  else
    echo "backend: 停止失败 (winpid $pid)，请人工 taskkill //PID $pid //T //F"
  fi
}

stop_pid_file() { # pidfile 名称 端口
  local f="$RUN/$1" pid wp
  if [ ! -f "$f" ]; then echo "$2: 无 pid 文件（可能未由 start.sh 启动）"; return; fi
  pid=$(tr -dc '0-9' < "$f" 2>/dev/null)
  rm -f "$f"
  if [ -z "$pid" ]; then echo "$2: 空 pid 文件，跳过"; return; fi
  if ! win_alive "$pid"; then
    # 兼容旧版记录的 MSYS 包装 pid：映射到 Windows pid 再试一次
    wp=$(winpid_of "$pid")
    if [ -n "${wp:-}" ] && win_alive "$wp"; then pid="$wp"; else
      echo "$2: pid $pid 已不存在"; return
    fi
  fi
  if [ "$(port_owner "$3")" != "$pid" ]; then
    echo "$2: pid $pid 存活但未持有 :$3（陈旧记录，不误杀；见残留检测）"; return
  fi
  # 优雅先行（docs/01 §6 四步；审计 P1-8）：不带 //F 先发温和终止（Linux=SIGTERM；
  # Windows 控制台进程常拒收 → 轮询窗口后强杀兜底）。应用侧 lifespan 四步
  # （SSE 广播 → cancel futures → 关连接池）在任何优雅信号送达时完整生效。
  taskkill //PID "$pid" >/dev/null 2>&1
  for _ in $(seq 1 8); do
    if ! win_alive "$pid"; then
      echo "$2: 已优雅停止 (winpid $pid，属主校验通过)"
      return
    fi
    sleep 1
  done
  if taskkill //PID "$pid" //T //F >/dev/null 2>&1; then
    echo "$2: 已停止 (winpid $pid，优雅窗口超时，强杀兜底)"
  elif powershell -NoProfile -Command "Stop-Process -Id $pid -Force" >/dev/null 2>&1; then
    # 受控环境（沙箱/加固策略）可能拦 tasklist/taskkill，PowerShell 兜底；失败不再假报成功
    echo "$2: 已停止 (winpid $pid，taskkill 被环境拦截，PowerShell 兜底)"
  else
    echo "$2: 停止失败 (winpid $pid)，请人工 taskkill //PID $pid //T //F"
  fi
}

stop_backend() { # backend 专属（NIGHT-013）：优雅先行 → 超时强停兜底。
  # pid 解析/属主校验口径与 stop_pid_file 一致；--force 时直接委托 stop_pid_file（旧行为原样）。
  local f="$RUN/backend.pid" pid wp
  if [ ! -f "$f" ]; then echo "backend: 无 pid 文件（可能未由 start.sh 启动）"; return; fi
  pid=$(tr -dc '0-9' < "$f" 2>/dev/null)
  rm -f "$f"
  if [ -z "$pid" ]; then echo "backend: 空 pid 文件，跳过"; return; fi
  if ! win_alive "$pid"; then
    wp=$(winpid_of "$pid")
    if [ -n "${wp:-}" ] && win_alive "$wp"; then pid="$wp"; else
      echo "backend: pid $pid 已不存在"; return
    fi
  fi
  if [ "$(port_owner 8000)" != "$pid" ]; then
    echo "backend: pid $pid 存活但未持有 :8000（陈旧记录，不误杀；见残留检测）"; return
  fi
  if grace_ctrl_break "$pid"; then
    echo "backend: 已定向投递 CTRL_BREAK (winpid $pid，属主校验通过)——等待优雅停机（上限 ${GRACE_TIMEOUT_S}s；广播入口 BidqaServer.shutdown → SSE 5031）"
    for _ in $(seq 1 "$GRACE_TIMEOUT_S"); do
      if ! win_alive "$pid"; then
        echo "backend: 已优雅停止 (winpid $pid)——SSE 5031 广播已在停机入口发出"
        return
      fi
      sleep 1
    done
    echo "backend: 优雅窗口 ${GRACE_TIMEOUT_S}s 超时（事件未生效），转强停兜底"
  else
    echo "backend: CTRL_BREAK 不可投递（本环境限制；Git Bash PTY 下属预期），转强停兜底"
  fi
  force_stop_pid "$pid"
}

if [ "$FORCE_STOP" -eq 1 ]; then
  stop_pid_file backend.pid backend 8000   # --force：旧强停路径原样（温和 taskkill → 8s → 强杀）
else
  stop_backend                              # 默认：优雅先行 → 超时强停兜底（NIGHT-013）
fi
stop_pid_file neo4j.pid "Neo4j" 7474
stop_pid_file qdrant.pid Qdrant 6333

if port_up 5432; then
  "$ROOT/services/postgresql/bin/pg_ctl.exe" stop -D "$ROOT/data/db/postgres" -m fast >/dev/null 2>&1
  echo "PostgreSQL: 已停止"
else
  echo "PostgreSQL: 未在运行"
fi

sleep 2
echo "── 残留检测 ──"
leftover=0
for p in 8000 7474 7687 6333 5432; do
  if port_up "$p"; then echo "  警告：端口 $p 仍有监听（属主 pid $(port_owner "$p")，可 taskkill //PID 处理）"; leftover=1; fi
done
[ "$leftover" -eq 0 ] && echo "  无残留，端口已全部释放"
