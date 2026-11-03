#!/usr/bin/env bash
# 手工验证 S5/S6 的 HTTP 契约。先另开一个终端跑 `make dev-api`（端口 8010），
# 且跑之前停掉其它占用 Qdrant 本地目录的进程。
#
# 用法：
#   ./scripts/manual_api_check.sh                # health + 一轮问答 + 会话列表
#   FOLLOWUP=1 ./scripts/manual_api_check.sh     # 额外追问「刚才第一个多少钱」（再花 2 次 API）
#   ./scripts/manual_api_check.sh --corrupt      # S6-3：需要刚重启的服务（内存缓存会掩盖磁盘损坏）
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8010}"
ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
BUYER="${BUYER:-manual-buyer}"
SID="${SID:-manual-$(date +%Y%m%d-%H%M%S)}"
FOLLOWUP="${FOLLOWUP:-0}"

post_intents() {
  curl -sS -X POST "$BASE/commerce/intents" \
    -H 'Content-Type: application/json' \
    -d "{\"buyer_id\":\"$BUYER\",\"shopping_session_id\":\"$SID\",\"raw_query\":\"$1\"}"
}

snapshot_path() {
  python3 -c '
import hashlib, sys
print(hashlib.sha256((sys.argv[1] + "\x00" + sys.argv[2]).encode()).hexdigest())
' "$1" "$2"
}

# ---------- S6-3：手工改坏快照，必须 500 而不是静默清空 ----------
if [ "${1:-}" = "--corrupt" ]; then
  TARGET_BUYER="${TARGET_BUYER:-smoke-buyer}"
  TARGET_SID="${TARGET_SID:-smoke-s6-1}"
  SNAP="$ROOT/data/sessions/snapshots/$(snapshot_path "$TARGET_BUYER" "$TARGET_SID").json"
  [ -f "$SNAP" ] || { echo "找不到快照：$SNAP（先跑 scripts/s6_smoke.py phase1）" >&2; exit 1; }

  # 前提检查：这个会话在当前进程里没有被缓存过。
  echo "== S6-3: 改坏 $TARGET_SID 的快照，期望 500 =="
  cp "$SNAP" "$SNAP.bak"
  printf '{not valid json' > "$SNAP"
  code=$(curl -sS -o /tmp/harbor_error.json -w '%{http_code}' \
    -X POST "$BASE/commerce/intents" -H 'Content-Type: application/json' \
    -d "{\"buyer_id\":\"$TARGET_BUYER\",\"shopping_session_id\":\"$TARGET_SID\",\"raw_query\":\"随便问问\"}")
  echo "HTTP $code"
  jq . /tmp/harbor_error.json
  test "$code" = "500"
  test -f "$SNAP"          # 坏文件保留，供排查
  mv "$SNAP.bak" "$SNAP"   # 恢复后会话应重新可用
  echo "已恢复快照：$SNAP"
  echo
  echo "注意：若服务在本进程里已加载过该会话，会命中内存缓存而不读盘；此时请先重启 make dev-api。"
  exit 0
fi

echo "== 0. health =="
curl -sS "$BASE/health" | jq '{service, llm_configured, model, dependencies}'

echo
echo "== 1. 第一轮：给猫买个饮水器（1 次探针 embedding + 1 次 query embedding + 1 次 chat）=="
post_intents "给猫买个饮水器" | tee /tmp/harbor_turn1.json |
  jq '{final_text, cards: [.cards[] | {sku_id, price_major, currency, title}]}'

echo
echo "== 2. 会话列表（S6-5：按 last_active_at 倒序）=="
curl -sS "$BASE/commerce/sessions?buyer_id=$BUYER" | jq .

if [ "$FOLLOWUP" = "1" ]; then
  echo
  echo "== 3. 追问：刚才第一个多少钱（再花 1 次 query embedding + 1 次 chat）=="
  post_intents "刚才第一个多少钱" | jq '{final_text, cards: [.cards[] | {sku_id, price_major, currency, title}]}'
  echo
  echo "== 3b. 列表里应有 turn_count = 2 =="
  curl -sS "$BASE/commerce/sessions?buyer_id=$BUYER" | jq .
else
  echo
  echo "== 3. 跳过追问（FOLLOWUP=1 才跑，会再花 2 次 API）=="
fi

echo
echo "完成：buyer=$BUYER session=$SID"
echo "快照：$ROOT/data/sessions/snapshots/$(snapshot_path "$BUYER" "$SID").json"
echo "流水：$ROOT/data/sessions/journal.jsonl（每成功轮恰好 2 行）"
