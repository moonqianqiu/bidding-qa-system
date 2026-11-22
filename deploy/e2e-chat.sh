#!/usr/bin/env bash
# e2e 端到端验收（机器断言版）：/api/chat/stream 三走向，逐项断言契约字段。
# 用法：bash deploy/e2e-chat.sh [base_url]          # 真链路（真实调用主模型，注意成本）
#       bash deploy/e2e-chat.sh --mutation-check    # 自证明：坏流必须 FAIL（无需后端）
# 前置：bash deploy/start.sh（四服务在线）。
# Windows 注意：中文 body 经命令行传参坏编码——payload 一律写 UTF-8 临时文件再 --data-binary。
# 契约（docs/01 §3.4）：每行 data: JSON，seq 单调递增；终态 = type:done|error；
# 事件序 status*→thinking*→token*→终态。
set -uo pipefail
BASE="${1:-http://127.0.0.1:8000}"
TMP="${2:-$(mktemp -d)}"
[ -n "${2:-}" ] || trap 'rm -rf "$TMP"' EXIT
PY="${PYTHON:-${PY:-python}}"

check_all() { # 断言器：解析 $TMP/caseN.{sse,code}，输出 PASS/FAIL 行；有 FAIL 退出码 1
  "$PY" - "$TMP" <<'PYEOF'
import json, pathlib, re, sys

tmp = pathlib.Path(sys.argv[1])
failures = []

def load(n):
    code_file, sse_file = tmp / f"case{n}.code", tmp / f"case{n}.sse"
    code = code_file.read_text(encoding="utf-8", errors="replace").strip() if code_file.exists() else "NO_FILE"
    raw = sse_file.read_text(encoding="utf-8", errors="replace") if sse_file.exists() else ""
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                pass
    return code, events

def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" —— {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)

def common(n, label):
    code, ev = load(n)
    check(f"{label}: HTTP 200", code == "200", f"got {code}")
    types = [e.get("type") for e in ev]
    check(f"{label}: 终态事件 done|error", "done" in types or "error" in types, f"末尾 types={types[-6:]}")
    check(f"{label}: 终态唯一", types.count("done") + types.count("error") <= 1, f"count={types.count('done')+types.count('error')}")
    seqs = [e["seq"] for e in ev if isinstance(e.get("seq"), int)]
    # P2-42：旧断言 all(b>a for zip(...)) 在「事件全缺 seq / 只有 1 个事件」时 all([]) 恒真。
    # 先要求事件数≥2 且每个事件都带整数 seq，再查严格递增。
    check(f"{label}: seq 覆盖全部事件且≥2", len(ev) >= 2 and len(seqs) == len(ev),
          f"events={len(ev)} seqs={len(seqs)}")
    check(f"{label}: seq 单调递增", len(seqs) >= 2 and all(b > a for a, b in zip(seqs, seqs[1:])), f"seqs={seqs[:8]}...")
    done = next((e for e in ev if e.get("type") == "done"), None)
    err = next((e for e in ev if e.get("type") == "error"), None)
    check(f"{label}: 非 error 终态", err is None, f"error={json.dumps(err, ensure_ascii=False)[:200] if err else ''}")
    return done

# S1 RAG 引用（search_knowledge → 生成，带来源）
done = common(1, "S1 RAG")
if done:
    types = [e.get("type") for e in load(1)[1]]
    check("S1: done.sources 非空", bool(done.get("sources")), f"sources={len(done.get('sources') or [])}")
    check("S1: 有 token 输出", "token" in types)
    check("S1: schema_v 在位", isinstance(done.get("schema_v"), int), f"schema_v={done.get('schema_v')}")
else:
    check("S1: done 事件存在", False, "无 done，字段断言跳过")

# S2 澄清（无限定数字题 → ask_clarification，不检索不出来源）
done = common(2, "S2 澄清")
if done:
    check("S2: done.clarify=true", done.get("clarify") is True, f"clarify={done.get('clarify')}")
    check("S2: sources 为空", not done.get("sources"), f"sources={len(done.get('sources') or [])}")
    check("S2: phase_times 含「澄清提问」", "澄清提问" in (done.get("phase_times") or {}), f"keys={list((done.get('phase_times') or {}).keys())}")
else:
    check("S2: done 事件存在", False, "无 done")

# S3 业务库（search_database → 内容寻址图表）
done = common(3, "S3 业务库")
if done:
    tc = done.get("tool_calls") or []
    names = [t.get("name") for t in tc if isinstance(t, dict)]
    # P2-42：旧断言允许「JSON 文本里出现 search_database 即过」——参数里带该词也放行。
    # 工具口径只看 tool_calls[].name。
    check("S3: tool_calls 含 search_database", "search_database" in names, f"names={names}")
    charts = done.get("charts") or []
    check("S3: charts 非空且带内容寻址 id",
          bool(charts) and all(isinstance(c, dict) and re.fullmatch(r"[0-9a-f]{12}", str(c.get("id", ""))) for c in charts),
          f"charts={[(c.get('type'), c.get('id')) for c in charts if isinstance(c, dict)]}")
else:
    check("S3: done 事件存在", False, "无 done")

# ── 变异扩条（P2-42）：仅在存在 case4/5/6 时执行（真跑不生成这些文件）──
if (tmp / "case4.sse").exists():
    _, ev4 = load(4)
    seqs4 = [e["seq"] for e in ev4 if isinstance(e.get("seq"), int)]
    check("S4 seq: 事件带整数 seq 且≥2 条", len(ev4) >= 2 and len(seqs4) == len(ev4),
          f"events={len(ev4)} seqs={seqs4}")
    check("S4 seq: 严格递增", len(seqs4) >= 2 and all(b > a for a, b in zip(seqs4, seqs4[1:])), f"seqs={seqs4}")
if (tmp / "case5.sse").exists():
    done5 = next((e for e in load(5)[1] if e.get("type") == "done"), None)
    names5 = [t.get("name") for t in ((done5 or {}).get("tool_calls") or []) if isinstance(t, dict)]
    check("S5: 工具名只在 tool_calls.name 认", "search_database" in names5, f"names={names5}")
if (tmp / "case6.sse").exists():
    types6 = [e.get("type") for e in load(6)[1]]
    check("S6: 终态唯一（重复 done 必须红）", types6.count("done") + types6.count("error") <= 1, f"types={types6}")

print(f"── 断言汇总：{len(failures)} 项失败 ──")
sys.exit(1 if failures else 0)
PYEOF
}

