#!/usr/bin/env bash
# 绿色服务安装（M0-4）：解压 services/downloads/*.zip → services/<name>/，并初始化数据目录。
# 幂等：已安装的组件自动跳过。先运行 download-services.sh。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
DL="services/downloads"
fail() { echo "错误：$1" >&2; exit 1; }

# ── 1) Qdrant ──
if [ ! -f services/qdrant/qdrant.exe ]; then
  [ -s "$DL/qdrant.zip" ] || fail "services/downloads/qdrant.zip 不存在，先运行 download-services.sh"
  echo "== 安装 Qdrant =="
  rm -rf services/qdrant; mkdir -p services/qdrant
  unzip -q "$DL/qdrant.zip" -d services/qdrant
else
  echo "Qdrant 已安装"
fi

# ── 2) Temurin JRE 21（Neo4j 运行时；Neo4j 只需 JRE）──
if [ ! -f services/jdk/bin/java.exe ]; then
  [ -s "$DL/jre21.zip" ] || fail "services/downloads/jre21.zip 不存在"
  echo "== 安装 JRE 21 =="
  rm -rf services/jdk_x; mkdir -p services/jdk_x
  unzip -q "$DL/jdk21.zip" -d services/jdk_x
  JDK_SRC=$(find services/jdk_x -maxdepth 1 -type d -name "jdk-*" | head -1)
  [ -n "$JDK_SRC" ] || fail "jdk21.zip 结构异常"
  mv "$JDK_SRC" services/jdk && rm -rf services/jdk_x
else
  echo "JDK 已安装"
fi

# ── 3) PostgreSQL（EDB binaries）──
if [ ! -f services/postgresql/bin/pg_ctl.exe ]; then
  ls "$DL"/postgresql-*-binaries.zip >/dev/null 2>&1 || fail "postgresql-*-binaries.zip 不存在"
  echo "== 安装 PostgreSQL =="
  rm -rf services/pg_x; mkdir -p services/pg_x
  unzip -q "$DL"/postgresql-*-binaries.zip -d services/pg_x
  [ -d services/pg_x/pgsql ] || fail "PostgreSQL zip 结构异常（预期顶层 pgsql/）"
  rm -rf services/postgresql
  mv services/pg_x/pgsql services/postgresql && rm -rf services/pg_x
else
  echo "PostgreSQL 已安装"
fi

# ── 4) Neo4j community ──
if [ ! -f services/neo4j/bin/neo4j.bat ]; then
  [ -s "$DL/neo4j.zip" ] || fail "services/downloads/neo4j.zip 不存在"
  echo "== 安装 Neo4j =="
  rm -rf services/nj_x; mkdir -p services/nj_x
  unzip -q "$DL/neo4j.zip" -d services/nj_x
  NJ_SRC=$(find services/nj_x -maxdepth 1 -type d -name "neo4j-community-*" | head -1)
  [ -n "$NJ_SRC" ] || fail "neo4j.zip 结构异常"
  rm -rf services/neo4j
  mv "$NJ_SRC" services/neo4j && rm -rf services/nj_x
else
  echo "Neo4j 已安装"
fi

# ── 5) PostgreSQL 数据目录（initdb 一次性）──
if [ ! -f data/db/postgres/PG_VERSION ]; then
  echo "== initdb（数据目录 data/db/postgres）=="
  services/postgresql/bin/initdb.exe -D data/db/postgres -U postgres -A trust -E UTF8 --locale=C
else
  echo "PostgreSQL 数据目录已初始化"
fi

# ── 6) Neo4j：仅监听本机 + 初始密码（一次性）──
if ! grep -q "server.default_listen_address=127.0.0.1" services/neo4j/conf/neo4j.conf 2>/dev/null; then
  printf "\nserver.default_listen_address=127.0.0.1\n" >> services/neo4j/conf/neo4j.conf
fi
if [ ! -f data/db/.neo4j_pw_set ]; then
  echo "== Neo4j 初始密码 =="
  # MSYS 路径不作环境变量自动转换：JAVA_HOME 必须显式转 Windows 风格
  JAVA_HOME="$(cygpath -w "$(cd services/jdk && pwd)")" services/neo4j/bin/neo4j-admin.bat dbms set-initial-password bidqa_dev_2026 \
    || fail "Neo4j 初始密码设置失败（详见上方输出）"
  touch data/db/.neo4j_pw_set
  echo "Neo4j 初始密码已设为 bidqa_dev_2026（开发用，改密后请同步 backend/.env 的 NEO4J_PASSWORD）"
else
  echo "Neo4j 密码已初始化"
fi

echo "── 安装完成 ──"
ls services/
