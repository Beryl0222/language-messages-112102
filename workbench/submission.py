"""成对语言的提交审核。

屏幕约定：中文和英文来自同一份经认可的语义覆盖。不能一边换了写法、
扩大了适用人群，另一边仍把同一批人排除在外；也不能两边各有条件却
互为补集，让某种来宾在切换语言时看到不同动作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .model import Tables, Template, VisitRequest
from .semantics import (
    Analysis,
    Issue,
    analyze_copy,
    can_auto_deliver,
    issues_for_request,
)
from .selection import (
    BLOCKED,
    FALLBACK as FALLBACK_STATUS,
    READY,
    RequestPlan,
    plan_request,
)


@dataclass
class Coverage:
    """一条模板在人群上的语义覆盖。"""

    template: Template
    analysis: Analysis

    @property
    def chosen_scope(self) -> tuple[object, ...]:
        return self.analysis.scope_key

    @property
    def action(self) -> str:
        return self.analysis.action

    @property
    def addressee(self) -> str | None:
        return self.analysis.addressee

    @property
    def rest_action(self) -> str | None:
        return self.analysis.rest_action


def coverage_of(template: Template) -> Coverage:
    return Coverage(template=template, analysis=analyze_copy(template))


@dataclass
class PairAudit:
    message_id: str
    zh: Template | None
    en: Template | None
    issues: list[Issue] = field(default_factory=list)
    zh_coverage: Coverage | None = None
    en_coverage: Coverage | None = None

    @property
    def same_coverage(self) -> bool:
        if self.zh_coverage is None or self.en_coverage is None:
            return False
        return self.zh_coverage.chosen_scope == self.en_coverage.chosen_scope

    @property
    def error_issues(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def acceptable(self) -> bool:
        """可正式提交：无错误，且两种语言覆盖一致。"""

        return not self.error_issues and self.same_coverage

    @property
    def needs_editorial_review(self) -> bool:
        return bool(self.warnings)

    def excluded_classes(self, tables: Tables, locale: str) -> dict[str, list[str]]:
        """该语言下，哪些来宾类别不能自动使用这条写法，附问题代码。

        只看语义与变量可得性；屏幕宽度由 ``preview_regression`` 逐条预演。
        """

        template = self.zh if locale == "zh" else self.en
        if template is None:
            return {"missing": sorted(tables.classes)}
        analysis = analyze_copy(template)
        excluded: dict[str, list[str]] = {}
        for request in sorted(tables.classes.values(), key=lambda item: item.id):
            issues = issues_for_request(
                template,
                analysis,
                request,
                tables.screen_for_request(request).required_keys,
            )
            blocking = [issue.code for issue in issues if issue.blocking]
            if blocking:
                excluded[request.id] = sorted(set(blocking))
        return excluded


def audit_submission(
    tables: Tables,
    message_id: str,
    zh_revision: str,
    en_revision: str,
) -> PairAudit:
    """核对某消息的一对正式提交。"""

    zh_rows = tables.templates_for(message_id, "zh", zh_revision)
    en_rows = tables.templates_for(message_id, "en", en_revision)
    zh = zh_rows[0] if zh_rows else None
    en = en_rows[0] if en_rows else None
    audit = PairAudit(message_id=message_id, zh=zh, en=en)

    if zh is None:
        audit.issues.append(
            Issue("missing_zh", "error", f"{message_id} 缺少中文修订 {zh_revision}。")
        )
    if en is None:
        audit.issues.append(
            Issue("missing_en", "error", f"{message_id} 缺少英文修订 {en_revision}。")
        )
    if zh is None or en is None:
        return audit

    audit.zh_coverage = coverage_of(zh)
    audit.en_coverage = coverage_of(en)

    screen_keys: set[str] = set()
    for screen in tables.screens.values():
        if message_id in screen.guide_message_ids:
            screen_keys.update(screen.required_keys)
    seen: set[tuple[str, str | None]] = set()
    for template in (zh, en):
        for issue in analyze_copy(template).issues:
            key = (issue.code, issue.slot)
            if key not in seen:
                seen.add(key)
                audit.issues.append(issue)
    # 具体请求才暴露的问题：同一模板同一问题只列一次，并注明受影响的来宾。
    for template in (zh, en):
        analysis = analyze_copy(template)
        if template.is_fallback_copy():
            continue  # 兜底文案不点具体来宾，逐条评估没有附加意义。
        affected: dict[tuple[str, str | None], tuple[Issue, list[str]]] = {}
        for request in tables.classes.values():
            for issue in issues_for_request(
                template, analysis, request, screen_keys
            ):
                key = (issue.code, issue.slot)
                if key in seen:
                    continue
                if key not in affected:
                    affected[key] = (issue, [])
                affected[key][1].append(request.id)
        for issue, request_ids in affected.values():
            seen.add((issue.code, issue.slot))
            audit.issues.append(
                Issue(
                    issue.code,
                    issue.severity,
                    f"{issue.message}（{template.locale} {template.id}，"
                    f"影响 {', '.join(request_ids)}）",
                    issue.slot,
                )
            )

    if not audit.same_coverage:
        audit.issues.append(
            Issue(
                "coverage_divergence",
                "error",
                "两种语言的适用条件、称呼要求、动作或“其余来宾”去向不一致："
                "同一批请求切换语言会被分到不同动作。",
            )
        )

    zh_excluded = audit.excluded_classes(tables, "zh")
    en_excluded = audit.excluded_classes(tables, "en")
    only_zh = sorted(set(zh_excluded) - set(en_excluded))
    only_en = sorted(set(en_excluded) - set(zh_excluded))
    if only_zh or only_en:
        detail = []
        if only_zh:
            detail.append("仅中文排除 " + ", ".join(only_zh))
        if only_en:
            detail.append("仅英文排除 " + ", ".join(only_en))
        audit.issues.append(
            Issue(
                "asymmetric_exclusion",
                "error",
                "两种语言排除的来宾不同（" + "；".join(detail) + "）。",
            )
        )

    if audit.zh_coverage.addressee != audit.en_coverage.addressee:
        audit.issues.append(
            Issue(
                "addressee_divergence",
                "error",
                "两种语言把动作交给不同的受话人。",
            )
        )
    if audit.zh_coverage.rest_action != audit.en_coverage.rest_action:
        audit.issues.append(
            Issue(
                "rest_action_divergence",
                "error",
                "两种语言给“其余来宾”的动作不同。",
            )
        )
    return audit


def latest_revision(tables: Tables, message_id: str, locale: str) -> Template | None:
    rows = tables.templates_for(message_id, locale)
    if not rows:
        return None
    return sorted(rows, key=lambda item: (-item.revision_number, item.id))[0]


def audit_all_submissions(tables: Tables) -> list[PairAudit]:
    """按每条消息的两种最新修订成对审核。"""

    results: list[PairAudit] = []
    for message_id in sorted(tables.message_ids()):
        zh = latest_revision(tables, message_id, "zh")
        en = latest_revision(tables, message_id, "en")
        if zh is not None and en is not None:
            results.append(audit_submission(tables, message_id, zh.revision, en.revision))
        else:
            results.append(
                PairAudit(
                    message_id=message_id,
                    zh=zh,
                    en=en,
                    issues=[
                        Issue(
                            "untranslated_message",
                            "error",
                            f"{message_id} 缺少成对语言，不能作为正式提交。",
                        )
                    ],
                )
            )
    return results


@dataclass
class RequestPreview:
    request: VisitRequest
    old: RequestPlan
    new: RequestPlan

    @property
    def old_signature(self) -> tuple[tuple[str, str | None], ...]:
        return tuple(
            (locale, choice.template.id if choice.template else None)
            for locale, choice in sorted(self.old.choices.items())
        )

    @property
    def new_signature(self) -> tuple[tuple[str, str | None], ...]:
        return tuple(
            (locale, choice.template.id if choice.template else None)
            for locale, choice in sorted(self.new.choices.items())
        )

    @property
    def changed(self) -> bool:
        return (
            self.old_signature != self.new_signature
            or self.old.action != self.new.action
            or self.old.status != self.new.status
        )

    @property
    def regression(self) -> bool:
        """原来能交付（含兜底），现在不能交付。"""

        return self.old.deliverable and not self.new.deliverable

    @property
    def action_changed(self) -> bool:
        return self.old.action != self.new.action


def _tables_with_previous_delivery(tables: Tables, screen) -> Tables:
    """用屏幕旧交付指向的修订重建一份资料，模拟“原来的请求”。"""

    keep: set[tuple[str, str, str]] = set()
    for message_id, revisions in screen.previous_delivery.items():
        for locale, revision in revisions.items():
            keep.add((message_id, locale, revision))
    for template in tables.templates.values():
        if template.message_id == screen.waiting_ref:
            keep.add((template.message_id, template.locale, template.revision))
    filtered = {
        identifier: template
        for identifier, template in tables.templates.items()
        if (template.message_id, template.locale, template.revision) in keep
    }
    return Tables(
        screens=dict(tables.screens),
        field_rules=dict(tables.field_rules),
        templates=filtered,
        classes=dict(tables.classes),
    )


def preview_regression(
    tables: Tables,
    requests: Iterable[VisitRequest] | None = None,
    active_shift: str | None = None,
) -> list[RequestPreview]:
    """同一批合成请求在旧交付与当前修订下的预览对照。"""

    pool = list(requests) if requests is not None else list(tables.classes.values())
    previews: list[RequestPreview] = []
    for request in sorted(pool, key=lambda item: item.id):
        screen = tables.screen_for_request(request)
        old = plan_request(
            _tables_with_previous_delivery(tables, screen),
            request,
            active_shift=active_shift,
        )
        new = plan_request(tables, request, active_shift=active_shift)
        previews.append(RequestPreview(request=request, old=old, new=new))
    return previews


def translation_gaps(tables: Tables, message_id: str) -> dict[str, object]:
    """某条消息在两种语言之间的覆盖缺口。

    既看“条件写法只有一边有”，也看“具体来宾在一边没有可用句”。
    """

    locales = ["zh", "en"]
    conditions: dict[str, set[str]] = {}
    for locale in locales:
        conditions[locale] = {
            (template.applies_to or "all")
            for template in tables.templates_for(message_id, locale)
        }
    only_zh = sorted(conditions["zh"] - conditions["en"])
    only_en = sorted(conditions["en"] - conditions["zh"])

    per_class: dict[str, dict[str, list[str]]] = {}
    for request in sorted(tables.classes.values(), key=lambda item: item.id):
        available: dict[str, list[str]] = {}
        for locale in locales:
            usable: list[str] = []
            for template in tables.templates_for(message_id, locale):
                analysis = analyze_copy(template)
                if template.applies_to == "group_count==0" and request.group_count != 0:
                    continue
                if template.applies_to and "身份称呼" in template.applies_to and request.honorific is None:
                    continue
                issues = issues_for_request(
                    template,
                    analysis,
                    request,
                    tables.screen_for_request(request).required_keys,
                )
                if can_auto_deliver(issues):
                    usable.append(template.id)
            available[locale] = usable
        missing = [locale for locale in locales if not available[locale]]
        if missing:
            per_class[request.id] = {
                "missing_locales": missing,
                "zh": available["zh"],
                "en": available["en"],
            }

    return {
        "message_id": message_id,
        "conditions_only_zh": only_zh,
        "conditions_only_en": only_en,
        "classes_missing_one_side": per_class,
        "symmetric": not only_zh and not only_en and not per_class,
    }


def delivery_report(
    tables: Tables,
    requests: Iterable[VisitRequest] | None = None,
    active_shift: str | None = None,
) -> dict[str, object]:
    """汇总：成对审核、逐条预览、待补译群体、需通知的设备。"""

    from .selection import notify_devices

    pool = list(requests) if requests is not None else list(tables.classes.values())
    plans = [plan_request(tables, request, active_shift=active_shift) for request in pool]
    audits = audit_all_submissions(tables)

    untranslated: dict[str, set[str]] = {}
    for plan in plans:
        for locale in plan.untranslated_locales:
            untranslated.setdefault(plan.request.visit_class, set()).add(locale)

    return {
        "plans": plans,
        "previews": preview_regression(tables, pool, active_shift=active_shift),
        "audits": audits,
        "untranslated": {
            key: sorted(value) for key, value in sorted(untranslated.items())
        },
        "not_deliverable": [
            {
                "request": plan.request.id,
                "visit_class": plan.request.visit_class,
                "screen": plan.screen.id,
                "device": plan.screen.device,
                "status": plan.status,
                "reason": sorted(
                    {issue.code for issue in plan.blocking_issues}
                ),
            }
            for plan in plans
            if not plan.deliverable
        ],
        "editorial_review": [
            {
                "request": plan.request.id,
                "visit_class": plan.request.visit_class,
                "screen": plan.screen.id,
                "status": plan.status,
                "warnings": sorted(
                    {
                        issue.code
                        for choice in plan.choices.values()
                        for issue in choice.issues
                        if issue.severity == "warning"
                    }
                ),
            }
            for plan in plans
            if plan.deliverable and not plan.served
        ],
        "notify_devices": {
            plan.request.id: notify_devices(tables, plan.request) for plan in plans
        },
        "translation_gaps": [
            translation_gaps(tables, message_id)
            for message_id in sorted(tables.message_ids())
        ],
        "missing_fallback": [
            {
                "screen": screen.id,
                "device": screen.device,
                "locale": locale,
            }
            for screen in tables.screens.values()
            for locale in ("zh", "en")
            if not tables.templates_for(screen.waiting_ref, locale)
        ],
    }