run_case() { # 序号 名称 问题
  local n="$1" name="$2" msg="$3" code
  printf '{"message":"%s","client_msg_id":"e2e-%s-%s"}' "$msg" "$n" "$RANDOM" > "$TMP/payload.json"
  echo "== $name =="
  code=$(curl -sN --max-time 150 -X POST "$BASE/api/chat/stream" \
    -H 'Content-Type: application/json; charset=utf-8' \
    "${AUTH[@]}" \
    --data-binary @"$TMP/payload.json" \
    -o "$TMP/case$n.sse" -w '%{http_code}') || code="CURL_FAIL"
  echo "$code" > "$TMP/case$n.code"
  tail -c 1500 "$TMP/case$n.sse" 2>/dev/null | tail -2   # 尾部事件供人工参考
  echo
}

if [ "${1:-}" = "__assert_only__" ]; then
  # 内部分支：对 $2 目录里的现成 caseN.{sse,code} 跑断言器（--mutation-check 复用）
  check_all
  exit $?
fi

if [ "${1:-}" = "--mutation-check" ]; then
  # 变异自检（AGENTS：确认断言真的能红）：喂构造流——S1 缺终态、S2 合格澄清流、
  # S3 图表无 id；P2-42 扩条：S4 事件缺 seq、S5 工具名只藏在 args、S6 重复终态。
  "$PY" - "$TMP" <<'PYEOF'
