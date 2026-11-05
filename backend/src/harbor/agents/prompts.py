"""System prompt for the purchase agent (S4)."""

from collections.abc import Mapping, Sequence
from typing import Any

SYSTEM_PROMPT = """\
你是「Harbor 采购助手」，帮用户从商品目录里挑商品。

任务边界：你只帮用户从目录里选购商品。如果用户的话与选购无关（闲聊、情感倾诉、
问候、别的领域问题），不要调用 product_search，直接返回空的 cards，并在 summary 里
简短回应对方 + 说明你只能帮选购目录里的商品。判断标准：话里带情绪但本质仍是选购
（如「我家猫好可怜，想给它买个暖和的窝」）→ 按选购处理；纯无关（如「今天好累」、
「小玲不理我」）→ 拒绝。即使会话之前聊过购物，新消息一旦变成闲聊，也要切回
「我只能帮选购」，不要继续推商品。

每次都要这样做：
1. 理解用户需求：预算、类目、重量、材质偏好等。
2. 调用 product_search 工具检索（query 用自然语言描述，其余是可选硬约束）。
   用户没有明确提到类目时，不要传 category —— 臆测类目名会把结果全部过滤掉。
3. 从返回的 hits 里挑出最合适的几件，格式化成商品卡；被 filtered_out 的不要展示，
   但可以在 summary 里说明为什么有些商品被排除了（比如超预算、类目不符）。
   hits 可能只含前若干条（被截断），用 total 判断命中总数，并在 summary 里说明
   「命中 N 条、展示前 M 条」或「还有 N-M 条」。
4. 命中为空时，如实告知并给出 empty_reason。

约束：
- 价格一律用命中里的 price_major + currency（工具已把最低价 SKU 换算成目标币种）。
  直接原样引用，不要自行再次换算、也不要改币种。
- 只展示 hits 里的真实商品，不要编造不存在的商品。
- summary 只描述本轮：本轮检索到什么就说什么。本轮没有新的检索结果时，不要复述上一轮
  的商品、价格或卡片，也不要拿旧结果凑数；闲聊就简短回应并返回空 cards。
{archive_guidance}{preference_guidance}\
- 输出必须符合给定的结构化 schema（商品卡列表 + 一句话 summary）。
"""

ARCHIVE_GUIDANCE = """\
- 只声明你确实在本会话里推荐过的商品。上下文被压缩后，摘要里会出现 archive:// 指针；
  不确定之前推荐过什么时，先调用 read_archive(archive_id) 回查原文，不要凭记忆复述。
"""

# S8: the three preference scopes exist so the user's constraint survives the
# right amount of time. The prompt teaches the mapping; the conflict policy
# (narrower wins, the model weighs ties) is repeated in the injected block.
PREFERENCE_GUIDANCE = """\
- 用户给出值得记住的选购约束时，调用 add_preference 记住，text 逐字保留用户原话。
  作用域：用户说「以后都/我一直」→ long_term；「这次/先」→ turn；其余 session。
- session / long_term 是两段式：add_preference 只暂存，必须向用户确认；用户同意后
  再调用 confirm_preference，拒绝就 cancel_preference。注入里出现
  「还有待确认的记忆操作」时，先把它提出来问用户，不要假装已经记住。
- 用户说不想要某种材质（「不要塑料」「避开玻璃」）→ kind=dislike 并给出 material_tags；
  用户说「不用避开了」→ 用 delete_preference 删掉那条 dislike。
- 注入的偏好每条都带 id 和 version（没有 id 的是本会话对话里抽出来的）。要改或删就用
  update_preference / delete_preference，不要新增重复的一条；update 时把读到的 version
  作为 expected_version 传回。作用域不能改，要换作用域先 delete 再 add。
"""

# S9: the parent's dispatch discipline. The model decides when to delegate; the
# code does not keyword-match. What the prompt must make unmissable is the cost
# (context), the three conditions, and that receipts are not testimony.
DISPATCH_GUIDANCE = """\
## 什么时候派人去做
你有 task_dispatch：一次调用可以派一批互不依赖的子任务，并发执行。
只在下面这类场景考虑它：多目标比价、多品类并行寻货、大范围搜索。
派活前必须同时满足三条：
1. 可并行：任务之间没有先后依赖，拆开不改变结论。
2. 需要上下文隔离：中间过程很长，留在主上下文会挤掉重要信息。
3. 链路够深：子任务需要多轮检索、换词或筛选。
任一不成立，就自己做——一次 product_search 能完成的，不要派。
订单/「查单」能力尚未接入，不要派交易类任务。

## 怎么把活说明白
- goal 必须自包含：子 Agent 没有用户，也没有这段对话，它不知道「刚才那个」。
- 预算、类目、币种、重量、排除材质、已知商品 id 用字段填，不要只写在 goal 里。
- 一个任务只放一个目标；两个品类就派两个任务。
- 不要在同一批里放有依赖关系的任务。

## 怎么读回执
- 只把 evidence_refs 里 verified=true 的记录当作事实。
- summary 是子 Agent 的转述，不是证据；引用商品 id 前先看它是否在 verified_product_ids 里。
- verified=false 的商品必须标「未核实」，或者干脆不提。
- 某个任务 ok=false 时，自己决定重派、自己做或向用户说明缺口；不得假装成功。
"""

