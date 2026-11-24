"""为一条实际来宾请求决定屏幕上该出现哪条短句。

选择是确定性的：同一请求、同一屏幕永远得到同一结果；两种语言不能
各自选到动作不同的短句。放不下时整条请求退回独立兜底文案，绝不截断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .model import Screen, Tables, Template, VisitRequest, display_width
from .semantics import Analysis, Issue, analyze_copy, can_auto_deliver, issues_for_request


ACTION_BY_TEMPLATE = {
    "guide-7": "guide_led_guest_to_room",
}
FALLBACK_ACTION = "ask_help_desk"

# 交付状态，从可直接投放到完全不可交付。
READY = "ready"
EDITOR_REVIEW = "editor_review"
FALLBACK = "fallback"
BLOCKED = "blocked"


class TemplateRenderError(ValueError):
    """模板与请求的变量不能拼成一条可显示文案。"""


def render(template: Template, request: VisitRequest) -> str:
    """用请求值填写模板；没有同意称呼时，不渲染需要称呼的模板。"""

    values = request.values
    missing = [name for name in template.placeholders if name not in values]
    if missing:
        raise TemplateRenderError(
            f"{template.id} 需要请求中没有的变量: {', '.join(missing)}"
        )
    if "honorific" in template.placeholders and values["honorific"] is None:
        raise TemplateRenderError(
            f"{template.id} 需要称呼，但来宾没有同意显示身份称呼"
        )
    text = template.text
    for name in template.placeholders:
        text = text.replace("{" + name + "}", str(values[name]))
    return text


def _available(template: Template, request: VisitRequest) -> bool:
    values = request.values
    return all(values.get(name) is not None for name in template.placeholders)


def _condition_matches(template: Template, request: VisitRequest) -> bool:
    applies = (template.applies_to or "").strip()
    if not applies:
        return True
    if applies == "group_count==0":
        return request.group_count == 0
    if "身份称呼" in applies or "honorific" in applies.lower():
        return request.honorific is not None
    return False


@dataclass(frozen=True)
class Candidate:
    template: Template
    analysis: Analysis
    text: str
    width: int
    issues: tuple[Issue, ...] = ()

    @property
    def blocking_count(self) -> int:
        return len([issue for issue in self.issues if issue.blocking])

    @property
    def condition_rank(self) -> int:
        return 1 if self.template.applies_to else 0

    @property
    def rank(self) -> tuple[int, int, int, str]:
        """条件匹配的具体写法优先，其次取最新的经认可修订。

        有警告的修订不会被悄悄跳过——它仍会被选中并标为待编辑，
        否则临时更正的文案会被旧稿掩盖。
        """

        return (
            self.condition_rank,
            self.template.revision_number,
            -self.blocking_count,
            self.template.id,
        )


@dataclass
class LocaleChoice:
    locale: str
    template: Template | None
    text: str
    width: int
    analysis: Analysis | None
    fallback: bool
    issues: list[Issue] = field(default_factory=list)
    fallback_reason: str | None = None
    """无可用导览句而退到兜底的原因：no_copy / too_wide / mixed_language。"""

    @property
    def condition(self) -> str | None:
        return self.template.applies_to if self.template else None

    @property
    def action(self) -> str:
        if self.fallback or self.template is None:
            return FALLBACK_ACTION
        return ACTION_BY_TEMPLATE.get(self.template.message_id, "unknown")

    @property
    def needs_editor(self) -> bool:
        return any(issue.severity == "warning" for issue in self.issues)


@dataclass
class RequestPlan:
    request: VisitRequest
    screen: Screen
    choices: dict[str, LocaleChoice]
    status: str = BLOCKED
    action: str | None = None
    issues: list[Issue] = field(default_factory=list)

    @property
    def fallback(self) -> bool:
        return self.status == FALLBACK

    @property
    def deliverable(self) -> bool:
        return self.status != BLOCKED

    @property
    def served(self) -> bool:
        """可以直接交付，不需要编辑再看。"""

        return self.status in (READY, FALLBACK)

    @property
    def action_consistent(self) -> bool:
        return len({choice.action for choice in self.choices.values()}) <= 1

    @property
    def untranslated_locales(self) -> list[str]:
        return sorted(
            locale
            for locale, choice in self.choices.items()
            if choice.fallback or choice.template is None
        )

    @property
    def blocking_issues(self) -> list[Issue]:
        own = [issue for issue in self.issues if issue.blocking]
        for choice in self.choices.values():
            own.extend(issue for issue in choice.issues if issue.blocking)
        return own

    def summary(self) -> str:
        parts = [
            f"{self.request.id} -> {self.screen.id} {self.screen.device} [{self.status}]"
        ]
        for locale, choice in sorted(self.choices.items()):
            label = (
                "兜底"
                if choice.fallback
                else f"{choice.template.message_id}/{choice.template.revision}"
            )
            parts.append(
                f"{locale}={label} ({choice.width}/{self.screen.max_width(locale)})"
            )
        parts.append(f"action={self.action or 'undetermined'}")
        return " | ".join(parts)


def _locales_for_screen(screen: Screen, tables: Tables) -> list[str]:
    locales: set[str] = set()
    for message_id in screen.guide_message_ids:
        locales.update(tables.locales_for(message_id))
    for template in tables.templates.values():
        if template.message_id == screen.waiting_ref:
            locales.add(template.locale)
    # 设备没给宽度的语言不能投放，也不该假装有文案。
    return sorted(locales & set(screen.supported_locales))


def _candidates(
    tables: Tables, screen: Screen, request: VisitRequest, locale: str
) -> list[Candidate]:
    result: list[Candidate] = []
    for template in tables.templates.values():
        if template.locale != locale:
            continue
        if template.message_id not in screen.guide_message_ids:
            continue
        if not _condition_matches(template, request):
            continue
        if not _available(template, request):
            continue
        analysis = analyze_copy(template)
        try:
            text = render(template, request)
        except TemplateRenderError:
            continue
        issues = tuple(
            issues_for_request(template, analysis, request, screen.required_keys)
        )
        result.append(
            Candidate(
                template=template,
                analysis=analysis,
                text=text,
                width=display_width(text),
                issues=issues,
            )
        )
    return result


def _fallback_choice(tables: Tables, screen: Screen, locale: str) -> LocaleChoice:
    templates = tables.templates_for(screen.waiting_ref, locale)
    if not templates:
        return LocaleChoice(
            locale=locale,
            template=None,
            text="",
            width=0,
            analysis=None,
            fallback=True,
            issues=[
                Issue(
                    "missing_fallback",
                    "error",
                    f"{screen.id} 没有 {locale} 的独立兜底文案，该请求无法交付。",
                )
            ],
        )
    template = sorted(templates, key=lambda item: (-item.revision_number, item.id))[0]
    width = display_width(template.text)
    issues: list[Issue] = []
    if width > screen.max_width(locale):
        issues.append(
            Issue(
                "fallback_too_wide",
                "error",
                f"兜底文案超过 {screen.id} 的 {locale} 宽度 "
                f"{screen.max_width(locale)}，该请求无安全可显示内容。",
            )
        )
    return LocaleChoice(
        locale=locale,
        template=template,
        text=template.text,
        width=width,
        analysis=analyze_copy(template),
        fallback=True,
        issues=issues,
    )


def _choose_locale(
    tables: Tables, screen: Screen, request: VisitRequest, locale: str
) -> tuple[LocaleChoice, list[Issue]]:
    """返回该语言的可用选择，以及被排除的候选原因。"""

    notes: list[Issue] = []
    usable: list[Candidate] = []
    saw_candidate = False
    saw_blocking = False
    for candidate in _candidates(tables, screen, request, locale):
        saw_candidate = True
        if candidate.width > screen.max_width(locale):
            notes.append(
                Issue(
                    "too_wide",
                    "note",
                    f"{candidate.template.id} 展开后 {candidate.width} 列，超过 "
                    f"{screen.id} 的 {locale} 上限 {screen.max_width(locale)}。",
                )
            )
            continue
        if not can_auto_deliver(candidate.issues):
            saw_blocking = True
            continue
        usable.append(candidate)

    if not usable:
        choice = _fallback_choice(tables, screen, locale)
        if not choice.issues:
            choice.fallback_reason = (
                "too_wide" if saw_candidate and not saw_blocking else "no_copy"
            )
        return choice, notes

    usable.sort(key=lambda item: item.rank, reverse=True)
    best = usable[0]
    tied = [other for other in usable[1:] if other.rank[:3] == best.rank[:3]]
    if tied:
        return (
            LocaleChoice(
                locale=locale,
                template=None,
                text="",
                width=0,
                analysis=None,
                fallback=False,
                issues=[
                    Issue(
                        "ambiguous_variant",
                        "error",
                        "同一条件下有 "
                        + "、".join(item.template.id for item in [best, *tied])
                        + " 多条同等可用的写法，无法唯一确定该用哪一条。",
                    )
                ],
            ),
            notes,
        )
    return (
        LocaleChoice(
            locale=locale,
            template=best.template,
            text=best.text,
            width=best.width,
            analysis=best.analysis,
            fallback=False,
            issues=list(best.issues),
        ),
        notes,
    )


def notify_devices(tables: Tables, request: VisitRequest) -> tuple[str, ...]:
    """该请求会影响哪些屏幕：本屏，加上正在投放同一消息的屏。"""

    screen = tables.screen_for_request(request)
    devices = {screen.device}
    for message_id in screen.guide_message_ids:
        devices.update(tables.devices_for_message(message_id))
    return tuple(sorted(devices))


def plan_request(
    tables: Tables,
    request: VisitRequest,
    active_shift: str | None = None,
) -> RequestPlan:
    """对一条合成请求给出屏幕文案、状态和需要通知的设备。"""

    screen = tables.screen_for_request(request)
    issues: list[Issue] = []
    if not request.available(active_shift):
        issues.append(
            Issue(
                "class_not_active",
                "error",
                f"{request.visit_class} 类别从 {request.from_shift} 起才允许发送；"
                f"当前班次 {active_shift or '未提供'} 不能交付。",
            )
        )

    choices: dict[str, LocaleChoice] = {}
    for locale in _locales_for_screen(screen, tables):
        choice, notes = _choose_locale(tables, screen, request, locale)
        choices[locale] = choice
        issues.extend(notes)

    if not choices:
        issues.append(
            Issue("no_locale", "error", f"{screen.id} 没有任何语言可投放。")
        )

    # 两种语言若来自不同的覆盖（条件不同），人群范围在切换语言时会变。
    # 只有一种语言可投放时不存在“切换语言”问题。
    active = [
        choice for choice in choices.values() if not choice.fallback
    ]
    scopes = {
        choice.analysis.scope_key
        for choice in active
        if choice.analysis is not None
    }
    if len(active) > 1 and len(scopes) > 1:
        issues.append(
            Issue(
                "language_scope_mismatch",
                "warning",
                "两种语言选用的短句不来自同一份语义覆盖（适用条件或称呼要求不同）；"
                "切换语言会改变谁被覆盖。",
            )
        )

    primary_actions = {
        choice.action
        for choice in choices.values()
        if not choice.fallback and choice.template is not None
    }
    if len(primary_actions) > 1:
        issues.append(
            Issue(
                "language_action_mismatch",
                "error",
                "两种语言会给出不同的动作，不能交付。",
            )
        )

    has_fallback = any(choice.fallback for choice in choices.values())
    if primary_actions and has_fallback:
        # 不能一边引导去房间、一边让人去服务台；整条请求统一兜底。
        for locale in choices:
            previous = choices[locale]
            choices[locale] = _fallback_choice(tables, screen, locale)
            if not previous.fallback:
                choices[locale].fallback_reason = "mixed_language"
        issues.append(
            Issue(
                "mixed_language_fallback",
                "warning",
                "至少一种语言放不下或没有可用写法；整条请求统一改用服务台兜底，"
                "避免切换语言得到不同动作。",
            )
        )

    plan_errors = [issue for issue in issues if issue.blocking]
    has_text = all(choice.text for choice in choices.values()) and bool(choices)
    if not has_text and not plan_errors:
        plan_errors.append(
            Issue("no_deliverable_copy", "error", "该请求没有任何可显示的文案。")
        )
        issues.extend(plan_errors)

    if any(choice.fallback for choice in choices.values()):
        action: str | None = FALLBACK_ACTION
    else:
        action = primary_actions.pop() if primary_actions else None

    if plan_errors or not has_text:
        status = BLOCKED
    elif any(choice.fallback for choice in choices.values()):
        status = FALLBACK
    elif any(choice.needs_editor for choice in choices.values()):
        status = EDITOR_REVIEW
    else:
        status = READY

    plan = RequestPlan(
        request=request,
        screen=screen,
        choices=choices,
        status=status,
        action=action,
        issues=issues,
    )
    if not plan.action_consistent:
        plan.status = BLOCKED
        plan.action = None
    return plan


def plan_requests(
    tables: Tables,
    requests: Iterable[VisitRequest] | None = None,
    active_shift: str | None = None,
) -> list[RequestPlan]:
    pool = list(requests) if requests is not None else list(tables.classes.values())
    return [
        plan_request(tables, request, active_shift=active_shift)
        for request in sorted(pool, key=lambda item: item.id)
    ]
