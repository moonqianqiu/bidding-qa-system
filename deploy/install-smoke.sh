#!/usr/bin/env bash
# install-services.sh 全新安装自测（审计 P0-8 收口）：沙箱内用假 zip 模拟 1-4 节，
# 钉死「守卫文件名 = 解压文件名 = 落盘目录」一致性（曾守卫 jre21.zip 却解压
# jdk21.zip → 全新部署必挂）。5/6 节（initdb/Neo4j 密码）以预置标记位跳过。
# 免费离线可跑：假 zip 由 backend venv python 的 zipfile 生成，不需要真下载产物。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
fail() { echo "SMOKE FAIL: $1" >&2; exit 1; }

PY="${BIDQA_PY:-backend/.venv/Scripts/python.exe}"
[ -e "$PY" ] || PY="python"  # venv 不在时兜底系统 python

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

# 预置 5/6 节的幂等标记（数据目录已初始化 + Neo4j 密码已设），只测 1-4 节
mkdir -p "$SANDBOX/data/db/postgres" "$SANDBOX/services/neo4j/conf"
touch "$SANDBOX/data/db/postgres/PG_VERSION" "$SANDBOX/data/db/.neo4j_pw_set"
touch "$SANDBOX/services/neo4j/conf/neo4j.conf"

"$PY" - "$SANDBOX" <<'EOF' || fail "假 zip 生成失败"
import sys, zipfile
from pathlib import Path
root = Path(sys.argv[1]) / "services" / "downloads"
root.mkdir(parents=True, exist_ok=True)
def add(zf, *names):
    for n in names:
        zf.writestr(n, "fake")
with zipfile.ZipFile(root / "qdrant.zip", "w") as z:
    add(z, "qdrant.exe")
with zipfile.ZipFile(root / "jre21.zip", "w") as z:
    add(z, "jdk-21.0.5+11/bin/java.exe", "jdk-21.0.5+11/release")
with zipfile.ZipFile(root / "postgresql-17-binaries.zip", "w") as z:
    add(z, "pgsql/bin/pg_ctl.exe", "pgsql/bin/initdb.exe")
with zipfile.ZipFile(root / "neo4j.zip", "w") as z:
    add(z, "neo4j-community-2025.01.0/bin/neo4j.bat",
          "neo4j-community-2025.01.0/conf/neo4j.conf")
EOF

BIDQA_INSTALL_ROOT="$SANDBOX" bash deploy/install-services.sh \
  || fail "install-services.sh 在沙箱安装失败"

for f in services/qdrant/qdrant.exe \
         services/jdk21/bin/java.exe \
         services/postgresql/bin/pg_ctl.exe \
         services/neo4j/bin/neo4j.bat; do
  [ -f "$SANDBOX/$f" ] || fail "安装后缺 $f（守卫名/解压名/落盘目录不一致？）"
done
[ ! -d "$SANDBOX/services/jdk_x" ] || fail "临时目录 services/jdk_x 未清理"
echo "install-smoke PASS（沙箱 $SANDBOX 已清理）"
