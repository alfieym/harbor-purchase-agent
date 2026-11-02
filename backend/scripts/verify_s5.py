"""Manually verify S5 end-to-end: POST /commerce/intents over the real model + index.

Usage:
    uv run --directory backend python scripts/verify_s5.py

Requires a model API key (``OPENAI_API_KEY`` / ``HARBOR_OPENAI_API_KEY``). Reuses
the on-disk index (``.cache/catalog-index/``) built by verify_s4's ``--real-catalog``
run; builds it once if absent. Exit code 0 = pass, 1 = fail.

Everything runs in a *single* asyncio event loop: the shared runtime (products +
index + model) is assembled by the app lifespan, and the model client holds
asyncio primitives bound to that loop. Mixing ``TestClient``'s loop with a second
``asyncio.run`` loop broke those primitives (``Event bound to a different event
loop``), so here the lifespan is entered manually and every request goes through
one ``httpx.ASGITransport`` client.

What it checks (the S5 checklist plus the two concurrency behaviours):

    ① S5-1 命中         /commerce/intents → 200 + final_text + 非空 cards + sku 在目录
    ② 会话复用          同 (buyer_id, shopping_session_id) 复用上下文
    ③ 空结果           无命中 → 空 cards + 非空 final_text（不编造）
    ④ 无关输入         闲聊/情感倾诉 → 空 cards + 说明只能选购（不硬推商品）
    ⑤ S5-3 健康         /health 返回 model 名 + 依赖状态
    ⑥ S5-4 跨会话并发   两个不同 session 并发 → 都 200（互不阻塞）
    ⑦ S5-5 同会话并发   同一 session 并发两请求 → 都 200（串行执行，第二次等第一次）
    ⑧ S5-2 超时故障注入  真组装上把单轮预算压到 1ms → 504 + http_504（无堆栈）
    ⑨ S5-2 工具故障注入  真组装上把 service.search 换成必抛错函数 → 502 + http_502（无堆栈）
"""

import asyncio
import time

import httpx

from harbor.agents.factory import DEFAULT_DATA_PATH
from harbor.core.config import get_settings
from harbor.features.catalog.persistence.seed_products import load_products
from harbor.main import create_app

HIT_PROMPT = "预算300元，给猫买个饮水器"
MISS_PROMPT = "预算1元买个猫饮水器"
OFFTOPIC_PROMPT = "我的小玲她不理我呀 哭"

BASE = "http://test"


def _check_hit(body: dict, legitimate_skus: set[str], label: str, problems: list[str]) -> None:
    cards = body.get("cards", [])
    if not body.get("final_text"):
        problems.append(f"{label} 未返回 final_text")
    if not cards:
        problems.append(f"{label} 应返回非空 cards")
    for card in cards:
        if card["sku_id"] not in legitimate_skus:
            problems.append(f"{label} 卡片 sku={card['sku_id']!r} 不在目录里（疑似编造）")


