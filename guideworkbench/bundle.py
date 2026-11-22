"""按屏幕与来宾请求组装可交付文案，并生成正式提交与变更核对报告。

交付规则是保守的：语义未对齐、范围无法确定、宽度不够或缺少安全回退时，
屏幕只显示独立的通用服务台指引；草稿仍保留给编辑，但不会冒充已批准的
路线指引。目的地在交付前逐字核对，避免把备用提示中的地点当成授权目的地。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence
import re

from .model import CopyVariant, VisitorRequest, Workbench, validate_copybook
from .render import (
    DEFAULT_POLICY,
    RenderPolicy,
    Rendered,
    check_fallback_text,
    render_text,
    template_fields,
)
from .review import (
    ALL_GUEST_TYPES,
    BilingualPair,
    CopyAnalysis,
    Review,
    Scope,
    DEFAULT_SEMANTICS,
    Semantics,
    analyze_copy,
    bilingual_matches,
    review_copybook,
)


ROUTE_PREFIX = "route_to_room:"
FALLBACK_ACTION = "ask_help_desk"

# 同一覆盖范围内若有多个可用句对，按此顺序确定唯一交付；数字小者优先。
# 只在语义等价且范围完全相同时使用，不代替核对。
VARIANT_PRIORITY: dict[str, int] = {
    "V1": 10,
    "V2": 60,
    "V3": 50,
    "V4": 20,
    "V5": 70,
    "V8": 5,
    "V9": 15,
}

# 需要重新写文案、不能用编辑签署放行的形态问题。
PARENTHETICAL_PLURAL = re.compile(r"\(\w+\)")


@dataclass(frozen=True)
class Approval:
    """编校员对某个覆盖范围的显式签署。

    scope_note 只能确认第二句是否指同一剩余来宾群体；不能用来放行同行
    人数、覆盖人群或称呼词位的差异，也不能放行需要改文案的形态问题。
    """

    zh_id: str
    en_id: str
    scope_note: str

    def matches(self, pair: BilingualPair) -> bool:
        return pair.zh.id == self.zh_id and pair.en.id == self.en_id


def _approval_for(pair: BilingualPair, approvals: Iterable[Approval]) -> Approval | None:
    for approval in approvals:
        if approval.matches(pair):
            return approval
    return None


def _approval_usable(pair: BilingualPair, approval: Approval | None) -> bool:
    """范围签署只确认第二句是否指同一剩余来宾群体。

    它不能覆盖两种语言覆盖范围不同、人数读法不同或称呼词位缺失的问题；
    否则切换语言时仍可能把某类来宾导向不同动作。
    """
    if approval is None:
        return False
    if pair.scope is None or not pair.scope.conditions:
        return False
    if pair.zh_analysis.scope != pair.en_analysis.scope:
        return False
    return True


def morphology_issues(pair: BilingualPair) -> tuple[str, ...]:
    """一处替换无法覆盖英文单复数/冠词时，必须另写短句而不是照发。"""
    issues: list[str] = []
    for variant in (pair.zh, pair.en):
        for match in PARENTHETICAL_PLURAL.finditer(variant.text):
            issues.append(
                f"{variant.label}: 用 {match.group(0)!r} 兼写单复数；"
                "0/1/多人的英文形式无法由同一次替换覆盖，需按人数另写短句"
            )
    return tuple(issues)


# ---------------------------------------------------------------------------
# 请求对象
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CopyRequest:
    id: str
    device: str
    visit_class: str
    values: Mapping[str, Any]
    shift: str | None = None
    allowed_name: str | None = None
    source: VisitorRequest | None = None

    @classmethod
    def from_visitor_request(
        cls,
        request: VisitorRequest,
        shift: str | None = None,
        allowed_name: str | None = None,
    ) -> "CopyRequest":
        return cls(
            id=request.id,
            device=request.device,
            visit_class=request.visit_class,
            values=request.values(allowed_name),
            shift=shift,
            allowed_name=allowed_name,
            source=request,
        )

    @property
    def available(self) -> bool:
        if self.source is None:
            return True
        return self.source.identity_is_settled(self.shift)

    @property
    def group_count(self) -> int | None:
        try:
            return int(self.values.get("group_count"))
        except (TypeError, ValueError):
            return None

    @property
    def visit_class_known(self) -> bool:
        return self.visit_class in ALL_GUEST_TYPES

    def forbidden_values(self) -> tuple[str, ...]:
        return tuple(
            str(self.values[key])
            for key in ("primary", "room", "guide")
            if self.values.get(key) not in (None, "")
        )


def request_from_id(
    workbench: Workbench,
    request_id: str,
    shift: str | None = None,
    allowed_name: str | None = None,
) -> CopyRequest:
    return CopyRequest.from_visitor_request(workbench.request(request_id), shift, allowed_name)


# ---------------------------------------------------------------------------
# 选择
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PairScore:
    pair: BilingualPair
    applicable: bool
    reason: str

    @property
    def triggered(self) -> tuple[str, ...]:
        scope = self.pair.joint_scope
        if scope is None:
            return ()
        return scope.conditions or ("无附加条件",)


def _pair_sort_key(pair: BilingualPair) -> tuple[Any, ...]:
    # 同等范围里，先取两种语言覆盖一致、无需编辑的完整句对。
    same_scope = int(pair.zh_analysis.scope == pair.en_analysis.scope)
    status_rank = {"match": 0, "conditional": 1}.get(pair.status, 2)
    status_rank = (1 - same_scope, status_rank)
    soft = len(pair.soft_failures)
    morphology = len(morphology_issues(pair))
    pending = len(pair.zh_analysis.pending) + len(pair.en_analysis.pending)
    priority = VARIANT_PRIORITY.get(pair.zh.id, 50) + VARIANT_PRIORITY.get(pair.en.id, 50)
    return (status_rank, soft, morphology, pending, priority, pair.zh.id, pair.en.id)


def score_pairs(
    review: Review,
    request: CopyRequest,
    message_id: str,
    policy: RenderPolicy = DEFAULT_POLICY,
) -> tuple[list[PairScore], list[PairScore]]:
    """返回 (可用句对, 全部句对)。可用 = 无硬失败且范围被本次请求满足。"""
    scores: list[PairScore] = []
    usable: list[PairScore] = []
    for pair in review.message(message_id).pairs:
        scope = pair.joint_scope
        if pair.status == "mismatch" or not pair.usable or scope is None:
            reason = "；".join(pair.hard_failures) or "句对不等价"
            scores.append(PairScore(pair, False, reason))
            continue
        if not scope.satisfied(
            request.values, neutral_allowed=policy.neutral_honorific_approved
        ):
            scores.append(
                PairScore(
                    pair,
                    False,
                    f"本请求不落在“{scope.label}”范围（{', '.join(scope.conditions) or '无限制'}）",
                )
            )
            continue
        score = PairScore(pair, True, f"适用于“{scope.label}”")
        scores.append(score)
        usable.append(score)
    return usable, scores


def _minimal_scopes(usable: Sequence[PairScore]) -> tuple[list[Scope], bool]:
    """找出范围上最具体的等价类。返回 (最小范围, 是否互不可比)。"""
    scopes: list[Scope] = []
    for score in usable:
        scope = score.pair.joint_scope
        assert scope is not None
        if scope not in scopes:
            scopes.append(scope)
    minimal = [
        scope
        for scope in scopes
        if not any(other.subsumes(scope) for other in scopes if other != scope)
    ]
    return minimal, len(minimal) > 1


@dataclass(frozen=True)
class Selection:
    pair: BilingualPair | None
    ambiguity: tuple[str, ...]
    all_scores: tuple[PairScore, ...]
    usable: tuple[PairScore, ...]

    @property
    def scope(self) -> Scope | None:
        return self.pair.joint_scope if self.pair else None


def select_pair(
    workbench: Workbench,
    review: Review,
    request: CopyRequest,
    message_id: str = "guide-7",
    policy: RenderPolicy = DEFAULT_POLICY,
) -> Selection:
    """为一个实际请求确定唯一的句对。

    规则：先排除硬失败；再要求范围被满足；在可用的范围中取最具体者。
    若剩两个互不可比的范围都能覆盖本次请求，就不猜，交给编辑。
    """
    usable, all_scores = score_pairs(review, request, message_id, policy)
    if not usable:
        return Selection(None, (), tuple(all_scores), ())

    minimal, incomparable = _minimal_scopes(usable)
    ambiguity: list[str] = []
    if incomparable:
        by_scope: dict[Scope, list[str]] = {}
        for score in usable:
            if score.pair.joint_scope in minimal:
                by_scope.setdefault(score.pair.joint_scope, []).append(score.pair.label)
        ambiguity.append(
            "多个互不可比的覆盖范围同时适用于本请求: "
            + "；".join(
                f"{scope.label}（{', '.join(labels)}）"
                for scope, labels in by_scope.items()
            )
        )
        return Selection(None, tuple(ambiguity), tuple(all_scores), tuple(usable))

    scope = minimal[0]
    in_scope = [
        score for score in usable if score.pair.joint_scope == scope
    ]
    in_scope.sort(key=lambda score: _pair_sort_key(score.pair))
    if len(in_scope) > 1:
        ambiguity.append(
            f"同一范围“{scope.label}”有多个等价句对: "
            + ", ".join(score.pair.label for score in in_scope)
            + "；按编辑优先级选定第一个，仍建议复核"
        )
    return Selection(in_scope[0].pair, tuple(ambiguity), tuple(all_scores), tuple(usable))


# ---------------------------------------------------------------------------
# 交付
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LocaleMessage:
    locale: str
    status: str  # route / fallback / blocked
    message_id: str
    copy_id: str | None
    revision: str | None
    text: str
    columns: int | None
    limit: int
    issues: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Delivery:
    request_id: str
    screen_id: str
    visit_class: str
    status: str  # formal / approved / needs_editor / fallback / blocked / not_yet
    action: str | None
    messages: Mapping[str, LocaleMessage]
    pair: BilingualPair | None = None
    drafts: Mapping[str, Rendered] = field(default_factory=dict)
    alternatives: tuple[PairScore, ...] = ()
    issues: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    ambiguity: tuple[str, ...] = ()
    fallback_used: bool = False
    available: bool = True

    @property
    def routes(self) -> bool:
        return self.status in ("formal", "approved")

    @property
    def copy_ids(self) -> tuple[str | None, str | None]:
        return (
            self.messages["zh"].copy_id if "zh" in self.messages else None,
            self.messages["en"].copy_id if "en" in self.messages else None,
        )

    @property
    def route_text(self) -> Mapping[str, str]:
        return {
            locale: message.text
            for locale, message in self.messages.items()
            if message.status == "route"
        }

    @property
    def draft_text(self) -> Mapping[str, str]:
        return {
            locale: rendered.text
            for locale, rendered in self.drafts.items()
            if rendered.text
        }

    @property
    def shown_text(self) -> Mapping[str, str]:
        return {locale: message.text for locale, message in self.messages.items() if message.text}


def _latest_variant(workbench: Workbench, message_id: str, locale: str) -> CopyVariant | None:
    variants = workbench.variants_for(message_id, locale)
    if not variants:
        return None

    def revision_key(variant: CopyVariant) -> tuple[int, str]:
        digits = "".join(char for char in variant.revision if char.isdigit())
        return (int(digits) if digits else -1, variant.revision)

    return max(variants, key=revision_key)


def _fallback_messages(
    workbench: Workbench,
    screen_id: str,
    request: CopyRequest,
    policy: RenderPolicy,
) -> tuple[dict[str, LocaleMessage], tuple[str, ...], bool]:
    screen = workbench.screen(screen_id)
    messages: dict[str, LocaleMessage] = {}
    issues: list[str] = []
    ok = True
    for locale in ("zh", "en"):
        limit = screen.max_columns.get(locale, 0)
        variant = _latest_variant(workbench, screen.waiting_ref, locale)
        if variant is None:
            ok = False
            issues.append(f"{locale}: 缺少退回消息 {screen.waiting_ref}")
            messages[locale] = LocaleMessage(
                locale=locale,
                status="blocked",
                message_id=screen.waiting_ref,
                copy_id=None,
                revision=None,
                text="",
                columns=None,
                limit=limit,
                issues=(f"缺少 {locale} 退回消息",),
            )
            continue
        rendered = render_text(variant.text, {}, policy)
        columns, problems = check_fallback_text(
            rendered.text,
            limit,
            forbidden_values=request.forbidden_values(),
        )
        if rendered.blocked:
            problems = rendered.blocked + problems
        if problems:
            ok = False
            issues.extend(f"{locale}: {problem}" for problem in problems)
        messages[locale] = LocaleMessage(
            locale=locale,
            status="fallback" if not problems else "blocked",
            message_id=variant.message_id,
            copy_id=variant.id,
            revision=variant.revision,
            text=rendered.text,
            columns=columns,
            limit=limit,
            issues=tuple(problems),
            notes=("独立静态退回消息，不含姓名、地点或人数",),
        )
    if not ok:
        for locale, message in list(messages.items()):
            if message.status == "fallback":
                messages[locale] = LocaleMessage(
                    locale=locale,
                    status="blocked",
                    message_id=message.message_id,
                    copy_id=message.copy_id,
                    revision=message.revision,
                    text=message.text,
                    columns=message.columns,
                    limit=message.limit,
                    issues=message.issues
                    + ("另一种语言缺少可用退回消息，不能只保留一侧",),
                    notes=message.notes,
                )
    return messages, tuple(dict.fromkeys(issues)), ok


def _value_coverage_issues(
    pair: BilingualPair,
    request: CopyRequest,
    messages: Mapping[str, LocaleMessage],
) -> tuple[str, ...]:
    """交付前逐字确认两种语言包含同一个目的地与领队。"""
    problems: list[str] = []
    for locale in ("zh", "en"):
        message = messages.get(locale)
        if message is None or message.status != "route":
            problems.append(f"{locale}: 没有路线指引")
            continue
        for key in ("primary", "guide", "room"):
            value = request.values.get(key)
            if value in (None, ""):
                problems.append(f"{locale}: 缺少 {key}，不能把备用提示里的地点当作已授权目的地")
            elif str(value) not in message.text:
                problems.append(
                    f"{locale}: 渲染结果未包含请求登记的 {key} {value!r}，可能被替换或截断"
                )
    if pair.zh.text and pair.en.text:
        if template_fields(pair.zh.text) != template_fields(pair.en.text):
            problems.append("中英变量集合不同，动作意图无法对齐")
    return tuple(problems)


def _fallback_preview(
    workbench: Workbench,
    request: CopyRequest,
    policy: RenderPolicy,
) -> Delivery:
    """只显示通用服务台指引的预览：仍保留为什么没有路线，不冒充已交付。"""
    fallback, fallback_issues, fallback_ok = _fallback_messages(
        workbench, request.device, request, policy
    )
    return Delivery(
        request_id=request.id,
        screen_id=request.device,
        visit_class=request.visit_class,
        status="blocked" if not fallback_ok else "fallback",
        action=FALLBACK_ACTION if fallback_ok else None,
        messages=fallback,
        pair=None,
        drafts={},
        issues=tuple(dict.fromkeys(fallback_issues or ["没有适用于本次请求的双语路线句对"])),
        notes=("屏幕显示通用服务台指引，未交付任何姓名或目的地",),
        fallback_used=True,
    )


def resolve_delivery(
    workbench: Workbench,
    request: CopyRequest | VisitorRequest | str,
    *,
    message_id: str = "guide-7",
    approvals: Iterable[Approval] = (),
    policy: RenderPolicy = DEFAULT_POLICY,
    semantics: "Semantics" = DEFAULT_SEMANTICS,
) -> Delivery:
    """给出屏幕实际会显示的内容，以及仍需编辑处理的部分。"""
    if isinstance(request, str):
        request = request_from_id(workbench, request)
    elif isinstance(request, VisitorRequest):
        request = CopyRequest.from_visitor_request(request)

    if not request.available:
        return Delivery(
            request_id=request.id,
            screen_id=request.device,
            visit_class=request.visit_class,
            status="not_yet",
            action=None,
            messages={},
            issues=(
                f"请求登记为 {request.source.available_from if request.source else '未到班次'} 起可用；未到班次不得预览或交付",
            ),
            available=False,
        )

    screen = workbench.screen(request.device)
    issues: list[str] = []
    notes: list[str] = []

    if not request.visit_class_known:
        # 新增类别不能借用已登记类别的读法；直接给退回指引并交给编辑。
        fallback_delivery = _fallback_preview(workbench, request, policy)
        return Delivery(
            request_id=request.id,
            screen_id=request.device,
            visit_class=request.visit_class,
            status=fallback_delivery.status,
            action=fallback_delivery.action,
            messages=fallback_delivery.messages,
            pair=None,
            drafts={},
            alternatives=(),
            issues=tuple(
                ["访客类别未登记；不能沿用现有类别的文案，需编辑确认范围"]
                + list(fallback_delivery.issues)
            ),
            notes=fallback_delivery.notes,
            fallback_used=True,
        )

    review = review_copybook(workbench, semantics)
    selection = select_pair(workbench, review, request, message_id, policy)
    pair = selection.pair
    ambiguity = list(selection.ambiguity)

    if not pair:
        if not selection.usable:
            issues.append("没有适用于本次请求的双语路线句对")
        if ambiguity:
            notes.append("存在并存短句，无法确定唯一动作；不交付路线指引")
        fallback_delivery = _fallback_preview(workbench, request, policy)
        return Delivery(
            request_id=request.id,
            screen_id=request.device,
            visit_class=request.visit_class,
            status=fallback_delivery.status,
            action=fallback_delivery.action,
            messages=fallback_delivery.messages,
            pair=None,
            drafts={},
            alternatives=selection.all_scores,
            issues=tuple(
                dict.fromkeys(
                    issues
                    + list(fallback_delivery.issues)
                    + (
                        ["多个专门短句并存但优先级未定，不能任选其一交付"]
                        if selection.usable
                        else []
                    )
                )
            ),
            notes=tuple(dict.fromkeys(notes + list(fallback_delivery.notes))),
            ambiguity=tuple(ambiguity),
            fallback_used=True,
        )

    drafts = {
        "zh": render_text(pair.zh.text, request.values, policy),
        "en": render_text(pair.en.text, request.values, policy),
    }
    form = pair.form_issues
    complement = pair.complement_issues
    if pair.zh_analysis.scope != pair.en_analysis.scope:
        # 一边覆盖更宽、另一边继续排除某类来宾：切语言会得到不同动作。
        form = form + (
            "coverage_split: 两种语言的覆盖范围不同"
            f"（{pair.zh_analysis.scope.label} / {pair.en_analysis.scope.label}）；"
            "不得作为同一覆盖交付，需换成同范围短句",
        )
    approval = _approval_for(pair, approvals)
    if not _approval_usable(pair, approval):
        approval = None

    layout: list[str] = []
    for locale in ("zh", "en"):
        rendered = drafts[locale]
        limit = screen.max_columns.get(locale, 0)
        if rendered.blocked:
            layout.extend(f"{locale}: {problem}" for problem in rendered.blocked)
        if rendered.columns > limit:
            layout.append(
                f"{locale}: 草稿宽 {rendered.columns} 列，超过 {screen.device} 的 {limit} 列；"
                "不得截断姓名或地点换成另一个人"
            )
    issues.extend(layout)
    issues.extend(form)
    # 指代含糊可以由编辑签署确认；签署后不再重复列为待处理问题。
    if complement and approval is None:
        issues.extend(complement)
        if not form and not layout:
            issues.append("第二句集合范围需编辑签署后才能交付")
    elif complement:
        notes.append(
            f"编辑已签署第二句范围“{approval.scope_note}”（{pair.scope.label if pair.scope else ''}）"
        )
    if pair.zh_analysis.pending or pair.en_analysis.pending:
        issues.extend(pair.zh_analysis.pending)
        issues.extend(pair.en_analysis.pending)

    if not issues:
        messages = {
            locale: LocaleMessage(
                locale=locale,
                status="route",
                message_id=pair.message_id,
                copy_id=(pair.zh.id if locale == "zh" else pair.en.id),
                revision=(pair.zh.revision if locale == "zh" else pair.en.revision),
                text=drafts[locale].text,
                columns=drafts[locale].columns,
                limit=screen.max_columns.get(locale, 0),
                notes=drafts[locale].notes,
            )
            for locale in ("zh", "en")
        }
        issues.extend(_value_coverage_issues(pair, request, messages))
        if not issues:
            status = "formal"
            if approval is not None and pair.has_complement_issue:
                status = "approved"
                notes.append(f"编辑已签署范围说明: {approval.scope_note}")
            return Delivery(
                request_id=request.id,
                screen_id=request.device,
                visit_class=request.visit_class,
                status=status,
                action=f"{ROUTE_PREFIX}{request.values.get('room')}",
                messages=messages,
                pair=pair,
                drafts=drafts,
                alternatives=selection.all_scores,
                notes=tuple(notes),
                ambiguity=tuple(ambiguity),
            )

    # 有需要编辑处理的问题：不交付路线，只给安全退回。
    if form or layout:
        notes.append("需要另写短句；宽度不够时优先交回只引导到通用服务台的独立文案")
    fallback, fallback_issues, fallback_ok = _fallback_messages(
        workbench, request.device, request, policy
    )
    issues.extend(fallback_issues)
    notes.append("屏幕实际显示通用服务台指引；上方草稿仅供编校员核对")
    return Delivery(
        request_id=request.id,
        screen_id=request.device,
        visit_class=request.visit_class,
        status="blocked" if not fallback_ok else "needs_editor",
        action=FALLBACK_ACTION if fallback_ok else None,
        messages=fallback,
        pair=pair,
        drafts=drafts,
        alternatives=selection.all_scores,
        issues=tuple(dict.fromkeys(issues)),
        notes=tuple(dict.fromkeys(notes)),
        ambiguity=tuple(ambiguity),
        fallback_used=True,
    )


# ---------------------------------------------------------------------------
# 正式提交
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FormalSubmission:
    zh_id: str
    en_id: str
    status: str  # formal / needs_editor / blocked
    scope: Scope | None
    reasons: tuple[str, ...]
    checks: tuple[Any, ...]
    waiver: Approval | None = None

    @property
    def label(self) -> str:
        return f"{self.zh_id} ↔ {self.en_id}"


@dataclass(frozen=True)
class Plan:
    message_id: str
    status: str
    submissions: tuple[FormalSubmission, ...]
    required_placeholder_keys: frozenset[str]
    gaps: tuple[str, ...]
    fallback_ok_by_screen: Mapping[str, bool]
    notes: tuple[str, ...]

    @property
    def formal(self) -> tuple[FormalSubmission, ...]:
        return tuple(item for item in self.submissions if item.status == "formal")

    def for_scope(self, scope: Scope) -> FormalSubmission | None:
        for item in self.submissions:
            if item.status == "formal" and item.scope == scope:
                return item
        return None


def required_placeholder_keys(workbench: Workbench, message_id: str) -> frozenset[str]:
    from .review import MESSAGE_EXPECTATIONS

    expectation = MESSAGE_EXPECTATIONS.get(message_id)
    if expectation is None:
        return frozenset()
    return frozenset(expectation.required_placeholders)


@dataclass(frozen=True)
class CoverageProbe:
    scope: Scope
    label: str
    locales_present: Mapping[str, tuple[str, ...]]

    @property
    def missing_locales(self) -> tuple[str, ...]:
        return tuple(locale for locale in ("zh", "en") if not self.locales_present.get(locale))


def _serves_probe(analysis: CopyAnalysis, scope: Scope) -> bool:
    """这一条短句能否在不改文案的前提下服务该范围。"""
    if not analysis.split or analysis.errors or analysis.pending:
        return False
    if not analysis.scope.covers(scope):
        return False
    if scope.requires_consent and not analysis.split.honorific_slot:
        return False
    if scope.max_count == 0 and "group_count" in template_fields(analysis.variant.text):
        # 单独来宾不会愿意看到“0 位同行者”。
        return False
    return True


def coverage_probes(
    workbench: Workbench,
    message_id: str = "guide-7",
    semantics: Semantics = DEFAULT_SEMANTICS,
) -> tuple[CoverageProbe, ...]:
    """按人数与称呼组合检查是否存在两种语言的同范围短句。

    静态退回消息不按访客范围划分，因此不产生覆盖缺口。
    """
    analyses = [
        analyze_copy(variant, semantics)
        for variant in workbench.variants_for(message_id)
    ]
    if analyses and all(analysis.is_static for analysis in analyses):
        return ()
    scopes = [
        (Scope.solo(), "单独来宾"),
        (Scope.party(), "成组来宾"),
        (Scope.consented_party(), "有经本人同意的身份称呼的成组来宾"),
        (Scope(max_count=0, requires_consent=True), "有经本人同意的身份称呼的单独来宾"),
    ]
    probes: list[CoverageProbe] = []
    for scope, label in scopes:
        present: dict[str, list[str]] = {"zh": [], "en": []}
        for analysis in analyses:
            if _serves_probe(analysis, scope):
                present[analysis.variant.locale].append(analysis.variant.id)
        probes.append(
            CoverageProbe(
                scope=scope,
                label=label,
                locales_present={locale: tuple(ids) for locale, ids in present.items()},
            )
        )
    return tuple(probes)


def coverage_gaps(
    workbench: Workbench,
    message_id: str = "guide-7",
    semantics: Semantics = DEFAULT_SEMANTICS,
) -> tuple[str, ...]:
    """找出只有一种语言有同范围短句的来宾群体。"""
    if isinstance(workbench, Review):
        workbench = workbench.workbench
    gaps: list[str] = []
    for probe in coverage_probes(workbench, message_id, semantics):
        missing = probe.missing_locales
        if not missing:
            continue
        have = "、".join(
            f"{locale}:{','.join(ids)}"
            for locale, ids in probe.locales_present.items()
            if ids
        )
        gaps.append(
            f"{probe.label}: 缺{'与'.join('中文' if m == 'zh' else '英文' for m in missing)}同范围短句"
            + (f"（现有 {have}）" if have else "")
        )
    return tuple(gaps)


def assemble_copybook(
    workbench: Workbench,
    message_id: str = "guide-7",
    approvals: Iterable[Approval] = (),
    semantics: Semantics = DEFAULT_SEMANTICS,
) -> Plan:
    """列出可正式提交的中英组合。

    正式提交的两种语言必须落在同一覆盖范围；用一个语言热换成更宽的人群、
    另一个语言继续排除某类来宾，都不能进入 formal。
    """
    review = review_copybook(workbench, semantics)
    approvals = tuple(approvals)
    submissions: list[FormalSubmission] = []
    notes: list[str] = []

    for pair in bilingual_matches(workbench, message_id, semantics):
        approval = _approval_for(pair, approvals)
        if not _approval_usable(pair, approval):
            approval = None
        reasons: list[str] = []
        status = "blocked"

        if pair.joint_scope is None:
            reasons.append("两种语言的适用范围没有交集")
        elif pair.zh_analysis.scope != pair.en_analysis.scope:
            reasons.append(
                f"两种语言的覆盖范围不同（{pair.zh_analysis.scope.label} / "
                f"{pair.en_analysis.scope.label}）；正式提交须来自同一覆盖，"
                "不能一边扩大人群、另一边继续排除"
            )
            status = "blocked"
        elif pair.status == "mismatch" or pair.hard_failures:
            reasons.extend(pair.hard_failures or pair.reasons)
        else:
            form = list(morphology_issues(pair)) + list(pair.form_issues)
            if form:
                reasons.extend(form)
                status = "blocked"
            elif pair.has_complement_issue and approval is None:
                reasons.append("第二句集合范围需编辑签署")
                status = "needs_editor"
            else:
                if pair.has_complement_issue and approval is not None:
                    notes.append(
                        f"{pair.label}: 接受编辑范围签署“{approval.scope_note}”（{pair.scope.label}）"
                    )
                status = "formal"
            if status == "formal" and pair.joint_scope is not None and not pair.joint_scope.is_unrestricted:
                notes.append(
                    f"{pair.label}: 仅按“{pair.joint_scope.label}”这一覆盖范围提交，"
                    "不得扩大到其他来宾"
                )

        submissions.append(
            FormalSubmission(
                zh_id=pair.zh.id,
                en_id=pair.en.id,
                status=status,
                scope=pair.joint_scope,
                reasons=tuple(dict.fromkeys(reasons)),
                checks=pair.checks,
                waiver=approval if status == "formal" and approval is not None else None,
            )
        )

    fallback_ok: dict[str, bool] = {}
    for screen_id, screen in workbench.screens.items():
        ok = True
        reasons = []
        for locale in ("zh", "en"):
            variant = _latest_variant(workbench, screen.waiting_ref, locale)
            if variant is None:
                ok = False
                reasons.append(f"{locale}: 缺退回消息")
                continue
            rendered = render_text(variant.text, {})
            _, problems = check_fallback_text(
                rendered.text, screen.max_columns.get(locale, 0)
            )
            if problems:
                ok = False
                reasons.extend(f"{locale}: {problem}" for problem in problems)
        fallback_ok[screen_id] = ok
        if not ok:
            notes.append(f"{screen.device}: 退回消息不可用: " + "；".join(reasons))

    if any(item.status == "formal" for item in submissions):
        status = "formal"
    elif any(item.status == "needs_editor" for item in submissions):
        status = "needs_editor"
    else:
        status = "blocked"
    if not any(fallback_ok.values()):
        status = "blocked"
        notes.append("没有任何屏幕具备安全退回消息")

    return Plan(
        message_id=message_id,
        status=status,
        submissions=tuple(submissions),
        required_placeholder_keys=required_placeholder_keys(workbench, message_id),
        gaps=coverage_gaps(workbench, message_id, semantics),
        fallback_ok_by_screen=fallback_ok,
        notes=tuple(dict.fromkeys(notes)),
    )


# ---------------------------------------------------------------------------
# 变更核对
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IntakeRow:
    request_id: str
    device: str
    visit_class: str
    before_status: str
    after_status: str
    before_copy_ids: tuple[str | None, str | None]
    after_copy_ids: tuple[str | None, str | None]
    changed: bool
    preview_available: bool
    route_delivered: bool
    issues: tuple[str, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class IntakeReport:
    rows: tuple[IntakeRow, ...]
    devices_to_notify: tuple[str, ...]
    unsuitable: tuple[str, ...]
    unsuitable_pairs: tuple[FormalSubmission, ...]
    missing_translations: tuple[str, ...]
    fallback_ok_by_screen: Mapping[str, bool]
    plan: Plan
    notes: tuple[str, ...]
    devices_to_notify_names: tuple[str, ...] = ()
    pending_translation_groups: tuple[str, ...] = ()

    @property
    def all_previewable(self) -> bool:
        return all(row.preview_available for row in self.rows)

    @property
    def all_route_delivered(self) -> bool:
        return all(row.route_delivered for row in self.rows)


def _load_proposed_variants(
    proposed: Iterable[CopyVariant | Mapping[str, Any]] | None,
) -> tuple[CopyVariant, ...] | None:
    if proposed is None:
        return None
    return tuple(
        item if isinstance(item, CopyVariant) else CopyVariant.from_row(item)
        for item in proposed
    )


def inspect_copybook(
    workbench: Workbench,
    *,
    proposed: Iterable[CopyVariant | Mapping[str, Any]] | None = None,
    extra_requests: Sequence[VisitorRequest | Mapping[str, Any]] = (),
    shift: str | None = None,
    approvals: Iterable[Approval] = (),
    semantics: Semantics = DEFAULT_SEMANTICS,
) -> IntakeReport:
    """核对临时更正或新增访客类别后，原有请求是否仍能照常预览。

    proposed 为 None 时只核对当前资料。extra_requests 允许传业务方新增类别的
    合成请求，用来看覆盖缺口和需要通知的设备；不写回 data/。
    """
    proposed_variants = _load_proposed_variants(proposed)
    if proposed_variants is not None:
        validate_copybook(proposed_variants)
    extra = [
        item if isinstance(item, VisitorRequest) else VisitorRequest.from_row(item)
        for item in extra_requests
    ]
    after = workbench
    if proposed_variants is not None or extra:
        after = Workbench(
            copybook=proposed_variants if proposed_variants is not None else workbench.copybook,
            screens=workbench.screens,
            requests=workbench.requests + tuple(extra),
            fields=workbench.fields,
        )

    reuse_notes: list[str] = []
    if proposed_variants is not None:
        original = {variant.id: variant for variant in workbench.copybook}
        for variant in proposed_variants:
            previous = original.get(variant.id)
            if previous is not None and (
                previous.text != variant.text
                or previous.message_id != variant.message_id
                or previous.locale != variant.locale
            ):
                reuse_notes.append(
                    f"{variant.id}: 文案文字已改动但沿用原 id 的语义登记，读法需编辑重新确认"
                )

    request_ids = [request.id for request in workbench.requests]
    request_ids.extend(request.id for request in extra)
    rows: list[IntakeRow] = []
    devices: set[str] = set()
    unsuitable: list[str] = []
    notes: list[str] = []
    approvals = tuple(approvals)

    for request_id in request_ids:
        request = after.request(request_id)
        source = CopyRequest.from_visitor_request(request, shift)
        before = resolve_delivery(
            workbench, source, approvals=approvals, semantics=semantics
        )
        after_delivery = resolve_delivery(
            after, source, approvals=approvals, semantics=semantics
        )
        changed = (
            before.status != after_delivery.status
            or before.copy_ids != after_delivery.copy_ids
            or before.route_text != after_delivery.route_text
        )
        preview_available = after_delivery.status not in ("blocked", "not_yet")
        route_delivered = after_delivery.status in ("formal", "approved")
        if not preview_available or after_delivery.status in ("needs_editor", "fallback"):
            devices.add(request.device)
        if not route_delivered:
            unsuitable.append(
                f"{request.id}({request.visit_class}) @ {request.device}: "
                f"{after_delivery.status}；"
                + ("；".join(after_delivery.issues) or "未交付路线指引")
            )
        rows.append(
            IntakeRow(
                request_id=request.id,
                device=request.device,
                visit_class=request.visit_class,
                before_status=before.status,
                after_status=after_delivery.status,
                before_copy_ids=before.copy_ids,
                after_copy_ids=after_delivery.copy_ids,
                changed=changed,
                preview_available=preview_available,
                route_delivered=route_delivered,
                issues=after_delivery.issues,
                notes=after_delivery.notes,
            )
        )

    plan = assemble_copybook(after, approvals=approvals, semantics=semantics)
    for screen_id, ok in plan.fallback_ok_by_screen.items():
        if not ok:
            devices.add(screen_id)
            notes.append(f"{after.screen(screen_id).device}: 缺安全退回消息，需通知设备方")
    for gap in plan.gaps:
        # 覆盖缺口影响所有仍在用该消息的屏幕。
        for screen_id in after.screens:
            devices.add(screen_id)
    if not rows:
        notes.append("没有可核对的请求")
    if not any(row.changed for row in rows):
        notes.append("所有请求的屏幕显示与当前交付相同")
    unsuitable_pairs = tuple(
        item for item in plan.submissions if item.status != "formal"
    )
    notes.extend(reuse_notes)
    device_ids = tuple(sorted(devices))
    return IntakeReport(
        rows=tuple(rows),
        devices_to_notify=device_ids,
        unsuitable=tuple(unsuitable),
        unsuitable_pairs=unsuitable_pairs,
        missing_translations=plan.gaps,
        fallback_ok_by_screen=dict(plan.fallback_ok_by_screen),
        plan=plan,
        notes=tuple(dict.fromkeys(notes)),
        devices_to_notify_names=tuple(
            after.screen(screen_id).device for screen_id in device_ids
        ),
        pending_translation_groups=plan.gaps,
    )


def build_bundle(
    workbench: Workbench,
    *,
    shift: str | None = None,
    approvals: Iterable[Approval] = (),
    semantics: Semantics = DEFAULT_SEMANTICS,
) -> tuple[Delivery, ...]:
    """按资料中的全部来宾请求生成屏幕预览。"""
    return tuple(
        resolve_delivery(
            workbench,
            CopyRequest.from_visitor_request(request, shift),
            approvals=approvals,
            semantics=semantics,
        )
        for request in workbench.requests
    )


def triage_requests(
    workbench: Workbench,
    *,
    shift: str | None = None,
    approvals: Iterable[Approval] = (),
) -> Mapping[str, tuple[str, tuple[str, ...]]]:
    """每个请求的交付状态与需要通知的设备。"""
    return {
        delivery.request_id: (delivery.status, (delivery.screen_id,))
        for delivery in build_bundle(workbench, shift=shift, approvals=approvals)
    }
