#!/usr/bin/env bash
# 一键启动（M0-4 基础版）：PostgreSQL → Qdrant → Neo4j → backend(uvicorn workers=1)。
# 幂等：已在运行的服务自动跳过。首次运行自动从模板创建 backend/.env。
# 注意：workers=1 是硬性约定（docs/01 §6，进程内状态假定单进程）。
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN="$ROOT/data/run"
mkdir -p "$RUN"

# Windows 踩坑：管理员账户的令牌会被 initdb/postgres 自我降权（CreateRestrictedToken），
# 恰好剥掉 Global\ 共享内存所需的 SeCreateGlobalPrivilege（错误 1314）。
# 账户已授「创建全局对象」权利（deploy/grant-elevate.cmd），PG_RESTRICT_EXEC 跳过自我降权。
export PG_RESTRICT_EXEC=1

port_up() { netstat -an | grep LISTENING | grep -qE "[:.]$1[[:space:]]"; }
wait_port() { # port 名称 超时秒
  local port="$1" name="$2" timeout="${3:-90}" waited=0
  while ! port_up "$port"; do
    waited=$((waited + 2))
    if [ "$waited" -ge "$timeout" ]; then
      echo "超时：$name 未在 ${timeout}s 内就绪（端口 $port），日志见 data/run/"
      return 1
    fi
    sleep 2
  done
  echo "  就绪: $name (:$port)"
}
winpid_of() { ps -W | awk -v p="$1" '$1==p{print $4; exit}'; }
kill_tree() {
  local wp; wp=$(winpid_of "$1")
  if [ -n "${wp:-}" ]; then taskkill //PID "$wp" //T //F >/dev/null 2>&1; else kill -9 "$1" 2>/dev/null; fi
}
# 端口属主（Windows pid，netstat -ano 最后一列）——pid 文件的唯一可信来源
port_owner() { netstat -ano | grep LISTENING | grep -E "[:.]$1[[:space:]]" | awk '{print $NF; exit}'; }

# 首次运行：占位 .env（核心槽位齐全可启动，聊天能力需填真实 key）
if [ ! -f "$ROOT/backend/.env" ]; then
  cp "$ROOT/backend/.env.example" "$ROOT/backend/.env"
  echo "已从模板创建 backend/.env（占位配置；聊天/嵌入等能力需填真实 API key）"
fi

echo "== 1/4 PostgreSQL =="
if port_up 5432; then
  echo "  已在运行 (:5432)"
elif [ ! -f "$ROOT/data/db/postgres/PG_VERSION" ]; then
  echo "  跳过：数据目录未初始化（先运行一次 deploy/grant-elevate.cmd 并重新登录，再执行 deploy/install-services.sh）"
else
  "$ROOT/services/postgresql/bin/pg_ctl.exe" start -D "$ROOT/data/db/postgres" -l "$RUN/postgres.log" -o "-p 5432" >/dev/null
  wait_port 5432 PostgreSQL 60 || echo "  警告：PostgreSQL 未就绪，对应能力将降级（docs/01 §5）"
fi

echo "== 2/4 Qdrant =="
if port_up 6333; then
  echo "  已在运行 (:6333，属主 pid $(port_owner 6333))"
else
  mkdir -p "$ROOT/data/db/qdrant"
  # Qdrant 1.20+ 无 --storage-dir CLI 参数：用环境变量覆盖配置（QDRANT__<SECTION>__<KEY>）
  (cd "$ROOT/services/qdrant" && QDRANT__SERVICE__HOST=127.0.0.1 QDRANT__STORAGE__STORAGE_PATH="$(cygpath -w "$ROOT/data/db/qdrant")" ./qdrant.exe > "$RUN/qdrant.log" 2>&1 &)
  wait_port 6333 Qdrant 60 || exit 1
  port_owner 6333 > "$RUN/qdrant.pid"   # 记录真实监听者（winpid）；旧 $! 是包装进程，杀不准
fi

echo "== 3/4 Neo4j =="
if port_up 7474; then
  echo "  已在运行 (:7474)"
elif [ ! -f "$ROOT/services/neo4j/bin/neo4j.bat" ]; then
  echo "  跳过：Neo4j 未安装（deploy/download-services.sh + install-services.sh）"
else
  # Neo4j 5.26 需要 Java 17/21：优先 jdk21（微软版），回退 jdk17full / jdk
  NEO4J_JRE=""
  for d in jdk21 jdk17full jdk; do
    [ -f "$ROOT/services/$d/bin/java.exe" ] && { NEO4J_JRE="$ROOT/services/$d"; break; }
  done
  [ -z "$NEO4J_JRE" ] && { echo "  错误：找不到 Java 17/21（services/jdk21 或 jdk17full）"; exit 1; }
  # 全局临时目录（多会话共用）里 JNA 释放 jnidispatch.dll 与 browser 静态资源解压
  # 会因残留/属主不匹配报「拒绝访问」→ 给 JVM 独立 tmpdir 绕开（2026-11-10 实证）。
  # cygpath -m 出正斜杠：-D 参数里的反斜杠会被 JVM 当转义。
  NEO_TMP_DIR="$ROOT/services/neo4j/run/neo-tmp"
  mkdir -p "$NEO_TMP_DIR"
  NEO_TMP="$(cygpath -m "$NEO_TMP_DIR")"
  # MSYS 路径不作参数自动转换：JAVA_HOME 必须显式转 Windows 风格
  (cd "$ROOT/services/neo4j" && \
     export JAVA_HOME="$(cygpath -w "$NEO4J_JRE")" && \
     export JAVA_TOOL_OPTIONS="-Djava.io.tmpdir=$NEO_TMP -Djna.tmpdir=$NEO_TMP" && \
     ./bin/neo4j.bat console > "$RUN/neo4j.log" 2>&1 &)
  if wait_port 7474 Neo4j 180; then
    port_owner 7474 > "$RUN/neo4j.pid"   # 真实监听者=java.exe（winpid）
  else
    echo "  警告：Neo4j 未就绪，图谱能力降级"
  fi
fi

echo "== 4/4 backend =="
if port_up 8000; then
  echo "  已在运行 (:8000，属主 pid $(port_owner 8000))——要跑新代码先 bash deploy/stop.sh"
else
  (cd "$ROOT/backend" && uv run bidqa serve --host 127.0.0.1 --port 8000 > "$RUN/backend.log" 2>&1 &)
  if wait_port 8000 backend 60; then
    port_owner 8000 > "$RUN/backend.pid"   # 真实监听者=python 子进程（winpid）；旧 $! 是 uv 包装，曾致「改了代码没效果」
    echo "  后端就绪，监听 pid $(cat "$RUN/backend.pid") 已记录"
  else
    echo "  错误：backend 未就绪，日志见 data/run/backend.log"
    exit 1
  fi
fi

echo "── 启动完成（workers=1）──"
"$ROOT/deploy/status.sh"