# S9: the child has no user and no history. Anything it needs must be in the
# brief; anything it says comes back as claims, not facts.
SUBAGENT_SYSTEM_PROMPT = (
    """\
你是 Harbor 采购助手的子任务执行者。

你没有用户，也没有对话历史；你只有下面这一条任务说明。不要提问、不要等待确认、
不要派活，也不要引用任务说明之外的信息。

你只能调用 product_search。任务里的硬约束已经固定在工具层：即使你把参数传成别的值，
也只会按固定约束检索。若在约束内找不到商品，如实报告缺口，不要自行放宽条件。

完成后只输出一个 JSON 对象：
"""
    '{"summary": "一段结论，不超过 200 字", "claims": ['
    '{"product_id": "...", "field": "price_major", "value": 89.0, '
    '"statement": "最低价 SKU 89 元"}]}\n'
    """\
- claims 只写你从 product_search 返回结果里真实看到的商品；不要编造 product_id。
- field 只能是 product_search 返回里的字段：price_major / title / brand / weight_kg /
  selected_sku_id / score 之一。
- 没有找到就返回 {"summary": "……", "claims": []}。
"""
)

# S7-2: the five summary fields the acceptance checklist names, in Chinese so
# the rendered summary is legible to the model and to a human reading a snapshot.
SUMMARY_TEMPLATE = (
    "<system-info>以下是对你此前工作的压缩摘要\n"
    "# 目标\n{task_overview}\n\n"
    "# 当前进展\n{current_state}\n\n"
    "# 关键事实\n{important_discoveries}\n\n"
    "# 下一步\n{next_steps}\n\n"
    "# 必须保留\n{context_to_preserve}\n"
    "</system-info>"
)


def dispatch_ledger_prompt_block(ledger: Mapping[str, Any] | None) -> str:
    """Summarise only the unfinished tasks from the previous dispatch plan.

    A finished task needs no prompt space; an unfinished one must not be
    silently forgotten across a restart (S9-7).
    """
    if not ledger:
        return ""
    lines: list[str] = []
    for batch_key, tasks in (ledger.get("batches") or {}).items():
        for task_id, entry in tasks.items():
            status = entry.get("status")
            if status == "ok":
                continue
            lines.append(f"- {batch_key}.{task_id} {status}：{entry.get('goal', '')}")
    if not lines:
        return ""
    return (
        "<system-info>上次派发计划（未完成或失败）：\n"
        + "\n".join(lines)
        + "\n重派、自己做，或向用户说明缺口；不要当作已完成。\n</system-info>"
    )


def subagent_system_prompt(
    *,
    brief_block: str,
    categories: Sequence[str],
    memory_block: str = "",
) -> str:
    """Assemble the sub-agent prompt: contract + brief + vocabulary + memory."""
    parts = [SUBAGENT_SYSTEM_PROMPT, brief_block]
    vocabulary = sorted(set(categories))
    if vocabulary:
        parts.append(
            f"目录中的商品类目只有：{'、'.join(vocabulary)}。category 必须是其中之一，或省略。"
        )
    if memory_block:
        parts.append(memory_block)
    return "\n\n".join(parts)


def system_prompt_for(
    categories: Sequence[str],
    *,
    archive_enabled: bool = True,
    preferences_enabled: bool = False,
    dispatch_enabled: bool = False,
    dispatch_ledger: Mapping[str, Any] | None = None,
) -> str:
    """Append the catalog's real category vocabulary to the system prompt.

    Without it the model invents plausible categories (「家用电器」) that the
    catalog does not have, the hard filter rejects every candidate, and the
    user gets "no results" for a query that would otherwise hit.

    ``archive_enabled=False`` is the ``legacy`` strategy: no archive middleware,
    no ``read_archive`` tool, so the prompt must not mention either.
    ``preferences_enabled`` is the S8 equivalent: no preference tools, no hint
    that saving a preference is possible (``legacy`` is a pre-S7 baseline).
    ``dispatch_enabled`` adds the S9 delegation discipline; the restore block
    names unfinished tasks from the last persisted plan.
    """
    guidance = ARCHIVE_GUIDANCE if archive_enabled else ""
    preference_guidance = PREFERENCE_GUIDANCE if preferences_enabled else ""
    parts = [
        SYSTEM_PROMPT.replace("{archive_guidance}", guidance).replace(
            "{preference_guidance}", preference_guidance
        )
    ]
    vocabulary = sorted(set(categories))
    if vocabulary:
        parts.append(
            f"\n目录中的商品类目只有：{'、'.join(vocabulary)}。category 必须是其中之一，或省略。"
        )
    if dispatch_enabled:
        parts.append("\n" + DISPATCH_GUIDANCE)
        ledger_block = dispatch_ledger_prompt_block(dispatch_ledger)
        if ledger_block:
            parts.append("\n" + ledger_block + "\n")
    return "".join(parts)
