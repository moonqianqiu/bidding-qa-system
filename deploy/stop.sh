#!/usr/bin/env bash
# 一键停止：backend → Neo4j → Qdrant → PostgreSQL 逆序停止。
# pid 文件统一记录「真实监听进程」的 Windows pid（start.sh 用 netstat -ano 归属取得）；
# 停止前做属主校验（pid 必须仍持有对应端口才杀）——旧版记 uv/包装进程导致「按 pid 杀不掉，
# 旧代码继续跑」（2026-11-02 实测事故），本版杜绝，且任何情形不误杀非本脚本进程。
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN="$ROOT/data/run"

port_up() { netstat -an | grep LISTENING | grep -qE "[:.]$1[[:space:]]"; }
port_owner() { netstat -ano | grep LISTENING | grep -E "[:.]$1[[:space:]]" | awk '{print $NF; exit}'; }
# 存活检测走 ps -W（无特权，沙箱/受控环境可用）；tasklist 会报「用户名或密码不正确」
win_alive() { ps -W | awk -v p="$1" 'BEGIN{f=0} $4==p{f=1} END{exit f?0:1}'; }
winpid_of() { ps -W | awk -v p="$1" '$1==p{print $4; exit}'; }

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

stop_pid_file backend.pid backend 8000
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
