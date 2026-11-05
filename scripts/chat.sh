#!/bin/sh
# 启动 Harbor，手工体验问答。
#
# 用法：
#   ./scripts/chat.sh             启动后端 + 前端，并打开浏览器
#   ./scripts/chat.sh --cli       只启动终端 REPL（不开前端、不占浏览器）
#   ./scripts/chat.sh --restart   8010 上的旧 Harbor 无响应时，先结束它再启动
#   ./scripts/chat.sh --no-open   不自动打开浏览器
#   ./scripts/chat.sh --check     只做启动前检查，不启动任何进程
#
# 端口默认 8010（HARBOR_PORT 可改），前端 5173。
# 日志写在 tmp/harbor-chat/api.log 与 tmp/harbor-chat/web.log。
#
# 注意：Qdrant 本地目录同一时间只能被一个进程打开，所以后端与 --cli 不能同时跑。

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PORT=${HARBOR_PORT:-8010}
WEB_PORT=5173
LOG_DIR="$ROOT/tmp/harbor-chat"

MODE=web
RESTART=0
CHECK_ONLY=0
OPEN_BROWSER=1

REUSE_API=0
REUSE_WEB=0
STARTED_API=0
STARTED_WEB=0
API_PID=""
WEB_PID=""
STOPPED=0

say() { printf '%s\n' "$*"; }
die() { printf '\n错误：%s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
    case "$arg" in
        --cli) MODE=cli ;;
        --restart) RESTART=1 ;;
        --no-open) OPEN_BROWSER=0 ;;
        --check) CHECK_ONLY=1 ;;
        -h|--help) usage; exit 0 ;;
        *) printf '未知参数：%s\n\n' "$arg"; usage; exit 2 ;;
    esac
done

port_pids() {
    lsof -ti tcp:"$1" -sTCP:LISTEN 2>/dev/null || true
}

health_ok() {
    curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1
}

web_ok() {
    curl -fsS --max-time 2 "http://localhost:$WEB_PORT/" >/dev/null 2>&1
}

stop_tree() {
    pid=$1
    [ -z "$pid" ] && return 0
    kill -0 "$pid" 2>/dev/null || return 0
    for child in $(pgrep -P "$pid" 2>/dev/null || true); do
        stop_tree "$child"
    done
    kill "$pid" 2>/dev/null || true
}

cleanup() {
    if [ "$STOPPED" -eq 1 ]; then
        return 0
    fi
    STOPPED=1
    trap - INT TERM EXIT
    if [ "$STARTED_WEB" -eq 1 ]; then
        stop_tree "$WEB_PID"
    fi
    if [ "$STARTED_API" -eq 1 ]; then
        stop_tree "$API_PID"
    fi
    say ""
    say "已停止。"
}

# ---------- 1. 环境 ----------
if [ ! -f "$ROOT/backend/.env" ]; then
    cp "$ROOT/backend/.env.example" "$ROOT/backend/.env"
    die "已生成 backend/.env。请填入 OPENAI_API_KEY（或 HARBOR_OPENAI_API_KEY），再重跑本脚本。"
fi

key_ok=0
if [ -n "${OPENAI_API_KEY:-}" ] || [ -n "${HARBOR_OPENAI_API_KEY:-}" ]; then
    key_ok=1
elif grep -Eq '^[[:space:]]*(OPENAI_API_KEY|HARBOR_OPENAI_API_KEY)[[:space:]]*=[[:space:]]*[^[:space:]]+' \
    "$ROOT/backend/.env"; then
    key_ok=1
fi
[ "$key_ok" -eq 1 ] || die "backend/.env 里没有模型 key（OPENAI_API_KEY）。补上再跑；本脚本不会打印 key。"

[ -x "$ROOT/backend/.venv/bin/python" ] || die "后端依赖没装：先跑 make setup"
if [ "$MODE" = web ]; then
    [ -d "$ROOT/frontend/node_modules" ] || die "前端依赖没装：先跑 make setup"
fi

indexed=0
[ -f "$ROOT/.cache/catalog-index/fingerprint.txt" ] && indexed=1

# ---------- 2. 端口与旧进程 ----------
api_pids=$(port_pids "$PORT")
api_state=free
if health_ok; then
    api_state=healthy
