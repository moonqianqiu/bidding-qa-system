#!/usr/bin/env bash
# 文档完整性机械审计（冻结勿改）—— 汇总 doc_defects，越低越好
# 分项：broken_links 死链 / xref_errors 功能点ID交叉引用 / stat_mismatch 统计不符
#       stale_terms 陈旧术语 / open_markers 未决标记 / fence_issues 围栏不平衡
cd "$(dirname "$0")/.." || exit 1

FILES=(README.md AGENTS.md docs/01_系统框架设计.md docs/02_功能结构细化.md docs/03_项目规划.md docs/04_项目结构流程图.md docs/AI交接文档.md docs/部署手册.md)

# ── 1) 死链：md 相对链接目标不存在 ──
broken=0
for f in "${FILES[@]}"; do
  [ -f "$f" ] || continue
  dir=$(dirname "$f")
  while IFS= read -r link; do
    [ -z "$link" ] && continue
    case "$link" in http*|\#*) continue ;; esac
    tgt="${link%%#*}"
    [ -z "$tgt" ] && continue
    if [ ! -e "$dir/$tgt" ]; then broken=$((broken+1)); fi
  done < <(grep -oE '\]\([^)]+\)' "$f" 2>/dev/null | sed -E 's/^\]\(//; s/\)$//; s/^<//; s/>$//' | grep -vE '^(https?:|#|mailto:)')
done
echo "METRIC broken_links=$broken"

# ── 2) 功能点 ID 交叉引用：03/README/01 与 02 映射行引用的 ID 必须在 02 定义 ──
# 注：用 awk 按 "|" 分列提取（字节级稳定），规避多字节 emoji 在 grep/sed ERE 下的 locale 陷阱
TMPDIR_LOC="${TMPDIR:-/tmp}"
AWK_OUT=$(awk -F'|' '
{
  id = $2; gsub(/[^A-Za-z0-9]/, "", id)
  nn = NF
  if (nn > 0 && $nn == "") nn--         # 行尾 "|" 会产生空尾字段，取倒数第二列
  if (nn < 1) next                      # 空行无字段
  pri = $nn; gsub(/\r/, "", pri); gsub(/[^A-Za-z0-9]/, "", pri)
  if (id ~ /^[A-H][0-9]+$/) {
    defined[id] = 1
    if (pri == "P0" || pri == "P1" || pri == "P2") cnt[pri]++
  }
}
END {
  for (k in defined) print "DEF", k
  for (k in cnt) print "CNT", k, cnt[k]
}' docs/02_功能结构细化.md)
printf '%s\n' "$AWK_OUT" | grep '^DEF ' | awk '{print $2}' | sort > "$TMPDIR_LOC/bidqa_defined.txt"
total_actual=$(printf '%s\n' "$AWK_OUT" | awk '$1=="CNT"{s+=$3; c[$2]=$3} END{print s+0}')
p0a=$(printf '%s\n' "$AWK_OUT" | awk '$1=="CNT"&&$2=="P0"{print $3+0}')
p1a=$(printf '%s\n' "$AWK_OUT" | awk '$1=="CNT"&&$2=="P1"{print $3+0}')
p2a=$(printf '%s\n' "$AWK_OUT" | awk '$1=="CNT"&&$2=="P2"{print $3+0}')

refs=$( { for f in README.md docs/01_系统框架设计.md docs/03_项目规划.md; do
            [ -f "$f" ] && grep -oE '\([^)]*\)' "$f"
          done
          grep -E '里程碑 ↔ 功能域映射' docs/02_功能结构细化.md | grep -oE '[A-H][0-9]+(-[A-H][0-9]+)?'
        } | grep -oE '[A-H][0-9]+(-[A-H][0-9]+)?' | sort -u )
xref=0
reflist=$(while IFS= read -r id; do
  [ -z "$id" ] && continue
  if [[ "$id" == *-* ]]; then
    lo="${id%-*}"; hi="${id#*-}"
    if [ "${lo:0:1}" = "${hi:0:1}" ]; then
      for ((n = ${lo:1}; n <= ${hi:1}; n++)); do echo "${lo:0:1}$n"; done
    fi
  else
    echo "$id"
  fi
done <<< "$refs" | sort -u)
while IFS= read -r id; do
  [ -z "$id" ] && continue
  grep -qx "$id" "$TMPDIR_LOC/bidqa_defined.txt" || xref=$((xref + 1))
done <<< "$reflist"
echo "METRIC xref_errors=$xref"

# ── 3) 统计一致性：02 统计表/README 总数 vs 实际功能点行数 ──
mismatch=0
claimed=$(grep -E '^\| P[012] \| [0-9]+ \|' docs/02_功能结构细化.md 2>/dev/null | awk -F'|' '{gsub(/\r/,"",$2); gsub(/ /,"",$2); gsub(/ /,"",$3); print $2"="$3}')
for pair in $claimed; do
  p="${pair%%=*}"; c="${pair##*=}"
  case "$p" in
    P0) a="$p0a" ;;
    P1) a="$p1a" ;;
    P2) a="$p2a" ;;
    *) a="" ;;
  esac
  [ "$c" != "$a" ] && mismatch=$((mismatch + 1))
