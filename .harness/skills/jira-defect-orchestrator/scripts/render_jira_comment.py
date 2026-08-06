#!/usr/bin/env python3
"""Render a structured Jira comment in the repository's preferred format."""

from __future__ import annotations

import argparse
from pathlib import Path

COMPACT_TITLE_BY_STAGE = {
    "intake": "缺陷信息",
    "triage": "分诊结论",
    "investigation": "根因分析",
    "e2e-repro": "E2E 复现",
    "fix-plan": "方案设计",
    "implementation": "实现说明",
    "verification": "已验证",
    "closure": "关闭结论",
    "gate-pass-sync": "关键信息",
}


def read_items(inline_values: list[str], file_paths: list[str]) -> list[str]:
    items: list[str] = []
    items.extend(value.strip() for value in inline_values if value.strip())

    for file_path in file_paths:
        content = Path(file_path).read_text(encoding="utf-8").strip()
        if content:
            items.append(content)

    return items


def format_bullets(items: list[str]) -> str:
    lines: list[str] = []
    for item in items:
        normalized = item.rstrip().splitlines()
        if not normalized:
            continue
        lines.append(f"- {normalized[0]}")
        for extra in normalized[1:]:
            lines.append(f"  {extra}")
    return "\n".join(lines)


def build_content_section(title: str, items: list[str]) -> list[str]:
    body = format_bullets(items)
    if not body:
        return []

    return [title, body, ""]


def collapse_blank_lines(lines: list[str]) -> list[str]:
    collapsed: list[str] = []
    previous_blank = False

    for line in lines:
        is_blank = line == ""
        if is_blank and previous_blank:
            continue
        collapsed.append(line)
        previous_blank = is_blank

    while collapsed and collapsed[-1] == "":
        collapsed.pop()

    return collapsed


def build_gate_section(args: argparse.Namespace) -> str:
    if args.compact:
        return ""

    if not any([args.gate, args.decision, args.why_now, args.options, args.recommended_option]):
        return ""

    lines = ["## 需要人工确认"]
    if args.gate:
        lines.append(f"- 门禁: {args.gate}")
    if args.decision:
        lines.append(f"- 决策问题: {args.decision}")
    if args.why_now:
        lines.append(f"- 当前原因: {args.why_now}")
    if args.options:
        option_text = "; ".join(args.options)
        lines.append(f"- 可选项: {option_text}")
    if args.recommended_option:
        lines.append(f"- 推荐选项: {args.recommended_option}")

    return "\n".join(lines)


def resolve_compact_title(args: argparse.Namespace) -> str:
    if args.compact_title:
        return args.compact_title

    return COMPACT_TITLE_BY_STAGE.get(args.stage, "关键信息")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, help="Workflow stage name used for full comments or compact title fallback")
    parser.add_argument("--fact", action="append", default=[], help="Confirmed fact bullet")
    parser.add_argument("--fact-file", action="append", default=[], help="File containing confirmed facts")
    parser.add_argument("--hypothesis", action="append", default=[], help="Hypothesis bullet")
    parser.add_argument("--hypothesis-file", action="append", default=[], help="File containing hypotheses")
    parser.add_argument("--unknown", action="append", default=[], help="Unknown or missing evidence bullet")
    parser.add_argument("--unknown-file", action="append", default=[], help="File containing unknowns")
    parser.add_argument("--next-action", action="append", default=[], help="Recommended next action bullet")
    parser.add_argument("--next-action-file", action="append", default=[], help="File containing next actions")
    parser.add_argument("--compact-title", help="Explicit title for compact Jira comments, e.g. 根因分析 / 方案设计 / 已验证")
    parser.add_argument("--gate", help="Gate name such as G1 Fix Plan")
    parser.add_argument("--decision", help="Decision question for human confirmation")
    parser.add_argument("--why-now", help="Why confirmation is needed now")
    parser.add_argument("--option", dest="options", action="append", default=[], help="Selectable options")
    parser.add_argument("--recommended-option", help="Recommended option summary")
    parser.add_argument("--compact", action="store_true", help="Render a Jira-friendly compact comment without gate or next-action sections")
    parser.add_argument("--output", help="Optional output file path; defaults to stdout")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    facts = read_items(args.fact, args.fact_file)
    hypotheses = read_items(args.hypothesis, args.hypothesis_file)
    unknowns = read_items(args.unknown, args.unknown_file)
    next_actions = read_items(args.next_action, args.next_action_file)
    gate_section = build_gate_section(args)

    sections: list[str] = []
    if args.compact:
        sections.extend(build_content_section(f"## {resolve_compact_title(args)}", facts))
    else:
        sections = ["## 阶段", args.stage, ""]
        sections.extend(build_content_section("## 已确认事实", facts))
        sections.extend(build_content_section("## 假设", hypotheses))
        sections.extend(build_content_section("## 未知项 / 缺失证据", unknowns))
        sections.extend(build_content_section("## 建议下一步", next_actions))

    if gate_section:
        sections.extend(["", gate_section])

    rendered = "\n".join(collapse_blank_lines(sections)).rstrip() + "\n"

    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