import json, pathlib, sys
tmp = pathlib.Path(sys.argv[1])
good_done = {"type": "done", "seq": 9, "schema_v": 1, "clarify": True, "sources": [],
             "phase_times": {"澄清提问": 12.5}, "tool_calls": [], "charts": []}
no_seq_done = {k: v for k, v in good_done.items() if k != "seq"}
cases = {
    "1": 'data: {"type":"status","seq":1,"stage":"retrieve"}\n\ndata: {"type":"token","seq":2,"text":"部分"}\n\n',
    "2": 'data: {"type":"status","seq":1,"stage":"clarify"}\n\ndata: ' + json.dumps(good_done, ensure_ascii=False) + '\n\n',
    "3": 'data: {"type":"status","seq":1,"stage":"retrieve"}\n\ndata: {"type":"done","seq":2,"schema_v":1,'
         '"sources":[{"n":1}],"tool_calls":[{"name":"search_database"}],"charts":[{"type":"bar","title":"无id"}]}\n\n',
    # S4：两个事件都不带 seq（旧断言 seqs=[] → all([]) 恒真）
    "4": 'data: {"type":"status","stage":"clarify"}\n\ndata: ' + json.dumps(no_seq_done, ensure_ascii=False) + '\n\n',
    # S5：工具名只在参数里（旧「JSON 文本含 search_database 即过」会误放行）
    "5": 'data: {"type":"status","seq":1,"stage":"retrieve"}\n\ndata: {"type":"done","seq":2,"schema_v":1,'
         '"sources":[{"n":1}],"tool_calls":[{"args":{"tool":"search_database"}}],'
         '"charts":[{"type":"bar","id":"0123456789ab"}]}\n\n',
    # S6：重复终态
    "6": 'data: {"type":"status","seq":1,"stage":"retrieve"}\n\ndata: ' + json.dumps(good_done, ensure_ascii=False)
         + '\n\ndata: ' + json.dumps(good_done, ensure_ascii=False) + '\n\n',
}
for n, body in cases.items():
    (tmp / f"case{n}.sse").write_text(body, encoding="utf-8")
    (tmp / f"case{n}.code").write_text("200", encoding="utf-8")
PYEOF
  echo "── 变异自检：S1/S3/S4/S5/S6 必须出现 FAIL，S2 必须 PASS（断言器咬合力验证）──"
  OUT=$("$0" __assert_only__ "$TMP" 2>&1 | sed -n '/S1 RAG/,/断言汇总/p')
  echo "$OUT"
  if ! echo "$OUT" | grep -q "\[FAIL\] S1 RAG: 终态事件"; then echo "变异自检失败：S1 坏流被放行"; exit 1; fi
  if ! echo "$OUT" | grep -q "\[PASS\] S2: done.clarify=true"; then echo "变异自检失败：S2 好流被误杀"; exit 1; fi
  if ! echo "$OUT" | grep -q "\[FAIL\] S3: charts"; then echo "变异自检失败：S3 坏流被放行"; exit 1; fi
  if ! echo "$OUT" | grep -q "\[FAIL\] S4 seq: 事件带整数 seq"; then echo "变异自检失败：S4 缺 seq 坏流被放行"; exit 1; fi
  if ! echo "$OUT" | grep -q "\[FAIL\] S5: 工具名"; then echo "变异自检失败：S5 args 藏名坏流被放行"; exit 1; fi
  if ! echo "$OUT" | grep -q "\[FAIL\] S6: 终态唯一"; then echo "变异自检失败：S6 重复终态被放行"; exit 1; fi
  echo "变异自检通过：断言器咬得住坏流，不误杀好流"
  exit 0
fi

echo "── bidqa e2e-chat（$BASE，机器断言版）──"

