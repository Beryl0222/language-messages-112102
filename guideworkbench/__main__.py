"""命令行入口：只读交换资料，输出核对结果或某次请求的屏幕预览。

用法示例：

    python3 -m guideworkbench review
    python3 -m guideworkbench pairs guide-7
    python3 -m guideworkbench survey
    python3 -m guideworkbench screen S1
    python3 -m guideworkbench request R1
    python3 -m guideworkbench request R3 --shift 晚班
    python3 -m guideworkbench intake
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .bundle import (
    CopyRequest,
    coverage_gaps,
    inspect_copybook,
    morphology_issues,
    resolve_delivery,
)
from .model import DataError, load_workbench
from .review import bilingual_matches, review_copybook
from .survey import plan_screen, survey_classes


def _flag_line(flag) -> str:
    return f"  [{flag.severity}] {flag.code} {flag.message}"


def _review(args: argparse.Namespace) -> int:
    workbench = load_workbench(args.data_dir)
    review = review_copybook(workbench)
    if args.json:
        payload = {
            "variants": {
                copy_id: {
                    "message_id": analysis.variant.message_id,
                    "locale": analysis.variant.locale,
                    "revision": analysis.variant.revision,
                    "deliverable": analysis.deliverable,
                    "scope": analysis.scope.conditions,
                    "scope_label": analysis.scope.label,
                    "pending": list(analysis.pending),
                    "flags": [
                        {"code": f.code, "severity": f.severity, "message": f.message}
                        for f in analysis.flags
                    ],
                    "serviceable_types": list(analysis.serviceable_types),
                }
                for copy_id, analysis in review.analyses.items()
            },
            "messages": {
                message_id: {
                    "clean_pairs": [[p.zh.id, p.en.id] for p in message.clean_pairs],
                    "conditional_pairs": [[p.zh.id, p.en.id] for p in message.conditional_pairs],
                    "gaps": list(coverage_gaps(review, message_id)),
                }
                for message_id, message in review.messages.items()
            },
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"文案 {len(workbench.copybook)} 条，屏幕 {len(workbench.screens)} 台，来宾请求 {len(workbench.requests)} 条")
    for message_id, message in review.messages.items():
        print(f"\n消息 {message_id}")
        for analysis in message.analyses:
            variant = analysis.variant
            status = "可交付" if analysis.deliverable else "需编辑/不可直接交付"
            print(
                f"  {variant.id} {variant.locale}/{variant.revision} "
                f"“{variant.text}” — {status}"
            )
            for flag in analysis.flags:
                print(_flag_line(flag))
            for item in analysis.pending:
                print(f"  [待编辑] {item}")
            if analysis.serviceable_types:
                print(f"  仍可服务的来宾类型: {', '.join(analysis.serviceable_types)}")
        for pair in message.pairs:
            scope = pair.scope.label if pair.scope else "无交集"
            print(f"  句对 {pair.zh.id}↔{pair.en.id}: {pair.status}（范围 {scope}）")
            for reason in pair.reasons:
                print(f"    - {reason}")
        gaps = coverage_gaps(review, message_id)
        if gaps:
            print("  覆盖缺口:")
            for gap in gaps:
                print(f"    - {gap}")
    return 0


def _pairs(args: argparse.Namespace) -> int:
    workbench = load_workbench(args.data_dir)
    review = review_copybook(workbench)
    for pair in bilingual_matches(workbench, args.message_id):
        print(f"{pair.zh.id} ↔ {pair.en.id}: {pair.status}")
        if pair.zh.id in review.analyses:
            print("  中文:", pair.zh.text)
            print("  英文:", pair.en.text)
        if pair.scope is not None:
            print(f"  适用范围: {pair.scope.label}（{', '.join(pair.scope.conditions) or '无限制'}）")
        for item in pair.hard_failures:
            print(f"  [硬失败] {item}")
        for item in pair.soft_failures:
            print(f"  [待编辑] {item}")
        for item in morphology_issues(pair):
            print(f"  [形态] {item}")
        for reason in pair.reasons:
            print(f"  - {reason}")
        print()
    return 0


def _survey(args: argparse.Namespace) -> int:
    workbench = load_workbench(args.data_dir)
    for result in survey_classes(workbench, shift=args.shift):
        print(f"{result.request_id} ({result.visit_class}) @ {result.device}: {result.status}")
        print(f"  动作: {result.action or '无'}；语言一致: {result.consistent_action}")
        for locale, text in sorted(result.shown.items()):
            print(f"  {locale}: {text}")
        for condition in result.triggered_conditions:
            print(f"  条件: {condition}")
        for issue in result.issues:
            print(f"  问题: {issue}")
        print()
    return 0


def _screen(args: argparse.Namespace) -> int:
    workbench = load_workbench(args.data_dir)
    view = plan_screen(workbench, args.screen_id, shift=args.shift)
    print(f"{view.screen_id} {view.device}")
    for visit_class, item in view.assignments.items():
        print(
            f"  {visit_class}: {item.status}；短句 {item.copy_ids}；动作 {item.action}；"
            f"语言一致={item.consistent_action}"
        )
        if item.draft_ids != item.copy_ids and item.draft_ids != (None, None):
            print(f"    草稿句对: {item.draft_ids[0]} ↔ {item.draft_ids[1]}（屏幕不显示）")
        for issue in item.issues:
            print(f"    问题: {issue}")
    for gap in view.gaps:
        print(f"  缺口: {gap}")
    print(f"  退回消息可用: {view.fallback_ok}")
    return 0


def _request(args: argparse.Namespace) -> int:
    workbench = load_workbench(args.data_dir)
    delivery = resolve_delivery(
        workbench,
        CopyRequest.from_visitor_request(
            workbench.request(args.request_id), shift=args.shift
        ),
    )
    print(f"{delivery.request_id} @ {delivery.screen_id}: {delivery.status}（动作 {delivery.action or '无'}）")
    for locale, message in sorted(delivery.messages.items()):
        print(f"  {locale} [{message.status}] {message.text!r} ({message.columns}/{message.limit} 列)")
        for issue in message.issues:
            print(f"    问题: {issue}")
    if delivery.pair is not None:
        print(f"  句对: {delivery.pair.zh.id} ↔ {delivery.pair.en.id} ({delivery.pair.status})")
    for locale, rendered in sorted(delivery.drafts.items()):
        if rendered.text:
            print(f"  草稿 {locale}: {rendered.text!r} ({rendered.columns} 列)")
    for issue in delivery.issues:
        print(f"  问题: {issue}")
    for note in delivery.notes:
        print(f"  说明: {note}")
    for item in delivery.ambiguity:
        print(f"  并存: {item}")
    return 0


def _intake(args: argparse.Namespace) -> int:
    workbench = load_workbench(args.data_dir)
    report = inspect_copybook(workbench, shift=args.shift)
    print(f"预览可用: {report.all_previewable}；路线交付: {report.all_route_delivered}")
    print(f"需要通知的设备: {', '.join(report.devices_to_notify) or '无'}")
    for row in report.rows:
        print(f"  {row.request_id} ({row.visit_class}) {row.before_status} → {row.after_status}")
        for issue in row.issues:
            print(f"    问题: {issue}")
    for item in report.unsuitable:
        print(f"  不适合交付: {item}")
    if report.devices_to_notify_names:
        print(f"  设备名称: {', '.join(report.devices_to_notify_names)}")
    for item in report.unsuitable_pairs:
        print(f"  组合不适合交付: {item.label} [{item.status}]")
    for gap in report.pending_translation_groups:
        print(f"  待补译: {gap}")
    for screen_id, ok in report.fallback_ok_by_screen.items():
        print(f"  退回消息 {screen_id}: {'可用' if ok else '不可用'}")
    for note in report.notes:
        print(f"  说明: {note}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="guideworkbench", description=__doc__)
    parser.add_argument("--data-dir", default=None, help="交换资料目录，默认 data/")
    subparsers = parser.add_subparsers(dest="command", required=True)

    review = subparsers.add_parser("review", help="核对全部短句与句对")
    review.add_argument("--json", action="store_true", help="输出 JSON")
    review.set_defaults(func=_review)

    pairs = subparsers.add_parser("pairs", help="列出某消息的全部中英组合")
    pairs.add_argument("message_id")
    pairs.set_defaults(func=_pairs)

    survey = subparsers.add_parser("survey", help="按访客类别合成请求并预览")
    survey.add_argument("--shift")
    survey.set_defaults(func=_survey)

    screen = subparsers.add_parser("screen", help="预览一台屏幕下各类来宾的选择")
    screen.add_argument("screen_id")
    screen.add_argument("--shift")
    screen.set_defaults(func=_screen)

    request = subparsers.add_parser("request", help="预览一条实际请求")
    request.add_argument("request_id")
    request.add_argument("--shift")
    request.set_defaults(func=_request)

    intake = subparsers.add_parser("intake", help="核对现有请求的预览与缺口")
    intake.add_argument("--shift")
    intake.set_defaults(func=_intake)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except DataError as exc:
        print(f"资料错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