elif [ -n "$api_pids" ]; then
    # uvicorn --reload 会再多一个 multiprocessing 子进程；只要有一个进程是
    # 本仓库的 ``-m harbor``，这一组就是我们的旧后端。
    ours=0
    for pid in $api_pids; do
        cmd=$(ps -o command= -p "$pid" 2>/dev/null || true)
        case "$cmd" in
            *"$ROOT"*"-m harbor"*) ours=1 ;;
        esac
    done
    if [ "$ours" -eq 1 ]; then
        api_state=stale
    else
        api_state=foreign
    fi
fi

web_pids=$(port_pids "$WEB_PORT")
web_state=free
if web_ok; then
    web_state=healthy
elif [ -n "$web_pids" ]; then
    web_state=stale
fi

say "Harbor 启动检查"
say "  后端  127.0.0.1:$PORT  $api_state${api_pids:+（PID: $(printf '%s' "$api_pids" | tr '\n' ' ')）}"
if [ "$MODE" = web ]; then
    say "  前端  localhost:$WEB_PORT  $web_state${web_pids:+（PID: $(printf '%s' "$web_pids" | tr '\n' ' ')）}"
fi
if [ "$indexed" -eq 1 ]; then
    say "  索引  已缓存，重启无需重新 embedding"
else
    say "  索引  未建立：首次启动约 4–5 分钟，会消耗 embedding 额度"
fi
say "  模型  key 已配置（值不打印）"

if [ "$CHECK_ONLY" -eq 1 ]; then
    say ""
    if [ "$api_state" = foreign ] || [ "$web_state" = stale ]; then
        say "检查结果：有阻塞项，见上面的端口状态。"
        exit 1
    fi
    say "检查结果：可以启动。"
    exit 0
fi

if [ "$api_state" = foreign ]; then
    say ""
    for pid in $api_pids; do ps -o pid=,command= -p "$pid"; done
    die "端口 $PORT 被别的程序占用，先结束它，或设 HARBOR_PORT 换端口。"
fi

if [ "$MODE" = cli ] && [ "$api_state" != free ]; then
    if [ "$RESTART" -eq 0 ]; then
        if [ -t 0 ]; then
            printf '后端正占着 Qdrant 目录，CLI 需要它让位。结束它？[y/N] '
            read -r answer
            case "$answer" in
                y|Y|yes|YES) ;;
                *) die "已取消。也可以加 --restart 重跑。" ;;
            esac
        else
            die "后端在跑，CLI 打不开同一个 Qdrant 目录。加 --restart 重跑，或先停后端。"
        fi
    fi
fi

if [ "$MODE" = web ] && [ "$api_state" = stale ]; then
    say "端口 $PORT 上有一个无响应的旧 Harbor。"
    if [ "$RESTART" -eq 0 ]; then
        if [ -t 0 ]; then
            printf '结束它并重新启动？[y/N] '
            read -r answer
            case "$answer" in
                y|Y|yes|YES) ;;
                *) die "已取消。也可以加 --restart 直接重启。" ;;
            esac
        else
            die "非交互环境不替你结束进程。加 --restart 重跑。"
        fi
    fi
fi

if [ "$MODE" = cli ] && [ "$api_state" = healthy ]; then
    # 走到这里说明用户同意用 --restart 让位。
    api_state=stale
fi

if [ "$api_state" = stale ]; then
    pids=$(port_pids "$PORT")
    for pid in $pids; do
        kill "$pid" 2>/dev/null || true
    done
    i=0
    while [ -n "$(port_pids "$PORT")" ] && [ "$i" -lt 10 ]; do
        sleep 1
        i=$((i + 1))
    done
    for pid in $(port_pids "$PORT"); do
        kill -9 "$pid" 2>/dev/null || true
    done
    say "旧进程已结束。"
    api_state=free
fi

if [ "$MODE" = web ] && [ "$web_state" = stale ]; then
    die "端口 $WEB_PORT 被占用且不响应（Vite 开了 strictPort，不会自动换端口）。先结束它再跑。"
fi

# ---------- 3. 启动 ----------
if [ "$MODE" = cli ]; then
    say ""
    if [ "$indexed" -eq 0 ]; then
        say "首次建索引约 4–5 分钟，期间会调用 embedding。"
    fi
    say "进入终端 REPL（/quit 退出）…"
    cd "$ROOT/backend"
    exec uv run python -m harbor.agents