done
total_claim=$(grep -oE '[0-9]+ 个功能点' README.md 2>/dev/null | head -1 | grep -oE '^[0-9]+')
if [ -n "$total_claim" ] && [ "$total_claim" != "$total_actual" ]; then mismatch=$((mismatch + 1)); fi
echo "METRIC stat_mismatch=$mismatch"
echo "METRIC total_points=$total_actual"

# ── 4) 陈旧术语残留（决策演进中已废弃的提法；含「已废弃」注记的历史行豁免）──
stale=0
for t in "智谱 embedding-3" "双供应商容灾" "GLM-5" "DEEPSEEK_API_KEY" "docker compose up" "Docker Compose (H4)" "compose 四服务" "Langfuse v3 自托管" "Langfuse 追踪 (H1)"; do
  n=$(grep -hF "$t" "${FILES[@]}" 2>/dev/null | grep -vc "已废弃")
  if [ "${n:-0}" -gt 0 ]; then stale=$((stale + 1)); fi
done
echo "METRIC stale_terms=$stale"

# ── 5) 未决标记 ──
markers=$(grep -hE 'TODO|TBD|FIXME|XXX|待定|待补充' "${FILES[@]}" 2>/dev/null | wc -l)
echo "METRIC open_markers=$markers"

# ── 6) 代码围栏平衡 ──
fence_issues=0
for f in "${FILES[@]}"; do
  [ -f "$f" ] || continue
  c=$(grep -cE '^```' "$f")
  [ $((c % 2)) -ne 0 ] && fence_issues=$((fence_issues + 1))
done
echo "METRIC fence_issues=$fence_issues"

# ── 7) 内容不变量（原 checks.sh 闸门，防"删内容刷指标"）──
invariants=0
inv() {
  if ! grep -qF "$2" "$1" 2>/dev/null; then echo "INVARIANT FAIL: $1 缺少 [$2]"; invariants=$((invariants + 1)); fi
}
[ -s README.md ] || { echo "INVARIANT FAIL: README.md 缺失或空"; invariants=$((invariants + 1)); }
for d in 01_系统框架设计 02_功能结构细化 03_项目规划 04_项目结构流程图; do
  [ -s "docs/$d.md" ] || { echo "INVARIANT FAIL: docs/$d.md 缺失或空"; invariants=$((invariants + 1)); }
done
inv README.md "OpenAI-compatible"
inv README.md "绿色安装"
inv README.md "三槽位"
inv docs/01_系统框架设计.md "sparse_bm25"
inv docs/01_系统框架设计.md "workers=1"
inv docs/01_系统框架设计.md "统一响应与错误模型"
inv docs/01_系统框架设计.md "指纹"
inv docs/02_功能结构细化.md "量化验收指标表"
inv docs/02_功能结构细化.md "黄金集题目 schema"
inv docs/03_项目规划.md "0.3 设计评审拷问决策"
inv docs/03_项目规划.md "回滚演练"
inv docs/03_项目规划.md "PRD 六类信息源"
inv docs/04_项目结构流程图.md "绿色安装"
echo "METRIC invariant_violations=$invariants"

defects=$((broken + xref + mismatch + stale + markers + fence_issues))
echo "METRIC doc_defects=$defects"