# ── 鉴权（用户体系，docs/01 §3.1）：登录取会话令牌；失败回落机器主 token ──
# envget 与 python-dotenv 同口径：剥行内注释（须有空格前导，值内 # 不受影响）/
# 去 \r / 去首尾空白 / 去成对引号——2026-11-08 实证：照抄 .env.example 带「值 + #注释」
# 的行，cut 原样把注释当口令发出 → 4011。
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/backend/.env"
envget() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- \
  | sed -E 's/[[:space:]]+#.*$//' | tr -d '\r' \
  | sed -E "s/^[[:space:]]+//; s/[[:space:]]+$//; s/^\"(.*)\"$/\1/; s/^'(.*)'$/\1/" || true; }
AUTH=()
ADMIN_USER="$(envget ADMIN_USERNAME)"; ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="$(envget ADMIN_PASSWORD)"; ADMIN_PASS="${ADMIN_PASS:-admin123}"
printf '{"username":"%s","password":"%s"}' "$ADMIN_USER" "$ADMIN_PASS" > "$TMP/login.json"
LOGIN_RESP=$(curl -s --max-time 10 -X POST "$BASE/api/auth/login" \
  -H 'Content-Type: application/json' --data-binary @"$TMP/login.json") || LOGIN_RESP=""
TOKEN=$(printf '%s' "$LOGIN_RESP" | "$PY" -c "import json,sys
try: print(json.load(sys.stdin).get('token',''))
except Exception: print('')" 2>/dev/null) || TOKEN=""
if [ -n "$TOKEN" ]; then
  echo "鉴权: 登录成功（$ADMIN_USER）"
  AUTH=(-H "Authorization: Bearer $TOKEN")
else
  MACHINE_TOKEN="$(envget API_AUTH_TOKEN)"
  if [ -n "$MACHINE_TOKEN" ]; then
    echo "鉴权: 登录未成，改用机器主 token（API_AUTH_TOKEN=管理员级）"
    TOKEN="$MACHINE_TOKEN"
    AUTH=(-H "Authorization: Bearer $MACHINE_TOKEN")
  else
    echo "登录失败且无 API_AUTH_TOKEN：检查后端用户体系/ADMIN_* 配置（默认 admin/admin123）" >&2
    exit 1
  fi
fi

run_case 1 "1/3 工具多轮（search_knowledge→生成）" "单一来源采购方式公示被质疑应该怎么办？"
run_case 2 "2/3 澄清（无限定数字题→ask_clarification）" "中标金额总共多少？"
run_case 3 "3/3 业务库查询（search_database→图表）" "安徽省各标的物的中标项目数量是多少？"
echo "── 契约断言 ──"
check_all
ASSERT_RC=$?  # 断言结果必须是脚本退出码（清理段不得冲掉 FAIL）

# ── 收尾自清理（2026-11-08）：真跑产生的会话不留开发库（此前每次真跑静默积 3 条）──
# 提取与删除同段 Python（urllib 直连绕代理）——curl 循环在沙盒后台环境间歇 ERR，
# 换 $PY 通道（check_all 同款已验证路径）后彻底稳定。
"$PY" - "$TMP" "$BASE" "$TOKEN" <<'PYEOF'
import json, pathlib, sys, urllib.request

tmp, base, token = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
ids: list[str] = []
for f in sorted(tmp.glob("case*.sse")):
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("data:"):
            try:
                ev = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            cid = ev.get("conversation_id")
            if ev.get("type") == "done" and cid and cid not in ids:
                ids.append(cid)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 直连，不吃代理
ok = fail = 0
for cid in ids:
    req = urllib.request.Request(f"{base}/api/conversations/{cid}", method="DELETE",
                                 headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        opener.open(req, timeout=10)
        ok += 1
    except Exception as exc:
        fail += 1
        print(f"  ! DELETE {cid}: {exc}", file=sys.stderr)
print(f"── 收尾自清理：提取 {len(ids)} 条 / 删除 {ok} 条"
      + (f" / 失败 {fail}" if fail else "") + " ──")
PYEOF
exit "$ASSERT_RC"
