#!/usr/bin/env bash
# 绿色服务下载脚本（M0-4）——并行分段 + 多源轮转版
# 背景（实测踩坑）：
# ①本机沙箱代理(127.0.0.1:9909)对 GitHub/EDB 等域名 TLS 握手失败（pypi/npmjs 正常）→ 强制直连
# ②单连接限速严重（数十~数百 KB/s，且随时间波动）→ 每文件 12 段并行 + 段级重试（源轮转）
# ③Neo4j 只需 JRE：用 Adoptium JRE 21（比 JDK 省 ~150MB）
# 产物：services/downloads/*.zip，由 install-services.sh 解压。重跑幂等（完整 zip 跳过）。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
DL="services/downloads"
mkdir -p "$DL"

# pdownload <输出文件名> <段数> <url1> [url2...] —— 同一文件的多镜像，段内按轮转选源
# 环境变量 PDL_PROXY 非空时走该代理（用于地域封锁源，如 dist.neo4j.org 只对大陆出口 403）
pdownload() {
  local out="$DL/$1"; shift
  local segs="$1"; shift
  local urls=("$@")
  local CURL_NET=(--noproxy '*')
  [ -n "${PDL_PROXY:-}" ] && CURL_NET=(-x "$PDL_PROXY")
  if [ -s "$out" ]; then echo "已存在，跳过: $(basename "$out")"; return 0; fi

  # 1) 取第一个可用源的 content-length（HEAD 被拒时用 0-0 段 GET 的 content-range 回退）
  local total="" u cr
  for u in "${urls[@]}"; do
    total=$(curl "${CURL_NET[@]}" -sIL --connect-timeout 10 --max-time 30 "$u" | grep -i '^content-length' | tail -1 | tr -dc 0-9)
    [ -n "$total" ] && break
  done
  if [ -z "$total" ]; then
    for u in "${urls[@]}"; do
      cr=$(curl "${CURL_NET[@]}" -sL -D - -o /dev/null -r 0-0 --connect-timeout 10 --max-time 30 "$u" | grep -i '^content-range' | tail -1)
      total=$(printf '%s' "$cr" | sed -E 's#.*/##' | tr -dc 0-9)
      [ -n "$total" ] && break
    done
  fi
  if [ -z "$total" ]; then echo "错误：所有源均不可用: $(basename "$out")"; return 1; fi

  # 2) 分段并行，段级重试 3 次（重试时轮转到下一源）
  local chunk=$(( (total + segs - 1) / segs ))
  local i start end
  echo "下载 $(basename "$out")（$((total / 1048576))MB，${#urls[@]} 源 × $segs 段）"
  for i in $(seq 0 $((segs - 1))); do
    start=$(( i * chunk )); end=$(( start + chunk - 1 ))
    [ "$end" -ge "$total" ] && end=$(( total - 1 ))
    [ "$start" -ge "$total" ] && continue
    (
      local want=$(( end - start + 1 )) a s ok=0 sz
      for a in 1 2 3 4 5 6; do
        s=$(( (a - 1) % ${#urls[@]} ))
        curl "${CURL_NET[@]}" -sL --connect-timeout 15 --max-time 900 -r "$start-$end" -o "$out.p$i" "${urls[$s]}" || { sleep 3; continue; }
        sz=$(stat -c %s "$out.p$i" 2>/dev/null || echo 0)
        [ "$sz" = "$want" ] && { ok=1; break; }
        sleep 3
      done
      [ "$ok" = 1 ] || echo "段失败: $(basename "$out") 段$i"
    ) &
  done
  wait

  # 3) 校验总长并拼接
  local got=0
  for i in $(seq 0 $((segs - 1))); do
    [ -f "$out.p$i" ] && got=$(( got + $(stat -c %s "$out.p$i" 2>/dev/null || echo 0) ))
  done
  if [ "$got" != "$total" ]; then
    echo "错误：$(basename "$out") 分段不完整（$got/$total 字节），清理后可重跑"
    rm -f "$out".p*
    return 1
  fi
  cat $(for i in $(seq 0 $((segs - 1))); do [ -f "$out.p$i" ] && echo "$out.p$i"; done) > "$out"
  rm -f "$out".p*
  echo "完成: $(basename "$out")"
}

# ── 1) Qdrant：API 解析最新版，多源 = gh-proxy / GitHub 直连 / ghfast ──
QV=$(curl --noproxy '*' -sL --connect-timeout 15 https://api.github.com/repos/qdrant/qdrant/releases/latest \
  | grep -oE '"tag_name": *"[^"]+"' | head -1 | sed -E 's/.*"v?([^"]+)".*/\1/')
QV="${QV:-1.20.0}"
QF="qdrant-x86_64-pc-windows-msvc.zip"
echo "Qdrant 版本: $QV"
pdownload qdrant.zip 12 \
  "https://gh-proxy.com/https://github.com/qdrant/qdrant/releases/download/v${QV}/${QF}" \
  "https://github.com/qdrant/qdrant/releases/download/v${QV}/${QF}" \
  "https://ghfast.top/https://github.com/qdrant/qdrant/releases/download/v${QV}/${QF}"

# ── 2) Temurin JRE 21（Neo4j 运行时）：清华镜像优先，Adoptium API 兜底 ──
JRE_F=$(curl --noproxy '*' -s --connect-timeout 15 --max-time 25 "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/21/jre/x64/windows/" \
  | grep -oE 'OpenJDK21U-jre_x64_windows_hotspot_[0-9._]+\.zip' | sort -Vu | tail -1)
if [ -n "$JRE_F" ]; then
  echo "JRE 版本: $JRE_F"
  pdownload jre21.zip 8 \
    "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/21/jre/x64/windows/${JRE_F}" \
    "https://api.adoptium.net/v3/binary/latest/21/ga/windows/x64/jre/hotspot/normal/eclipse?project=jdk"
else
  echo "TUNA 列表不可用，走 Adoptium API"
  pdownload jre21.zip 8 \
    "https://api.adoptium.net/v3/binary/latest/21/ga/windows/x64/jre/hotspot/normal/eclipse?project=jdk"
fi

# ── 3) PostgreSQL 17.5 EDB binaries（单源多段）──
pdownload postgresql-17.5-1-binaries.zip 12 \
  "https://get.enterprisedb.com/postgresql/postgresql-17.5-1-windows-x64-binaries.zip"

# ── 4) Neo4j community 5.26.0 ──
# dist.neo4j.org 对大陆出口 403（地域封锁）但支持 Range（206）→ 经用户本地代理分段下载；
# 华为云镜像直连作备用（吞吐不稳，实测 0~3.6KB/s 波动）
PDL_PROXY="${PDL_PROXY:-http://127.0.0.1:9909}" pdownload neo4j.zip 8 \
  "https://dist.neo4j.org/neo4j-community-5.26.0-windows.zip" \
  "https://mirrors.huaweicloud.com/neo4j/neo4j-community-5.26.0-windows.zip"

echo "── 下载汇总 ──"
ls -lh "$DL"
