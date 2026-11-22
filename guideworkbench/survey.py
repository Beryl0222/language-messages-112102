"""面向编校员的高层视图：把交付、覆盖缺口和双语言动作稳定性放在一起。

宽度和退回消息按资料中登记的真实屏幕检查；新增访客类别、替换短句等
临时请求可以在内存中构造，不改动 data/ 下的交换资料。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .bundle import (
    Approval,
    CopyRequest,
    Delivery,
    IntakeReport,
    assemble_copybook,
    coverage_gaps,
    inspect_copybook,
    request_from_id,
    resolve_delivery,
)
from .model import CopyVariant, VisitorRequest, Workbench, load_workbench
from .render import DEFAULT_POLICY, RenderPolicy
from .review import (
    ALL_GUEST_TYPES,
    DEFAULT_SEMANTICS,
    Semantics,
)


@dataclass(frozen=True)
class RequestResult:
    """一个合成请求的完整预览结果。"""

    request_id: str
    visit_class: str
    device: str
    delivery: Delivery
    renderable: bool
    consistent_action: bool
    triggered_conditions: tuple[str, ...]
    unrendered_locales: tuple[str, ...]
    issues: tuple[str, ...]

    @property
    def status(self) -> str:
        return self.delivery.status

    @property
    def shown(self) -> Mapping[str, str]:
        return {
            locale: message.text
            for locale, message in self.delivery.messages.items()
            if message.text
        }

    @property
    def action(self) -> str | None:
        return self.delivery.action


def _triggered_conditions(delivery: Delivery) -> tuple[str, ...]:
    """列出这次请求触发的语言条件，逐条标明作用在哪一种语言。"""
    pair = delivery.pair
    if pair is None:
        return ()
    triggers: list[str] = []
    for locale, variant in (("zh", pair.zh), ("en", pair.en)):
        if variant.applies_to:
            triggers.append(f"{locale} {variant.id}: {variant.applies_to}")
    scope = pair.joint_scope
    if scope is not None:
        for condition in scope.conditions:
            triggers.append(f"句对共同范围: {condition}")
    return tuple(dict.fromkeys(triggers)) or ("无附加条件",)


def _locales_without_output(delivery: Delivery) -> tuple[str, ...]:
    missing: list[str] = []
    for locale in ("zh", "en"):
        message = delivery.messages.get(locale)
        if message is None or not message.text:
            missing.append(locale)
    return tuple(missing)


def _actions_consistent(delivery: Delivery) -> bool:
    """切换语言时动作意图一致：要么两侧都是路线，要么两侧都是同一退回。"""
    if delivery.status == "not_yet":
        return True
    zh = delivery.messages.get("zh")
    en = delivery.messages.get("en")
    if zh is None or en is None:
        return False
    if zh.status == "route" and en.status == "route":
        return delivery.action is not None and delivery.action.startswith("route_to_room:")
    if zh.status == "fallback" and en.status == "fallback":
        return delivery.action == "ask_help_desk"
    return False


def render_request(
    workbench: Workbench,
    request: CopyRequest | VisitorRequest | str,
    *,
    message_id: str = "guide-7",
    approvals: Iterable[Approval] = (),
    policy: RenderPolicy = DEFAULT_POLICY,
    semantics: Semantics = DEFAULT_SEMANTICS,
) -> RequestResult:
    if isinstance(request, str):
        request = request_from_id(workbench, request)
    elif isinstance(request, VisitorRequest):
        request = CopyRequest.from_visitor_request(request)
    delivery = resolve_delivery(
        workbench,
        request,
        message_id=message_id,
        approvals=approvals,
        policy=policy,
        semantics=semantics,
    )
    return RequestResult(
        request_id=request.id,
        visit_class=request.visit_class,
        device=request.device,
        delivery=delivery,
        renderable=not _locales_without_output(delivery),
        consistent_action=_actions_consistent(delivery),
        triggered_conditions=_triggered_conditions(delivery),
        unrendered_locales=_locales_without_output(delivery),
        issues=delivery.issues,
    )


def _synthetic_request(
    request_id: str,
    visit_class: str,
    device: str,
    values: Mapping[str, Any],
    shift: str | None = None,
) -> CopyRequest:
    return CopyRequest(
        id=request_id,
        device=device,
        visit_class=visit_class,
        values=dict(values),
        shift=shift,
    )


def _class_values(base: Mapping[str, Any], visit_class: str) -> dict[str, Any]:
    """按访客类别调整字段。

    只显示自选名的来宾可能独自到场，也可能与同行者一起；这里保留基准
    请求的人数，便于比较“同一人数、不同身份处理”的两种情况。
    """
    values = dict(base)
    if visit_class == "solo":
        values["group_count"] = 0
        values.pop("permitted_public_name", None)
        values["honorific"] = None
    elif visit_class == "party":
        values["group_count"] = max(1, int(values.get("group_count") or 1))
        values.pop("permitted_public_name", None)
        values["honorific"] = None
    elif visit_class == "closed-name":
        if not values.get("permitted_public_name"):
            values["permitted_public_name"] = str(values.get("primary", "")).split()[0]
        values.setdefault("honorific", None)
    else:
        values.setdefault("honorific", None)
    return values


def survey_classes(
    workbench: Workbench | str = None,
    *,
    base_request_id: str = "R2",
    shift: str | None = None,
    semantics: Semantics = DEFAULT_SEMANTICS,
) -> tuple[RequestResult, ...]:
    """为每个已登记访客类别各造一条实际会发送的请求。

    名字、领队、地点和人数沿用 base_request_id 的登记值，只改变类别相关
    字段，便于比较同一短句在不同来宾身上会怎样。
    """
    if workbench is None or isinstance(workbench, str):
        workbench = load_workbench(workbench)
    base = workbench.request(base_request_id)
    device = base.device
    results: list[RequestResult] = []
    for index, visit_class in enumerate(ALL_GUEST_TYPES):
        request = _synthetic_request(
            f"synthetic-{visit_class}",
            visit_class,
            device,
            _class_values(base.data, visit_class),
            shift=shift,
        )
        results.append(render_request(workbench, request, semantics=semantics))
    return tuple(results)


@dataclass(frozen=True)
class Assignment:
    """某类来宾在该屏幕上的分配。draft_ids 是屏幕不显示、仅供核对的草稿。"""

    status: str
    action: str
    copy_ids: tuple[str | None, str | None]
    draft_ids: tuple[str | None, str | None]
    consistent_action: bool
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScreenView:
    """某屏幕下，各类来宾被分配到哪个短句、用什么动作。"""

    screen_id: str
    device: str
    assignments: Mapping[str, Assignment]
    gaps: tuple[str, ...]
    fallback_ok: bool
    notes: tuple[str, ...] = ()

    @property
    def ambiguous(self) -> bool:
        return any(not item.consistent_action for item in self.assignments.values())

    @property
    def delivered(self) -> tuple[str, ...]:
        return tuple(
            visit_class
            for visit_class, item in self.assignments.items()
            if item.status in ("formal", "approved")
        )


def plan_screen(
    workbench: Workbench,
    screen_id: str,
    *,
    base_request_id: str | None = None,
    approvals: Iterable[Approval] = (),
    shift: str | None = None,
    semantics: Semantics = DEFAULT_SEMANTICS,
) -> ScreenView:
    if not workbench.requests:
        raise ValueError("资料中没有来宾请求，无法构造各族类预览")
    base = (
        workbench.request(base_request_id)
        if base_request_id is not None
        else workbench.requests[0]
    )
    if base.device != screen_id:
        notes = (f"基准请求登记在 {base.device}，此处按 {screen_id} 的宽度重算",)
    else:
        notes = ()
    assignments: dict[str, Assignment] = {}
    for visit_class in ALL_GUEST_TYPES:
        request = _synthetic_request(
            f"synthetic-{visit_class}",
            visit_class,
            screen_id,
            _class_values(base.data, visit_class),
            shift=shift,
        )
        result = render_request(
            workbench, request, approvals=approvals, semantics=semantics
        )
        assignments[visit_class] = Assignment(
            status=result.status,
            action=result.delivery.action or "none",
            copy_ids=result.delivery.copy_ids,
            draft_ids=(
                (result.delivery.pair.zh.id, result.delivery.pair.en.id)
                if result.delivery.pair is not None
                else (None, None)
            ),
            consistent_action=result.consistent_action,
            issues=result.issues,
        )
    plan = assemble_copybook(workbench, approvals=approvals, semantics=semantics)
    return ScreenView(
        screen_id=screen_id,
        device=workbench.screen(screen_id).device,
        assignments=assignments,
        gaps=coverage_gaps(workbench, semantics=semantics),
        fallback_ok=plan.fallback_ok_by_screen.get(screen_id, False),
        notes=notes,
    )


@dataclass(frozen=True)
class CrossScreenView:
    per_screen: Mapping[str, ScreenView]

    def same_action(self, visit_class: str) -> bool:
        actions = {
            view.assignments[visit_class].action for view in self.per_screen.values()
        }
        if len(actions) != 1:
            return False
        action = actions.pop()
        return action != "none"

    def inconsistent_samples(self) -> Mapping[str, Mapping[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for visit_class in ALL_GUEST_TYPES:
            actions = {
                screen_id: view.assignments[visit_class].action
                for screen_id, view in self.per_screen.items()
            }
            if len(set(actions.values())) > 1:
                out[visit_class] = actions
        return out


def cross_screen_view(
    workbench: Workbench,
    *,
    screen_ids: Sequence[str] | None = None,
    approvals: Iterable[Approval] = (),
    shift: str | None = None,
    semantics: Semantics = DEFAULT_SEMANTICS,
) -> CrossScreenView:
    """检查同一类来宾在不同屏幕切换时是否落到同一动作。"""
    ids = list(screen_ids) if screen_ids is not None else list(workbench.screens)
    per_screen = {
        screen_id: plan_screen(
            workbench,
            screen_id,
            approvals=approvals,
            shift=shift,
            semantics=semantics,
        )
        for screen_id in ids
    }
    return CrossScreenView(per_screen=per_screen)


def survey_requests(
    workbench: Workbench,
    request_ids: Sequence[str] | None = None,
    *,
    shift: str | None = None,
    approvals: Iterable[Approval] = (),
    semantics: Semantics = DEFAULT_SEMANTICS,
) -> tuple[RequestResult, ...]:
    """用实际会发送的合成请求逐个预览。"""
    ids = list(request_ids) if request_ids is not None else [r.id for r in workbench.requests]
    return tuple(
        render_request(
            workbench,
            request_from_id(workbench, request_id, shift=shift),
            approvals=approvals,
            semantics=semantics,
        )
        for request_id in ids
    )


def check_intake(
    workbench: Workbench,
    *,
    proposed: Iterable[CopyVariant | Mapping[str, Any]] | None = None,
    extra_requests: Sequence[VisitorRequest | Mapping[str, Any]] = (),
    shift: str | None = None,
    approvals: Iterable[Approval] = (),
    semantics: Semantics = DEFAULT_SEMANTICS,
) -> IntakeReport:
    return inspect_copybook(
        workbench,
        proposed=proposed,
        extra_requests=extra_requests,
        shift=shift,
        approvals=approvals,
        semantics=semantics,
    )