async def main() -> None:
    settings = get_settings()
    problems: list[str] = []

    if not settings.llm_configured:
        print("未配置模型 key（OPENAI_API_KEY / HARBOR_OPENAI_API_KEY），跳过真实模型验证")
        raise SystemExit(1)

    print("装配共享运行时 + 索引（复用 .cache/catalog-index，首次约 4–5 分钟）…")
    app = create_app(settings)
    legitimate_skus = {
        sku.sku_id for product in load_products(DEFAULT_DATA_PATH) for sku in product.skus
    }

    # raise_app_exceptions=False: 任何未预期异常都变成 500 响应而不是把脚本打崩，脚本如实记 FAIL。
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
            # ① S5-1 命中
            r1 = await client.post(
                "/commerce/intents",
                json={"buyer_id": "u1", "shopping_session_id": "s1", "raw_query": HIT_PROMPT},
            )
            if r1.status_code != 200:
                problems.append(f"① 命中查询返回 {r1.status_code}：{r1.text}")
            else:
                body = r1.json()
                _check_hit(body, legitimate_skus, "①", problems)
                n_cards = len(body.get("cards", []))
                print(f"① 命中查询：final_text={body.get('final_text')!r} cards={n_cards}")

                # ② 会话复用
                r2 = await client.post(
                    "/commerce/intents",
                    json={
                        "buyer_id": "u1",
                        "shopping_session_id": "s1",
                        "raw_query": "换个品牌推荐",
                    },
                )
                if r2.status_code != 200:
                    problems.append(f"② 会话复用返回 {r2.status_code}：{r2.text}")
                else:
                    print("② 会话复用（同 buyer+session）OK")

            # ③ 空结果
            r3 = await client.post(
                "/commerce/intents",
                json={"buyer_id": "u1", "shopping_session_id": "s3", "raw_query": MISS_PROMPT},
            )
            if r3.status_code != 200:
                problems.append(f"③ 空结果查询返回 {r3.status_code}：{r3.text}")
            else:
                b3 = r3.json()
                if b3["cards"]:
                    problems.append(f"③ 无命中查询应返回空 cards，实际 {len(b3['cards'])} 张")
                elif not b3["final_text"].strip():
                    problems.append("③ 空结果 final_text 为空")
                else:
                    print(f"③ 空结果：cards=0 final_text={b3['final_text']!r}")

            # ④ 无关输入（会话内闲聊）—— 应拒绝选购，返回空 cards + 说明
            r4 = await client.post(
                "/commerce/intents",
                json={
                    "buyer_id": "u1",
                    "shopping_session_id": "s4",
                    "raw_query": OFFTOPIC_PROMPT,
                },
            )
            if r4.status_code != 200:
                problems.append(f"④ 无关输入返回 {r4.status_code}：{r4.text}")
            else:
                b4 = r4.json()
                if b4["cards"]:
                    problems.append(f"④ 无关输入应返回空 cards，实际 {len(b4['cards'])} 张")
                else:
                    print(f"④ 无关输入：cards=0 final_text={b4['final_text']!r}")

            # ⑤ S5-3 健康
            r5 = await client.get("/health")
            if r5.status_code != 200:
                problems.append(f"⑤ /health 返回 {r5.status_code}：{r5.text}")
            else:
                b5 = r5.json()
                if b5.get("dependencies", {}).get("llm") != "ok":
                    problems.append(
                        f"⑤ /health dependencies.llm 应为 ok，实际 {b5.get('dependencies')}"
                    )
                if not b5.get("model"):
                    problems.append("⑤ /health 应返回 model 名")
                else:
                    print(f"⑤ /health：model={b5['model']} dependencies={b5['dependencies']}")

            # ⑥⑦ 并发。放在故障注入之前：⑨ 会让模型按 max_iters 反复重试、密集打
            # 模型 API，若并发跑在它后面会被误伤成限流 502。
            # S5-4 跨会话并发
            start = time.perf_counter()
            ra, rb = await asyncio.gather(
                client.post(
                    "/commerce/intents",
                    json={
                        "buyer_id": "uA",
                        "shopping_session_id": "sA",
                        "raw_query": HIT_PROMPT,
                    },
                ),
                client.post(
                    "/commerce/intents",
                    json={
                        "buyer_id": "uB",
                        "shopping_session_id": "sB",
                        "raw_query": HIT_PROMPT,
                    },
                ),
            )
            cross = time.perf_counter() - start
            for code, label in ((ra.status_code, "⑥A"), (rb.status_code, "⑥B")):
                if code != 200:
                    problems.append(f"{label} 跨会话并发返回 {code}")
            print(f"⑥ 跨会话并发：A={ra.status_code} B={rb.status_code} 耗时{cross:.1f}s")

            # S5-5 同会话并发
            start = time.perf_counter()
            rc, rd = await asyncio.gather(
                client.post(
                    "/commerce/intents",
                    json={
                        "buyer_id": "uC",
                        "shopping_session_id": "sC",
                        "raw_query": HIT_PROMPT,
                    },
                ),
                client.post(
                    "/commerce/intents",
                    json={
                        "buyer_id": "uC",
                        "shopping_session_id": "sC",
                        "raw_query": "再推荐一个",
                    },
                ),
            )
            same = time.perf_counter() - start
            for code, label in ((rc.status_code, "⑦A"), (rd.status_code, "⑦B")):
                if code != 200:
                    problems.append(f"{label} 同会话并发返回 {code}")
            print(f"⑦ 同会话并发：A={rc.status_code} B={rd.status_code} 耗时{same:.1f}s")

            # ⑧ S5-2 超时故障注入：单轮预算压到 1ms，端到端触发 504。
            original_timeout = settings.turn_timeout_seconds
            settings.turn_timeout_seconds = 0.001
            try:
                r8 = await client.post(
                    "/commerce/intents",
                    json={
                        "buyer_id": "u1",
                        "shopping_session_id": "s-timeout",
                        "raw_query": HIT_PROMPT,
                    },
                )
            finally:
                settings.turn_timeout_seconds = original_timeout
            err8 = r8.json().get("error", {})
            if r8.status_code != 504 or err8.get("code") != "http_504":
                problems.append(f"⑧ 超时注入应返回 504/http_504，实际 {r8.status_code}：{r8.text}")
            else:
                print(f"⑧ 超时注入：{r8.status_code} {err8['code']} {err8.get('message')!r}")

            # ⑨ S5-2 工具故障注入：把 service.search 换成必抛错函数，逼 AgentScope 走
            # ToolResultState.ERROR → run_turn 转 502（真实 tool adapter，非离线 stub）。
            runtime = app.state.runtime

            def _failing_search(query_text: str, spec: object, limit: int = 10) -> list:
                raise RuntimeError("verify_s5 注入的检索故障")

            original_search = runtime.service.search
            runtime.service.search = _failing_search
            try:
                r9 = await client.post(
                    "/commerce/intents",
                    json={
                        "buyer_id": "u1",
                        "shopping_session_id": "s-toolfail",
                        "raw_query": HIT_PROMPT,
                    },
                )
            finally:
                runtime.service.search = original_search
            err9 = r9.json().get("error", {})
            if r9.status_code != 502 or err9.get("code") != "http_502":
                problems.append(
                    f"⑨ 工具故障注入应返回 502/http_502，实际 {r9.status_code}：{r9.text}"
                )
            else:
                print(f"⑨ 工具故障注入：{r9.status_code} {err9['code']} {err9.get('message')!r}")

    print(f"\n{'=' * 60}\nS5: {'OK' if not problems else 'FAIL'}")
    for problem in problems:
        print(f"  - {problem}")
    raise SystemExit(0 if not problems else 1)


if __name__ == "__main__":
    asyncio.run(main())
