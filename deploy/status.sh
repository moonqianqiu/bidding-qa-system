#!/usr/bin/env bash
# 状态总览：端口监听 + pid 属主校验 + HTTP 健康。
# pid 文件 = 真实监听者的 Windows pid（start.sh 由 netstat -ano 归属取得）；
# 「存活」还须与端口属主交叉核对，不一致即提示陈旧记录（旧代码仍在跑的典型信号）。
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN="$ROOT/data/run"

port_up() { netstat -an | grep LISTENING | grep -qE "[:.]$1[[:space:]]"; }
port_owner() { netstat -ano | grep LISTENING | grep -E "[:.]$1[[:space:]]" | awk '{print $NF; exit}'; }
# 存活检测走 ps -W（无特权，沙箱/受控环境可用）；tasklist 会报「用户名或密码不正确」
win_alive() { ps -W | awk -v p="$1" 'BEGIN{f=0} $4==p{f=1} END{exit f?0:1}'; }
winpid_of() { ps -W | awk -v p="$1" '$1==p{print $4; exit}'; }

check() { # 端口 名称 pidfile 健康URL
  local port="$1" name="$2" pidfile="$3" url="$4"
  local state pidinfo="-" http="-"

  if port_up "$port"; then state="监听 :$port"; else state="未监听 :$port"; fi

  if [ -n "$pidfile" ] && [ -f "$RUN/$pidfile" ]; then
    local pid wp
    pid=$(tr -dc '0-9' < "$RUN/$pidfile" 2>/dev/null)
    if [ -n "$pid" ] && win_alive "$pid"; then
      if [ "$(port_owner "$port")" = "$pid" ]; then pidinfo="pid $pid 属主一致"
      else pidinfo="pid $pid 存活但未持有 :$port（陈旧!）"; fi
    else
      wp=$(winpid_of "$pid")
      if [ -n "${wp:-}" ] && win_alive "$wp"; then pidinfo="pid $pid（旧式）→$wp 存活"
      else pidinfo="pid ${pid:-?} 已死（残留!）"; fi
    fi
  elif [ -n "$pidfile" ]; then
    pidinfo="无 pid 文件"
  fi

  if [ -n "$url" ] && port_up "$port"; then
    http=$(curl -s -o /dev/null -w "%{http_code}" --noproxy '*' --max-time 3 "$url" 2>/dev/null || echo "ERR")
  fi

  printf "%-11s %-13s %-36s http:%s\n" "$name" "$state" "$pidinfo" "$http"
}

echo "── bidqa 服务状态 ──"
check 5432 PostgreSQL "" ""
check 6333 Qdrant qdrant.pid "http://127.0.0.1:6333/healthz"
check 7474 Neo4j neo4j.pid "http://127.0.0.1:7474"
check 8000 backend backend.pid "http://127.0.0.1:8000/api/health"
