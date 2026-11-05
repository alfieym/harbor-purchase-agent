"""S9 流程图的手工验证：机器能查的自动查，只能人眼看的列成清单。

用法（仓库根目录）：

    uv run --directory backend python ../scripts/verify_s9_page.py
    uv run --directory backend python ../scripts/verify_s9_page.py --open

自动检查分八组：文件形态、主题 token、外部资源、手机留白、泳道图连线、
判定交互、页面默认值与 ``dispatch.py`` 一致、七条验收齐全。任一组失败返回 1。

页面引用的实测数字（S9-1 的 0.40s/0.80s、7.1s/9.1s）不在这里复算——
那是 ``backend/scripts/verify_s9.py`` 与 ``accept_s9_real.py`` 的活。
"""

import argparse
import inspect
import re
import webbrowser
from pathlib import Path

from harbor.agents.dispatch import (
    _LEDGER_HISTORY,
    DISPATCH_STATE_KEY,
    make_task_dispatch_tool,
)
from harbor.agents.prompts import system_prompt_for

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "_pipeline_html" / "s9-dispatch-pipeline.html"

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    CHECKS.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name} — {detail}")


def block(text: str, pattern: str) -> str:
    """Return the first brace block matching ``pattern``, braces balanced."""
    match = re.search(pattern, text)
    if match is None:
        return ""
    open_at = text.find("{", match.start())
    if open_at < 0:
        return ""
    depth = 0
    for index in range(open_at, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[open_at + 1 : index]
    return ""


def tokens_in(css: str) -> set[str]:
    return set(re.findall(r"(--[\w-]+)\s*:", css))


def section_shape(text: str) -> None:
    print("\n[1/8] 文件形态")
    forbidden = [
        pattern
        for pattern in (r"<!doctype", r"<html[\s>]", r"<head[\s>]", r"<body[\s>]")
        if re.search(pattern, text, re.IGNORECASE)
    ]
    check(
        "片段页（无 doctype / html / head / body）",
        not forbidden,
        f"命中={forbidden}" if forbidden else "发布时由 Artifact 外壳补齐",
    )

    title = re.search(r"<title>(.*?)</title>", text)
    title_text = title.group(1) if title else ""
    check(
        "title 位于前 8KB 且是短名",
        title is not None and title.end() <= 8192 and 2 <= len(title_text) <= 24,
        f"「{title_text}」（结束于第 {title.end() if title else 0} 字符）",
    )

    check(
        "声明 viewport-fit-sides",
        'name="claude:viewport-fit-sides"' in text,
        "与 body 两侧 max() 留白配套出现",
    )


def section_theme(text: str) -> None:
    print("\n[2/8] 主题 token")
    light = block(text, r"(?m)^\s*:root\s*\{")
    light_tokens = tokens_in(light)
    used = set(re.findall(r"var\((--[\w-]+)\)", text))
    missing = sorted(used - light_tokens)
    check(
        "所有 var() 都先在裸 :root 定义",
        bool(light_tokens) and not missing,
        f"浅色定义 {len(light_tokens)} 个；缺失={missing or '无'}",
    )

    dark_media = tokens_in(block(text, r':root:not\(\[data-theme="light"\]\)\s*\{'))
    dark_attr = tokens_in(block(text, r':root\[data-theme="dark"\]\s*\{'))
    check(
        "两个深色块重定义同一组 token",
        bool(dark_media) and dark_media == dark_attr,
        f"media={len(dark_media)} 个；attr={len(dark_attr)} 个",
    )
    check(
        "深色块只重定义、不新增 token",
        dark_media <= light_tokens,
        f"新增={sorted(dark_media - light_tokens) or '无'}",
    )
    check(
        "深色块带 color-scheme: dark",
        text.count("color-scheme: dark") >= 2 and "color-scheme: light" in light,
        f"dark 声明 {text.count('color-scheme: dark')} 次；light 在裸 :root",
    )

    body = block(text, r"(?m)^\s*body\s*\{")
    check("body 用 token 画背景", "background: var(--bg)" in body, "避免透出宿主背景")

    literals = [
        line.strip()
        for line in text.splitlines()
        if re.search(r":\s*#[0-9A-Fa-f]{3,8}\b", line) and not line.strip().startswith("--")
    ]
    check("token 块之外没有硬编码颜色", not literals, f"命中={literals[:3] or '无'}")


def section_assets(text: str) -> None:
    print("\n[3/8] 外部资源")
    links = re.findall(r'<link[^>]+href="([^"]+)"', text)
    bad = [url for url in links if not url.startswith("https://fonts.googleapis.com/")]
    check(
        "样式表只来自 Google Fonts",
        bool(links) and not bad,
        f"共 {len(links)} 个；越界={bad or '无'}",
    )
    check("没有外部脚本", "<script src" not in text, "页面脚本全部内联")
    check("没有外链图片", 'src="http' not in text, "图形全部是内联 SVG")


def section_layout(text: str) -> None:
    print("\n[4/8] 手机留白与溢出")
    gutter = (
        "padding-left: max(16px, env(safe-area-inset-left, 0px))" in text
        and "padding-right: max(16px, env(safe-area-inset-right, 0px))" in text
    )
    check("body 两侧 ≥16px 安全区留白", gutter, "侧边系统条也不会压字")
    check(
        "流程图自己横向滚动，不推宽页面",
        "overflow-x: auto" in text and "min-width: 720px" in text,
        ".diagram 容器 overflow-x: auto",
    )
    check(
        "有 400px 级断点",
        "max-width: 560px" in text or "max-width: 640px" in text,
        "表格、甘特、流程条各自收成单列",
    )
    check("尊重 prefers-reduced-motion", "prefers-reduced-motion" in text, "关闭平滑滚动与过渡")


def section_diagram(text: str) -> None:
    print("\n[5/8] 泳道图连线")
    ids = set(re.findall(r'id="([^"]+)"', text))
    refs = set(re.findall(r"url\(#([\w-]+)\)", text))
    check(
        "每个 url(#…) 都能解析",
        bool(refs) and refs <= ids,
        f"引用 {len(refs)} 个；缺失={sorted(refs - ids) or '无'}",
    )
    check(
        "三支箭头标记齐全",
        {"arr", "arr-accent", "arr-soft"} <= ids,
        "中性 / 派发 / 证据各一支",
    )
    check(
        "图有无障碍描述",
        'role="img"' in text and 'aria-label="S9' in text,
        "SVG 带 aria-label，读屏可读",
    )
    check("图有题注", text.count("<figcaption>") >= 1, "figcaption 说清图的主张")
    check("viewBox 驱动缩放", 'viewBox="0 0 980 470"' in text, "宽度交给 CSS")


def section_interaction(text: str) -> None:
    print("\n[6/8] 三判据判定")
    ids = [f"c{index}" for index in (1, 2, 3)]
    check("三个判据复选框都在", all(f'id="{value}"' in text for value in ids), "c1 / c2 / c3")
    check(
        "脚本绑定每个复选框",
        all(f'getElementById("{value}")' in text for value in ids),
        "change 事件驱动 render()",
    )
    check(
        "两种判定文案都在",
        "派发 · " in text and "自己做 · " in text,
        "勾掉任一条会翻转",
    )
    check("判定说明会点名不成立的那条", "因为「" in text and "不成立" in text, "不是笼统地改口")


def section_facts(text: str) -> None:
    print("\n[7/8] 页面默认值 ↔ dispatch.py")
    signature = inspect.signature(make_task_dispatch_tool)

    def default(name: str) -> object:
        return signature.parameters[name].default

    concurrency = default("max_concurrency")
    task_timeout = default("task_timeout_seconds")
    batch_timeout = default("batch_timeout_seconds")
    max_tasks = default("max_tasks")

    check("并发上限", f"≤{concurrency}" in text, f"代码={concurrency}")
    check(
        "单任务超时",
        "单任务超时" in text and f"{int(task_timeout)}s" in text,
        f"代码={task_timeout}s",
    )
    check(
        "批次预算",
        f"批预算 {int(batch_timeout)}s" in text and batch_timeout < task_timeout,
        f"代码={batch_timeout}s，且小于单任务超时",
    )
    check("单批任务上限", f"≤ {max_tasks}" in text, f"代码={max_tasks}")
    check("账本保留批数", f"保留 {_LEDGER_HISTORY} 批" in text, f"代码={_LEDGER_HISTORY}")
    check("状态键", DISPATCH_STATE_KEY in text, f"middle_context['{DISPATCH_STATE_KEY}']")

    prompt = system_prompt_for(["家居生活"], dispatch_enabled=True)
    criteria = ["可并行", "需要上下文隔离", "链路够深"]
    check(
        "三判据与主 prompt 同词",
        all(word in prompt for word in criteria) and all(f'"{word}"' in text for word in criteria),
        "页面没有自造判据名",
    )


def section_acceptance(text: str) -> None:
    print("\n[8/8] 告示的验收结论")
    missing = [f"S9-{index}" for index in range(1, 8) if f"S9-{index}" not in text]
    check("七条编号齐全", not missing, f"缺失={missing or '无'}")
    passes = len(re.findall(r'class="tstat pass">PASS<', text))
    check("七行都标 PASS", passes == 7, f"PASS 行={passes}")
    check("备注了实测出处", "离线脚本" in text, "数字不是凭空写的")


def print_checklist() -> None:
    print("\n人眼点检（脚本替不了的部分）")
    for line in (
        f"打开：{PAGE}",
        "1. 三条判据都勾着 → 右侧应是「派发 · task_dispatch」",
        "2. 勾掉任意一条 → 翻成「自己做 · product_search」，并点名是哪条",
        "3. Tab 走到复选框、空格切换 → 键盘可用",
        "4. 系统切深色（或 DevTools 模拟 prefers-color-scheme: dark）→ 纸感不变、字不发灰",
        "5. 窗口缩到 400px → 页面没有横向滚动条；只有泳道图在自己的框里横向滚",
        "6. 页首 flow 条 ①–⑥ 与泳道图里的圈号一一对应",
        "7. 复核实测数字：uv run --directory backend python scripts/verify_s9.py",
    ):
        print(f"  □ {line}")


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 S9 流程图页面")
    parser.add_argument("--open", action="store_true", help="全部通过后在浏览器打开")
    args = parser.parse_args()

    if not PAGE.exists():
        print(f"找不到页面：{PAGE}")
        return 2

    text = PAGE.read_text(encoding="utf-8")
    size = len(text.encode("utf-8"))
    print("S9 流程图手工验证（静态检查 + 人工清单）")
    print(f"页面：{PAGE}（{size} 字节）")

    section_shape(text)
    section_theme(text)
    section_assets(text)
    section_layout(text)
    section_diagram(text)
    section_interaction(text)
    section_facts(text)
    section_acceptance(text)

    failed = [name for name, ok, _ in CHECKS if not ok]
    print()
    if failed:
        print(f"失败 {len(failed)} 项：{'、'.join(failed)}")
        return 1

    print(f"自动检查全部通过（{len(CHECKS)} 项）。")
    print_checklist()
    if args.open:
        webbrowser.open(PAGE.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
