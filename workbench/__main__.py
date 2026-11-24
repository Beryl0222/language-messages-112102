"""本地双语文案工作台命令行。

    python3 -m workbench [--data DIR] [--shift 班次] [--json] <command>

命令只读资料；文本输出供编校员阅读，``--json`` 供存档和回归比对。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from .model import Tables, display_width, load_requests, load_tables
from .selection import plan_requests
from .semantics import analyze_copy, served_classes
from .submission import (
    audit_all_submissions,
    audit_submission,
    delivery_report,
    preview_regression,
)


def _issue_row(issue) -> dict[str, Any]:
    return {
        "code": issue.code,
        "severity": issue.severity,
        "message": issue.message,
        "slot": issue.slot,
    }


def _template_row(tables: Tables, template) -> dict[str, Any]:
    analysis = analyze_copy(template)
    screen_keys: set[str] = set()
    for screen in tables.screens.values():
        if template.message_id in screen.guide_message_ids:
            screen_keys.update(screen.required_keys)
    served, editorial = served_classes(
        template, analysis, tables.classes.values(), screen_keys
    )
    return {
        "id": template.id,
        "message_id": template.message_id,
        "locale": template.locale,
        "revision": template.revision,
        "text": template.text,
        "width": display_width(template.text),
        "placeholders": list(template.placeholders),
        "applies_to": template.applies_to,
        "role_hint": template.role_hint,
        "addressee": analysis.addressee,
        "rest_scope": analysis.rest_scope,
        "rest_action": analysis.rest_action,
        "action": analysis.action,
        "slots": {
            name: {
                "theme": reading.theme,
                "function": reading.function,
                "affects_action": reading.affects_action,
                "governs": list(reading.governs),
                "modified_by": list(reading.modified_by),
                "evidence": list(reading.evidence),
            }
            for name, reading in sorted(analysis.slots.items())
        },
        "issues": [_issue_row(issue) for issue in analysis.issues],
        "needs_editor": analysis.is_ambiguous
        or any(issue.severity == "warning" for issue in analysis.issues),
        "served_classes": served,
        "editorial_classes": [
            {
                "request": request_id,
                "reasons": [_issue_row(issue) for issue in issues],
            }
            for request_id, issues in editorial
        ],
    }


def cmd_templates(tables: Tables, args: argparse.Namespace) -> Any:
    rows = []
    for template in tables.templates.values():
        if args.message and template.message_id != args.message:
            continue
        if args.locale and template.locale != args.locale:
            continue
        rows.append(_template_row(tables, template))
    rows.sort(
        key=lambda row: (row["message_id"], row["locale"], row["revision"], row["id"])
    )
    return {"templates": rows}


def _pool(tables: Tables, args: argparse.Namespace):
    if getattr(args, "requests", None):
        extra = load_requests(args.requests)
        clashes = {request.id for request in extra} & set(tables.classes)
        if clashes:
            raise SystemExit("合成请求 id 与既有类别重复: " + ", ".join(sorted(clashes)))
        for request in extra:
            tables.classes[request.id] = request
        return extra
    return None


def cmd_classes(tables: Tables, args: argparse.Namespace) -> Any:
    plans = plan_requests(
        tables, _pool(tables, args), active_shift=args.shift
    )
    from .selection import notify_devices

    return {
        "plans": [
            {
                "request": plan.request.id,
                "visit_class": plan.request.visit_class,
                "device": plan.screen.id,
                "device_name": plan.screen.device,
                "status": plan.status,
                "served": plan.served,
                "deliverable": plan.deliverable,
                "action": plan.action,
                "action_consistent": plan.action_consistent,
                "fallback": plan.fallback,
                "locales": {
                    locale: {
                        "template": choice.template.id if choice.template else None,
                        "revision": choice.template.revision if choice.template else None,
                        "text": choice.text,
                        "width": choice.width,
                        "max_width": plan.screen.max_width(locale),
                        "fallback": choice.fallback,
                        "fallback_reason": choice.fallback_reason,
                        "condition": choice.condition,
                        "action": choice.action,
                        "issues": [_issue_row(issue) for issue in choice.issues],
                    }
                    for locale, choice in sorted(plan.choices.items())
                },
                "issues": [_issue_row(issue) for issue in plan.issues],
                "notify_devices": list(notify_devices(tables, plan.request)),
            }
            for plan in plans
        ]
    }


def cmd_audit(tables: Tables, args: argparse.Namespace) -> Any:
    if args.zh or args.en:
        if not (args.message and args.zh and args.en):
            raise SystemExit("指定修订审核时需同时给 --message、--zh、--en")
        rows = [audit_submission(tables, args.message, args.zh, args.en)]
    else:
        rows = audit_all_submissions(tables)
        if args.message:
            rows = [row for row in rows if row.message_id == args.message]
    return {
        "audits": [
            {
                "message_id": row.message_id,
                "zh": row.zh.id if row.zh else None,
                "en": row.en.id if row.en else None,
                "acceptable": row.acceptable,
                "same_coverage": row.same_coverage,
                "needs_editorial_review": row.needs_editorial_review,
                "issues": [_issue_row(issue) for issue in row.issues],
                "zh_excluded": row.excluded_classes(tables, "zh"),
                "en_excluded": row.excluded_classes(tables, "en"),
            }
            for row in rows
        ]
    }


def cmd_preview(tables: Tables, args: argparse.Namespace) -> Any:
    pool = _pool(tables, args)
    return {
        "previews": [
            {
                "request": preview.request.id,
                "visit_class": preview.request.visit_class,
                "changed": preview.changed,
                "regression": preview.regression,
                "action_changed": preview.action_changed,
                "old": preview.old.summary(),
                "new": preview.new.summary(),
                "old_status": preview.old.status,
                "new_status": preview.new.status,
                "old_action": preview.old.action,
                "new_action": preview.new.action,
                "old_served": preview.old.served,
                "new_served": preview.new.served,
            }
            for preview in preview_regression(
                tables, pool, active_shift=args.shift
            )
        ]
    }


def cmd_report(tables: Tables, args: argparse.Namespace) -> Any:
    report = delivery_report(
        tables, _pool(tables, args), active_shift=args.shift
    )
    return {
        "plans": [
            {
                "request": plan.request.id,
                "visit_class": plan.request.visit_class,
                "device": plan.screen.id,
                "status": plan.status,
                "served": plan.served,
                "action": plan.action,
                "summary": plan.summary(),
            }
            for plan in report["plans"]
        ],
        "previews": [
            {
                "request": preview.request.id,
                "regression": preview.regression,
                "action_changed": preview.action_changed,
            }
            for preview in report["previews"]
        ],
        "audits": [
            {
                "message_id": audit.message_id,
                "acceptable": audit.acceptable,
                "issues": [_issue_row(issue) for issue in audit.issues],
            }
            for audit in report["audits"]
        ],
        "untranslated": report["untranslated"],
        "not_deliverable": report["not_deliverable"],
        "editorial_review": report["editorial_review"],
        "notify_devices": report["notify_devices"],
        "translation_gaps": report["translation_gaps"],
        "missing_fallback": report["missing_fallback"],
    }


def _print_text(command: str, payload: Any) -> None:
    if command == "templates":
        for row in payload["templates"]:
            print(
                f"{row['id']} {row['message_id']}/{row['locale']}/{row['revision']} "
                f"addressee={row['addressee']} rest={row['rest_scope']}"
                f"/{row['rest_action']}"
            )
            for name, slot in row["slots"].items():
                print(
                    f"  {{{name}}}: {slot['function']} "
                    f"(主题 {slot['theme']}, 决定动作={slot['affects_action']})"
                    + (f" 修饰 {slot['modified_by']}" if slot["modified_by"] else "")
                    + (f" 支配 {slot['governs']}" if slot["governs"] else "")
                )
            for issue in row["issues"]:
                print(f"  [{issue['severity']}] {issue['code']}: {issue['message']}")
            if row["needs_editor"]:
                print(f"  可自动服务: {row['served_classes'] or '无'}")
                for item in row["editorial_classes"]:
                    reasons = ", ".join(
                        sorted({issue["code"] for issue in item["reasons"]})
                    )
                    print(f"  交编辑: {item['request']} ({reasons})")
    elif command == "classes":
        for plan in payload["plans"]:
            print(
                f"{plan['request']} ({plan['visit_class']}) {plan['device_name']} "
                f"[{plan['status']}] action={plan['action']} "
                f"notify={plan['notify_devices']}"
            )
            for locale, choice in sorted(plan["locales"].items()):
                label = "FALLBACK" if choice["fallback"] else choice["template"]
                reason = (
                    f" ({choice['fallback_reason']})" if choice["fallback_reason"] else ""
                )
                print(
                    f"  {locale}: {label}{reason} {choice['width']}/{choice['max_width']} "
                    f"condition={choice['condition']} {choice['text']}"
                )
                for issue in choice["issues"]:
                    print(f"    [{issue['severity']}] {issue['code']}: {issue['message']}")
            for issue in plan["issues"]:
                print(f"  [{issue['severity']}] {issue['code']}: {issue['message']}")
    elif command == "audit":
        for row in payload["audits"]:
            print(
                f"{row['message_id']} zh={row['zh']} en={row['en']} "
                f"acceptable={row['acceptable']} same_coverage={row['same_coverage']}"
            )
            print(f"  zh 不可自动用: {row['zh_excluded'] or '无'}")
            print(f"  en 不可自动用: {row['en_excluded'] or '无'}")
            for issue in row["issues"]:
                print(f"  [{issue['severity']}] {issue['code']}: {issue['message']}")
    elif command == "preview":
        for row in payload["previews"]:
            print(
                f"{row['request']} changed={row['changed']} "
                f"regression={row['regression']} action_changed={row['action_changed']}"
            )
            print(f"  old: {row['old']}")
            print(f"  new: {row['new']}")
    elif command == "report":
        for plan in payload["plans"]:
            print(plan["summary"])
        print("previews:", [
            (row["request"], row["regression"], row["action_changed"])
            for row in payload["previews"]
        ])
        print("untranslated:", payload["untranslated"])
        print("not_deliverable:", payload["not_deliverable"])
        print("editorial_review:", payload["editorial_review"])
        print("notify:", payload["notify_devices"])
        print("gaps:", payload["translation_gaps"])
        print("missing_fallback:", payload["missing_fallback"])
    else:  # pragma: no cover
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workbench", description=__doc__)
    parser.add_argument("--data", default=None, help="资料目录，默认 data/")
    parser.add_argument("--shift", default=None, help="当前班次，用于 from 类别的可用性")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    json_flag = argparse.ArgumentParser(add_help=False)
    json_flag.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    json_flag.add_argument(
        "--requests",
        default=None,
        help="额外的合成请求文件（与 classes.json 同结构）",
    )

    templates = subparsers.add_parser(
        "templates", help="逐条标注短句里的变量", parents=[json_flag]
    )
    templates.add_argument("--message", default=None)
    templates.add_argument("--locale", default=None, choices=["zh", "en"])

    subparsers.add_parser(
        "classes", help="按合成请求预览屏幕文案", parents=[json_flag]
    )

    audit = subparsers.add_parser(
        "audit", help="成对语言的覆盖审核", parents=[json_flag]
    )
    audit.add_argument("--message", default=None)
    audit.add_argument("--zh", default=None, help="中文修订号，例如 r2")
    audit.add_argument("--en", default=None, help="英文修订号，例如 r5")

    subparsers.add_parser(
        "preview", help="旧交付与新修订的预览对照", parents=[json_flag]
    )
    subparsers.add_parser("report", help="交付总表", parents=[json_flag])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    tables = load_tables(args.data)
    handlers = {
        "templates": cmd_templates,
        "classes": cmd_classes,
        "audit": cmd_audit,
        "preview": cmd_preview,
        "report": cmd_report,
    }
    payload = handlers[args.command](tables, args)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_text(args.command, payload)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