fi

mkdir -p "$LOG_DIR"
STOPPED=0
trap cleanup INT TERM EXIT

if [ "$api_state" = healthy ]; then
    say ""
    say "复用已在运行的后端。"
    REUSE_API=1
else
    : >"$LOG_DIR/api.log"
    say ""
    say "启动后端…（日志：tmp/harbor-chat/api.log）"
    (
        cd "$ROOT/backend"
        exec uv run python -m harbor
    ) >"$LOG_DIR/api.log" 2>&1 &
    API_PID=$!
    STARTED_API=1

    if [ "$indexed" -eq 0 ]; then
        wait_seconds=600
    else
        wait_seconds=120
    fi
    printf '等待后端就绪'
    waited=0
    while [ "$waited" -lt "$wait_seconds" ]; do
        health_ok && break
        if ! kill -0 "$API_PID" 2>/dev/null; then
            say ""
            tail -n 30 "$LOG_DIR/api.log" >&2
            die "后端进程退出了。上面是最后 30 行日志。"
        fi
        sleep 2
        waited=$((waited + 2))
        if [ $((waited % 10)) -eq 0 ]; then
            printf '.'
        fi
    done
    if ! health_ok; then
        say ""
        tail -n 30 "$LOG_DIR/api.log" >&2
        die "等了 ${wait_seconds}s 后端仍无响应。"
    fi
    say " 就绪。"
fi

health=$(curl -fsS --max-time 3 "http://127.0.0.1:$PORT/health" 2>/dev/null || true)
case "$health" in
    *'"llm_configured":true'*) say "模型：已配置。" ;;
    *) say "警告：健康检查显示 LLM 未配置，问答会失败。" ;;
esac

if [ "$web_state" = healthy ]; then
    say "复用已在运行的前端。"
    REUSE_WEB=1
else
    : >"$LOG_DIR/web.log"
    say "启动前端…（日志：tmp/harbor-chat/web.log）"
    (
        cd "$ROOT/frontend"
        VITE_API_PROXY_TARGET="http://127.0.0.1:$PORT" exec npm run dev
    ) >"$LOG_DIR/web.log" 2>&1 &
    WEB_PID=$!
    STARTED_WEB=1

    printf '等待前端就绪'
    waited=0
    while [ "$waited" -lt 60 ]; do
        web_ok && break
        if ! kill -0 "$WEB_PID" 2>/dev/null; then
            say ""
            tail -n 30 "$LOG_DIR/web.log" >&2
            die "前端进程退出了。上面是最后 30 行日志。"
        fi
        sleep 1
        waited=$((waited + 1))
        printf '.'
    done
    if ! web_ok; then
        say ""
        tail -n 30 "$LOG_DIR/web.log" >&2
        die "前端没有起来。"
    fi
    say " 就绪。"
fi

if [ "$OPEN_BROWSER" -eq 1 ]; then
    if command -v open >/dev/null 2>&1; then
        open "http://localhost:$WEB_PORT"
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "http://localhost:$WEB_PORT" >/dev/null 2>&1 || true
    fi
fi

cat <<EOF

已就绪
  健康页    http://localhost:$WEB_PORT   （前端目前只有健康卡片，还没有聊天 UI）
  接口文档  http://127.0.0.1:$PORT/docs
  问答接口  POST http://127.0.0.1:$PORT/commerce/intents

在另一个终端直接问一句（同一 buyer_id + shopping_session_id 会接着上文）：

  curl -s http://127.0.0.1:$PORT/commerce/intents \\
    -H 'content-type: application/json' \\
    -d '{"buyer_id":"me","shopping_session_id":"s1","raw_query":"300 元以内的不锈钢保温杯"}' \\
    | python3 -m json.tool --no-ensure-ascii

试几句：
  · 300 元以内的不锈钢保温杯
  · 200 元以内的宠物饮水器，不要塑料
  · 母婴宠物类目，轻一点的猫砂盆
  · 帮我对比两款 500 元以内的猫爬架

想连续对话就用 ./scripts/chat.sh --cli（终端 REPL，/quit 退出）。
首次问答会下载本地重排模型（约 1GB，失败会自动跳过）；每条查询会调一次 embedding。
Ctrl-C 停止（只结束本脚本启动的进程；复用的后端不会被动）。
EOF

wait
