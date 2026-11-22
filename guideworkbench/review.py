"""核对短句的语义声明：变量指谁、限定谁、第二句承接哪个集合。

字符串层面“变量齐全、字数相同”不作为通过条件。每条短句都要能说清
哪些人走、哪些人留、领队是谁、人数是否含本人、第二句的指代范围，
以及某个词位只是显示还是同时参与分流。

覆盖范围用条件表示，而不是用一句“适用所有来宾”概括。这样“单独来宾”
和“成组来宾”两种各自可用的短写法可以并存，但任一实际请求只会落进
其中一个；两种语言的句对也只在双方共同成立的范围里才算等价。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping
import re

from .model import CopyVariant, Workbench
from .render import TemplateParts, parse_template, template_fields


ERROR = "error"
WARN = "warn"
INFO = "info"


@dataclass(frozen=True)
class Flag:
    code: str
    severity: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.code}: {self.message}"


@dataclass(frozen=True)
class Check:
    id: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class Scope:
    """一条短句获准服务的来宾范围。

    只用人数下界/上界和“是否有经本人同意的身份称呼”两个维度；这不是
    要覆盖所有语义，而是把材料里真实出现的变量条件说清楚。
    """

    min_count: int = 0
    max_count: int | None = None
    requires_consent: bool = False

    @classmethod
    def all(cls) -> "Scope":
        return cls()

    @classmethod
    def solo(cls) -> "Scope":
        return cls(max_count=0)

    @classmethod
    def party(cls) -> "Scope":
        return cls(min_count=1)

    @classmethod
    def consented_party(cls) -> "Scope":
        return cls(min_count=1, requires_consent=True)

    @classmethod
    def not_applicable(cls) -> "Scope":
        return cls(min_count=0, max_count=-1)

    @property
    def is_unrestricted(self) -> bool:
        return self.min_count == 0 and self.max_count is None and not self.requires_consent

    @property
    def is_empty(self) -> bool:
        return self.max_count is not None and self.max_count < self.min_count

    @property
    def conditions(self) -> tuple[str, ...]:
        out: list[str] = []
        if self.min_count == 1 and self.max_count is None:
            out.append("group_count>=1")
        elif self.max_count == 0:
            out.append("group_count==0")
        elif self.min_count == self.max_count and self.max_count is not None:
            out.append(f"group_count=={self.min_count}")
        elif self.min_count > 0:
            out.append(f"group_count>={self.min_count}")
        if self.max_count is not None and self.min_count != self.max_count:
            out.append(f"group_count<={self.max_count}")
        if self.requires_consent:
            out.append("有经本人同意的身份称呼")
        return tuple(out)

    @property
    def label(self) -> str:
        if self.is_empty:
            return "不适用（静态消息）"
        if self.is_unrestricted:
            return "所有已登记来宾"
        names = []
        if self.max_count == 0:
            names.append("单独来宾")
        elif self.min_count >= 1:
            names.append("成组来宾")
        if self.requires_consent:
            names.append("有经本人同意的身份称呼")
        return "、".join(names) or "；".join(self.conditions)

    def satisfied(
        self, values: Mapping[str, Any], neutral_allowed: bool = False
    ) -> bool:
        """本次请求是否落在此范围。

        neutral_allowed 表示已有获准的中性写法；没有记录称呼、本人也不愿被
        称先生/女士时，可以用中性写法满足称呼要求，而不是扩大给未同意的人。
        """
        if self.is_empty:
            return False
        try:
            count = int(values.get("group_count") or 0)
        except (TypeError, ValueError):
            return False
        if count < self.min_count:
            return False
        if self.max_count is not None and count > self.max_count:
            return False
        if self.requires_consent:
            present = values.get("honorific") not in (None, "")
            consent = bool(
                values.get("honorific_consent") or values.get("honorific_approved")
            )
            if present and consent:
                return True
            if not present and neutral_allowed:
                return True
            return False
        return True

    def intersect(self, other: "Scope") -> "Scope | None":
        if self.is_empty or other.is_empty:
            return None
        low = max(self.min_count, other.min_count)
        if self.max_count is None:
            high = other.max_count
        elif other.max_count is None:
            high = self.max_count
        else:
            high = min(self.max_count, other.max_count)
        if high is not None and low > high:
            return None
        return Scope(low, high, self.requires_consent or other.requires_consent)

    def subsumes(self, other: "Scope") -> bool:
        """self 是否严格比 other 更窄。"""
        if self == other:
            return False
        return other.covers(self)

    def covers(self, other: "Scope") -> bool:
        """self 的范围是否包含 other 的全部情形。"""
        if self.min_count > other.min_count:
            return False
        if self.max_count is not None:
            if other.max_count is None or other.max_count > self.max_count:
                return False
        if self.requires_consent and not other.requires_consent:
            return False
        return True

    def representative_values(self, *, honorific: bool = True) -> dict[str, Any]:
        if self.max_count == 0:
            count = 0
        else:
            count = max(self.min_count, 2)
        values: dict[str, Any] = {"group_count": count}
        if self.requires_consent:
            values["honorific"] = "获准称呼" if honorific else None
            values["honorific_consent"] = True
        return values


def parse_applies_to(raw: str | None) -> tuple[Scope | None, bool]:
    """把 applies_to 转成范围。返回 (范围, 是否可判定)。"""
    if raw is None:
        return (None, True)
    text = raw.strip()
    if not text:
        return (None, False)
    match = re.fullmatch(r"group_count\s*(==|!=|>=|<=|>|<)\s*(-?\d+)", text)
    if match:
        operator, number_text = match.groups()
        number = int(number_text)
        if operator == "==":
            return (Scope(number, number), True)
        if operator == ">=":
            return (Scope(number, None), True)
        if operator == ">":
            return (Scope(number + 1, None), True)
        if operator == "<=":
            return (Scope(0, number), True)
        if operator == "<":
            return (Scope(0, number - 1), True)
        return (None, False)  # != 可用并集表达，这里不猜
    lowered = text.casefold()
    consent_phrases = {
        "有经本人同意的身份称呼",
        "有获准称呼",
        "有身份称呼",
        "honorific is not null",
        "has honorific",
        "consented honorific",
    }
    if text in consent_phrases or lowered in {item.casefold() for item in consent_phrases}:
        return (Scope(requires_consent=True), True)
    return (None, False)


@dataclass(frozen=True)
class GroupSplit:
    """一条短句对“谁走、谁留”的声明。"""

    goers: frozenset[str]
    guide_goes: bool
    party_size_displayed: bool
    party_size_known: bool
    party_size_includes_primary: bool | None
    complement_universe: str | None
    complement_kind: str | None
    complement_source: str | None
    honorific_slot: bool
    action: str | None
    scope: Scope

    @property
    def copy_scope(self) -> Scope:
        return self.scope


@dataclass(frozen=True)
class CopyAnalysis:
    variant: CopyVariant
    split: GroupSplit | None
    checks: tuple[Check, ...]
    flags: tuple[Flag, ...]
    pending: tuple[str, ...]
    slot_use: Mapping[str, tuple[str, ...]]

    @property
    def scope(self) -> Scope:
        return self.split.scope if self.split else Scope.not_applicable()

    @property
    def errors(self) -> tuple[Flag, ...]:
        return tuple(flag for flag in self.flags if flag.severity == ERROR)

    @property
    def warnings(self) -> tuple[Flag, ...]:
        return tuple(flag for flag in self.flags if flag.severity == WARN)

    @property
    def is_static(self) -> bool:
        check = self.check("static_message")
        return bool(check and check.ok)

    @property
    def deliverable(self) -> bool:
        if self.is_static:
            return not self.errors
        return bool(self.split) and not self.errors and not self.pending

    @property
    def serviceable_types(self) -> tuple[str, ...]:
        """按三类来宾的形态判断短句能否服务；可选身份另见 scope。"""
        shape = {
            "solo": {"group_count": 0},
            "party": {"group_count": 2},
            "closed-name": {
                "group_count": 1,
                "permitted_public_name": "自选名",
            },
        }
        out = []
        for name, values in shape.items():
            probe = dict(values)
            if self.scope.requires_consent:
                probe["honorific"] = "获准称呼"
                probe["honorific_consent"] = True
            if self.scope.satisfied(probe):
                out.append(name)
        return tuple(out)

    def check(self, check_id: str) -> Check | None:
        for check in self.checks:
            if check.id == check_id:
                return check
        return None


# ---------------------------------------------------------------------------
# 已登记消息的语义预期。变量在本消息里有固定身份，翻译不能重新分配。
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MessageExpectation:
    message_id: str
    kind: str
    required_placeholders: frozenset[str]
    optional_placeholders: frozenset[str]
    leader_key: str | None
    follower_key: str | None
    destination_key: str | None


MESSAGE_EXPECTATIONS: dict[str, MessageExpectation] = {
    "guide-7": MessageExpectation(
        message_id="guide-7",
        kind="route",
        required_placeholders=frozenset({"primary", "guide", "room"}),
        optional_placeholders=frozenset({"group_count", "honorific"}),
        leader_key="guide",
        follower_key="primary",
        destination_key="room",
    ),
    "guide-8": MessageExpectation(
        message_id="guide-8",
        kind="static_assist",
        required_placeholders=frozenset(),
        optional_placeholders=frozenset(),
        leader_key=None,
        follower_key=None,
        destination_key=None,
    ),
}

ALL_GUEST_TYPES = ("solo", "party", "closed-name")


# ---------------------------------------------------------------------------
# 已核对读法。每条都明确范围；没有声明的短句不会被当成“同样适用”。
# ---------------------------------------------------------------------------

def _split(
    *,
    goers: Iterable[str],
    party_displayed: bool,
    party_known: bool,
    party_includes_primary: bool | None,
    universe: str | None,
    kind: str | None,
    source: str | None,
    honorific: bool,
    scope: Scope,
    guide_goes: bool = True,
) -> GroupSplit:
    return GroupSplit(
        goers=frozenset(goers),
        guide_goes=guide_goes,
        party_size_displayed=party_displayed,
        party_size_known=party_known,
        party_size_includes_primary=party_includes_primary,
        complement_universe=universe,
        complement_kind=kind,
        complement_source=source,
        honorific_slot=honorific,
        action="travel_to_room_with_guide",
        scope=scope,
    )


@dataclass(frozen=True)
class Semantic:
    roles: Mapping[str, str]
    split: GroupSplit
    slot_use: Mapping[str, tuple[str, ...]]
    pending: tuple[str, ...]
    notes: tuple[str, ...] = ()


DECLARED: dict[str, Semantic] = {
    "V1": Semantic(
        roles={
            "primary": "被引领的来宾本人",
            "group_count": "同组同行者人数，不含本人",
            "guide": "实际领队，随行到目的地",
            "room": "已登记的目的地",
        },
        split=_split(
            goers=("primary", "companions", "guide"),
            party_displayed=True,
            party_known=True,
            party_includes_primary=False,
            universe="guests",
            kind="remaining",
            source="sentence1_goers",
            honorific=False,
            scope=Scope.all(),
        ),
        slot_use={
            "primary": ("display", "control"),
            "group_count": ("display", "control"),
            "guide": ("display", "control"),
            "room": ("display", "action"),
        },
        pending=(),
        notes=(
            "“其余来宾”是第一句同行集合在来宾范围内的补集，第二句范围不得换成其他人。",
            "“同组”把同行者限定为与主来宾同一行的人。",
        ),
    ),
    "V2": Semantic(
        roles={
            "primary": "实际被追随的人，不是领队",
            "group_count": "被叫去会合的另一批同行人数",
            "guide": "受话人，被要求去与来宾会合",
            "room": "会合地点",
        },
        split=_split(
            goers=("primary", "companions", "guide"),
            party_displayed=True,
            party_known=True,
            party_includes_primary=False,
            universe="people",
            kind="remaining",
            source="sentence1_goers",
            honorific=False,
            scope=Scope.all(),
        ),
        slot_use={
            "primary": ("display", "control"),
            "group_count": ("display", "control"),
            "guide": ("display", "control"),
            "room": ("display", "action"),
        },
        pending=(),
        notes=(
            "“Everyone else”字面上是所有人的补集，不是“来宾”的补集。",
            "领队与来宾的句法角色对调：领队成了受话并去会合的一方。",
        ),
    ),
    "V3": Semantic(
        roles={
            "primary": "被引领的来宾本人",
            "group_count": "被写成“你的 N 人团”，读法是整团人数",
            "guide": "实际领队，随行到目的地",
            "room": "已登记的目的地",
        },
        split=_split(
            goers=("primary", "companions", "guide"),
            party_displayed=True,
            party_known=True,
            party_includes_primary=True,
            universe="guests",
            kind="remaining",
            source="sentence1_goers",
            honorific=False,
            scope=Scope.all(),
        ),
        slot_use={
            "primary": ("display", "control"),
            "group_count": ("display", "control"),
            "guide": ("display", "control"),
            "room": ("display", "action"),
        },
        pending=(),
        notes=(
            "字段约定 group_count 不含本人；“your party of N”读成整团 N 人，会把一人留在原处。",
        ),
    ),
    "V4": Semantic(
        roles={
            "primary": "被引领的来宾本人",
            "group_count": "本人的同行人数，不含本人",
            "guide": "实际领队，随行到目的地",
            "room": "已登记的目的地",
        },
        split=_split(
            goers=("primary", "companions", "guide"),
            party_displayed=True,
            party_known=True,
            party_includes_primary=False,
            universe="guests",
            kind="relative",
            source="addressee",
            honorific=False,
            scope=Scope.party(),
        ),
        slot_use={
            "primary": ("display", "control"),
            "group_count": ("display", "control"),
            "guide": ("display", "control"),
            "room": ("display", "action"),
        },
        pending=(),
        notes=(
            "“Other guests”是相对受话者说的另一批来宾，尚未明确是同行集合的补集。",
            "“N companion(s)”要求 N>=1，因此只覆盖成组来宾；单独来宾需要使用另一条短写法。",
        ),
    ),
    "V5": Semantic(
        roles={
            "primary": "被引领的来宾本人",
            "guide": "实际领队，随行到目的地",
            "room": "已登记的目的地",
        },
        split=_split(
            goers=("primary", "companions", "guide"),
            party_displayed=False,
            party_known=False,
            party_includes_primary=None,
            universe="people",
            kind="remaining",
            source="sentence1_goers",
            honorific=False,
            scope=Scope.party(),
        ),
        slot_use={
            "primary": ("display", "control"),
            "guide": ("display", "control"),
            "room": ("display", "action"),
        },
        pending=(
            "role_hint 未决：“一行”是否可以为单人。",
            "短句不显示 group_count，无法核实同行人数是否与登记一致。",
            "“其他人”不是“来宾”的补集，可能把工作人员也算进来。",
        ),
        notes=(
            "即使写成成组短句，单人读法未确认，不能当作通用文案。",
        ),
    ),
    "V8": Semantic(
        roles={
            "primary": "被引领的来宾本人",
            "guide": "实际领队，随行到目的地",
            "room": "已登记的目的地",
        },
        split=_split(
            goers=("primary", "guide"),
            party_displayed=False,
            party_known=True,
            party_includes_primary=False,
            universe="guests",
            kind="relative",
            source="addressee",
            honorific=False,
            scope=Scope.solo(),
        ),
        slot_use={
            "primary": ("display", "control"),
            "guide": ("display", "control"),
            "room": ("display", "action"),
            "group_count": ("control",),
        },
        pending=(),
        notes=(
            "“on your own”已说明没有同行者；group_count 在这里只用于确定适用范围，不显示。",
        ),
    ),
    "V9": Semantic(
        roles={
            "primary": "被引领的来宾本人",
            "group_count": "本人的同行人数，不含本人",
            "guide": "实际领队，随行到目的地",
            "room": "已登记的目的地",
            "honorific": "须经本人同意的身份称呼；未获同意时不能显示",
        },
        split=_split(
            goers=("primary", "companions", "guide"),
            party_displayed=True,
            party_known=True,
            party_includes_primary=False,
            universe="guests",
            kind="relative",
            source="addressee",
            honorific=True,
            scope=Scope.consented_party(),
        ),
        slot_use={
            "primary": ("display", "control"),
            "group_count": ("display", "control"),
            "guide": ("display", "control"),
            "room": ("display", "action"),
            "honorific": ("display",),
        },
        pending=(),
        notes=(
            "applies_to 只要求有经本人同意的称呼，但正文的“N companion(s)”还要求至少一名同行者；单人情形不成立。",
            "没有获准的中性写法时，不能以空称呼顶替后继续把短句用于无称呼的来宾。",
        ),
    ),
}


@dataclass(frozen=True)
class Semantics:
    """一份已认可的读法登记。测试与临时更正可以扩展，不改模块源码。"""

    declarations: Mapping[str, Semantic] = field(default_factory=lambda: dict(DECLARED))

    def get(self, copy_id: str) -> Semantic | None:
        return self.declarations.get(copy_id)

    def with_declaration(self, copy_id: str, semantic: Semantic) -> "Semantics":
        merged = dict(self.declarations)
        merged[copy_id] = semantic
        return Semantics(merged)


DEFAULT_SEMANTICS = Semantics()

def make_route_semantic(
    *,
    scope: Scope,
    roles: Mapping[str, str] | None = None,
    goers: Iterable[str] = ("primary", "companions", "guide"),
    party_displayed: bool = True,
    party_known: bool = True,
    party_includes_primary: bool | None = False,
    universe: str = "guests",
    kind: str = "remaining",
    source: str = "sentence1_goers",
    honorific: bool = False,
    slot_use: Mapping[str, tuple[str, ...]] | None = None,
    pending: tuple[str, ...] = (),
    notes: tuple[str, ...] = (),
) -> Semantic:
    """为新近认可的路线短句登记读法。

    正式提交要求两种语言来自同一份认可覆盖；用这个工厂可以把编辑确认后的
    新短句纳入核对，而不必改动现有登记。
    """
    if roles is None:
        roles = {
            "primary": "被引领的来宾本人",
            "group_count": "同行者人数，不含本人",
            "guide": "实际领队，随行到目的地",
            "room": "已登记的目的地",
        }
    if slot_use is None:
        slot_use = {
            "primary": ("display", "control"),
            "group_count": ("display", "control") if party_displayed else ("control",),
            "guide": ("display", "control"),
            "room": ("display", "action"),
        }
        if honorific:
            slot_use = dict(slot_use)
            slot_use["honorific"] = ("display",)
    return Semantic(
        roles=dict(roles),
        split=_split(
            goers=goers,
            party_displayed=party_displayed,
            party_known=party_known,
            party_includes_primary=party_includes_primary,
            universe=universe,
            kind=kind,
            source=source,
            honorific=honorific,
            scope=scope,
        ),
        slot_use=dict(slot_use),
        pending=pending,
        notes=notes,
    )



# ---------------------------------------------------------------------------
# 未知短句的浅层角色推断：只用于发现“变量名齐全但角色被换”。
# ---------------------------------------------------------------------------

def infer_leader_follower(text: str, locale: str) -> tuple[str | None, str | None, str | None]:
    leader = follower = destination = None
    if locale == "zh":
        match = re.search(r"(?:跟|随)\s*\{(\w+)\}\s*(?:往|到)", text)
        if match:
            leader = match.group(1)
        match = re.search(r"(?:请|让)\s*\{(\w+)\}", text)
        if match:
            follower = match.group(1)
        match = re.search(r"(?:往|到)\s*\{(\w+)\}", text)
        if match:
            destination = match.group(1)
    else:
        match = re.match(
            r"\s*(?:\{\w+\}\s+)?\{(\w+)\}\s*(?:and[^,]*)?,\s*(?:please\s+)?(?:join|follow)\b",
            text,
            re.IGNORECASE,
        )
        if match:
            follower = match.group(1)
        match = re.search(r"follow\s+\{(\w+)\}", text, re.IGNORECASE)
        if match:
            leader = match.group(1)
        else:
            match = re.search(r"join\s+\{(\w+)\}", text, re.IGNORECASE)
            if match:
                leader = match.group(1)
        match = re.search(r"(?:to|at)\s+\{(\w+)\}", text, re.IGNORECASE)
        if match:
            destination = match.group(1)
    return leader, follower, destination


def _placeholder_findings(
    variant: CopyVariant,
    expectation: MessageExpectation | None,
    parts: TemplateParts,
) -> tuple[list[Check], list[Flag]]:
    checks: list[Check] = []
    flags: list[Flag] = []
    fields = set(parts.unique_fields)

    known = set(DECLARED["V1"].roles) | {"honorific"}
    unknown = sorted(fields - known)
    checks.append(
        Check(
            "placeholders_known",
            not unknown,
            "变量均在登记值域内" if not unknown else f"出现未登记变量: {', '.join(unknown)}",
        )
    )
    if unknown:
        flags.append(
            Flag("P001", ERROR, f"{variant.label}: 出现未登记变量 {', '.join(unknown)}，设备无对应值")
        )

    if expectation is not None:
        required = set(expectation.required_placeholders)
        allowed = required | set(expectation.optional_placeholders)
        missing_required = sorted(required - fields)
        extra = sorted(fields - allowed)
        ok = not missing_required and not extra
        detail = "变量集合符合消息约定"
        if missing_required:
            detail = f"缺少消息约定变量: {', '.join(missing_required)}"
        if extra:
            detail = (detail + "；" if detail else "") + f"多出变量: {', '.join(extra)}"
        checks.append(Check("placeholder_contract", ok, detail))
        if not ok:
            flags.append(Flag("P002", ERROR, f"{variant.label}: {detail}"))

        leader, follower, destination = infer_leader_follower(variant.text, variant.locale)
        role_ok = True
        role_detail = "未从句子结构中发现角色调换"
        if expectation.leader_key and leader and leader != expectation.leader_key:
            role_ok = False
            role_detail = f"领队位置被写成 {leader!r}，应为 {expectation.leader_key!r}"
        if expectation.follower_key and follower and follower != expectation.follower_key:
            role_ok = False
            role_detail = (
                (role_detail + "；" if role_detail else "")
                + f"跟随者位置被写成 {follower!r}，应为 {expectation.follower_key!r}"
            )
        if expectation.destination_key and destination and destination != expectation.destination_key:
            role_ok = False
            role_detail = (
                (role_detail + "；" if role_detail else "")
                + f"目的地位置被写成 {destination!r}，应为 {expectation.destination_key!r}"
            )
        checks.append(Check("role_positions", role_ok, role_detail))
        if not role_ok:
            flags.append(
                Flag(
                    "P003",
                    ERROR,
                    f"{variant.label}: {role_detail}。变量名齐全不等于角色正确。",
                )
            )
    else:
        checks.append(Check("placeholder_contract", False, "消息无登记契约，需编辑确认变量身份"))
        flags.append(Flag("P004", WARN, f"{variant.label}: 消息 {variant.message_id!r} 无登记语义契约"))

    return checks, flags


def analyze_copy(
    variant: CopyVariant, semantics: Semantics = DEFAULT_SEMANTICS
) -> CopyAnalysis:
    """核对一条短句。不会为了通过而改写原文。"""
    try:
        parts = parse_template(variant.text)
    except Exception as exc:  # noqa: BLE001 - 保留原句并交给编辑
        return CopyAnalysis(
            variant=variant,
            split=None,
            checks=(Check("template_parses", False, str(exc)),),
            flags=(Flag("T001", ERROR, f"{variant.label}: 模板无法解析: {exc}"),),
            pending=("模板无法解析，需人工读取。",),
            slot_use={},
        )

    expectation = MESSAGE_EXPECTATIONS.get(variant.message_id)
    checks, flags = _placeholder_findings(variant, expectation, parts)

    if expectation and expectation.kind == "static_assist":
        if parts.unique_fields:
            flags.append(Flag("S001", ERROR, f"{variant.label}: 退回/静态消息不应依赖来宾变量"))
        checks.append(Check("static_message", not parts.unique_fields, "静态退回消息不依赖变量"))
        return CopyAnalysis(
            variant=variant,
            split=None,
            checks=tuple(checks),
            flags=tuple(flags),
            pending=(),
            slot_use={},
        )

    declared = semantics.get(variant.id)
    if declared is None:
        leader, follower, _ = infer_leader_follower(variant.text, variant.locale)
        pending = ["新短句没有已认可的语义声明，需编辑确认读法。"]
        if expectation is not None and expectation.leader_key:
            if leader and leader != expectation.leader_key:
                flags.append(
                    Flag(
                        "P003",
                        ERROR,
                        f"{variant.label}: 领队位置写成了 {leader!r}，不是约定的 {expectation.leader_key!r}。",
                    )
                )
            elif leader == expectation.leader_key and follower == expectation.follower_key:
                pending.append("句子结构看似把领队和跟随者放在约定位置，但仍需确认第二句范围。")
        pending.append("仍可服务的来宾类型无法从文本确认；请按同行人数与身份称呼逐类核对。")
        checks.append(Check("declared_reading", False, "没有已登记读法"))
        checks.append(Check("role_positions_unverified", False, "角色位置只能浅层推断"))
        return CopyAnalysis(
            variant=variant,
            split=None,
            checks=tuple(checks),
            flags=tuple(flags),
            pending=tuple(pending),
            slot_use={},
        )

    split = declared.split
    checks.extend(
        [
            Check("declared_reading", True, "采用已登记读法"),
            Check(
                "party_size_semantics",
                split.party_size_includes_primary is False or not split.party_size_displayed,
                "同组人数不含本人" if split.party_size_includes_primary is False else "人数读法需确认",
            ),
            Check(
                "complement_scope",
                split.complement_universe == "guests" and split.complement_kind == "remaining",
                "第二句是同行集合在来宾中的补集"
                if split.complement_universe == "guests" and split.complement_kind == "remaining"
                else "第二句范围与第一句不一致或指代含糊",
            ),
            Check("scope_declared", True, f"范围: {split.scope.label}"),
        ]
    )

    if split.party_size_displayed and split.party_size_includes_primary is not False:
        flags.append(
            Flag(
                "G001",
                ERROR,
                f"{variant.label}: group_count 的读法与字段约定冲突（约定不含本人），会把同行者范围算错。",
            )
        )
    if split.complement_universe != "guests":
        flags.append(
            Flag(
                "G002",
                WARN,
                f"{variant.label}: 第二句补集是“{split.complement_universe}”，不是“来宾”；集合范围与第一句不一致。",
            )
        )
    if split.complement_kind != "remaining":
        flags.append(
            Flag(
                "G003",
                WARN,
                f"{variant.label}: 第二句用“另一批人”而非“剩余来宾”，读者无法确定是否与第一句同一群体。",
            )
        )
    if not split.party_size_known:
        flags.append(
            Flag("G004", ERROR, f"{variant.label}: 不显示同行人数，无法核实集合归属。")
        )
    if "group_count" in parts.unique_fields and "display" not in declared.slot_use.get("group_count", ()):
        flags.append(
            Flag(
                "G005",
                WARN,
                f"{variant.label}: group_count 出现在句中却只被当作分流变量，读者看不到人数。",
            )
        )
    if "group_count" not in parts.unique_fields and variant.message_id == "guide-7":
        flags.append(
            Flag(
                "G006",
                INFO,
                f"{variant.label}: 不显示 group_count；若消息登记要求该值，设备仍需传入，但不得据此扩大同行范围。",
            )
        )

    declared_scope, applies_known = parse_applies_to(variant.applies_to)
    if variant.applies_to and not applies_known:
        flags.append(
            Flag("C001", WARN, f"{variant.label}: 适用条件 {variant.applies_to!r} 无法判定，需编辑确认。")
        )
    elif declared_scope is not None and declared_scope != split.scope:
        if split.scope.subsumes(declared_scope):
            flags.append(
                Flag(
                    "C002",
                    WARN,
                    f"{variant.label}: 正文要求比 applies_to（{declared_scope.label}）更窄的"
                    f"“{split.scope.label}”；按较窄范围使用。",
                )
            )
        else:
            flags.append(
                Flag(
                    "C003",
                    ERROR,
                    f"{variant.label}: 正文范围“{split.scope.label}”与 applies_to"
                    f"“{declared_scope.label}”不一致，可能把其他人卷入或排除。",
                )
            )

    if declared.pending:
        flags.append(
            Flag("E001", WARN, f"{variant.label}: 读法未决，不能作为交付主文案。")
        )

    if re.search(r"\(\w+\)", variant.text):
        flags.append(
            Flag(
                "M001",
                WARN,
                f"{variant.label}: 用括号兼写单复数；0、1、多人的英文形式无法由同一次替换全部覆盖，"
                "需按人数另写短句后才能交付。",
            )
        )

    return CopyAnalysis(
        variant=variant,
        split=split,
        checks=tuple(checks),
        flags=tuple(flags),
        pending=declared.pending,
        slot_use=declared.slot_use,
    )


# ---------------------------------------------------------------------------
# 双语句对
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BilingualPair:
    message_id: str
    zh: CopyVariant
    en: CopyVariant
    status: str  # match / conditional / mismatch
    joint_scope: Scope | None
    reasons: tuple[str, ...]
    checks: tuple[Check, ...]
    zh_analysis: CopyAnalysis
    en_analysis: CopyAnalysis

    @property
    def label(self) -> str:
        return f"{self.zh.id} ↔ {self.en.id}"

    @property
    def hard_failures(self) -> tuple[str, ...]:
        if self.joint_scope is None:
            return ("两种语言的适用条件互不相交",)
        out = [f"{check.id}: {check.detail}" for check in self.checks if check.id in HARD_CHECKS and not check.ok]
        for analysis in (self.zh_analysis, self.en_analysis):
            out.extend(flag.message for flag in analysis.errors)
            out.extend(analysis.pending)
        return tuple(dict.fromkeys(out))

    @property
    def soft_failures(self) -> tuple[str, ...]:
        return tuple(
            f"{check.id}: {check.detail}"
            for check in self.checks
            if check.id in SOFT_CHECKS and not check.ok
        )

    @property
    def scope(self) -> Scope | None:
        return self.joint_scope

    @property
    def has_complement_issue(self) -> bool:
        return any(
            check.id in ("complement_universe", "complement_reference") and not check.ok
            for check in self.checks
        )

    @property
    def complement_issues(self) -> tuple[str, ...]:
        return tuple(
            f"{check.id}: {check.detail}"
            for check in self.checks
            if check.id in ("complement_universe", "complement_reference") and not check.ok
        )

    @property
    def form_issues(self) -> tuple[str, ...]:
        """不能靠编辑签署放行、必须另写短句的形态问题。"""
        return tuple(
            f"{check.id}: {check.detail}"
            for check in self.checks
            if check.id in ("party_size_capacity", "zero_companion_wording") and not check.ok
        )

    @property
    def has_capacity_issue(self) -> bool:
        return any(
            check.id == "party_size_capacity" and not check.ok for check in self.checks
        )

    @property
    def needs_editor(self) -> bool:
        return (
            self.status == "conditional"
            or bool(self.soft_failures)
            or bool(self.zh_analysis.pending)
            or bool(self.en_analysis.pending)
        )

    @property
    def usable(self) -> bool:
        return self.status != "mismatch" and not self.hard_failures


# 变量名集合不同本身不构成失败：例如单独来宾短句不需要显示 group_count。
HARD_CHECKS = frozenset(
    {
        "same_goers",
        "party_size_reading",
        "honorific",
        "complement_universe",
        "same_action",
    }
)
# 指代是否明确、英美人称形态、单人措辞等，需要编辑确认或另写短句。
SOFT_CHECKS = frozenset(
    {
        "complement_reference",
        "party_size_capacity",
        "zero_companion_wording",
    }
)


def effective_goers(split: GroupSplit, scope: Scope) -> frozenset[str]:
    """在给定范围里实际会一起走的人。

    单独来宾范围内没有同行者，companions 是空集，不应被当成另一群人。
    """
    goers = set(split.goers)
    if scope.max_count == 0:
        goers.discard("companions")
    return frozenset(goers)


def _party_size_equivalent(zh: CopyAnalysis, en: CopyAnalysis, joint: Scope) -> tuple[bool, str]:
    a, b = zh.split, en.split
    assert a is not None and b is not None
    if a.party_size_includes_primary != b.party_size_includes_primary:
        return False, "同行人数读法不同：一侧含本人"
    if a.party_size_displayed and b.party_size_displayed:
        return True, "两侧都显示同行人数，且都不含本人"
    # 一侧不显示人数时，只有该句把人数固定为 0（单独来宾）才不存在歧义。
    if a.party_size_displayed and not b.party_size_displayed:
        if b.party_size_known and joint.max_count == 0:
            return True, "一侧不显示人数，但其范围固定为单独来宾，人数只能是 0"
        return False, "只有一侧显示同行人数，另一侧无法核实集合大小"
    if b.party_size_displayed and not a.party_size_displayed:
        if a.party_size_known and joint.max_count == 0:
            return True, "一侧不显示人数，但其范围固定为单独来宾，人数只能是 0"
        return False, "只有一侧显示同行人数，另一侧无法核实集合大小"
    if a.party_size_known and b.party_size_known:
        return True, "两侧都不显示人数，但范围共同确定了人数"
    return False, "两侧都不显示同行人数，无法核实集合大小"


def _pair_checks(zh: CopyAnalysis, en: CopyAnalysis, joint: Scope) -> tuple[Check, ...]:
    checks: list[Check] = []
    checks.append(
        Check(
            "same_placeholders",
            set(template_fields(zh.variant.text)) == set(template_fields(en.variant.text)),
            "两种语言的变量名集合相同"
            if set(template_fields(zh.variant.text)) == set(template_fields(en.variant.text))
            else "两种语言的变量名集合不同；名称齐全与否本身不证明集合归属",
        )
    )
    if zh.split is None or en.split is None:
        checks.append(Check("set_membership", False, "至少一侧没有可核对的集合声明"))
        return tuple(checks)
    a, b = zh.split, en.split
    zh_goers = effective_goers(a, joint)
    en_goers = effective_goers(b, joint)
    checks.append(
        Check(
            "same_goers",
            zh_goers == en_goers and a.guide_goes == b.guide_goes,
            "同行集合与领队身份一致"
            if zh_goers == en_goers
            else f"同行集合不同：中文 {sorted(zh_goers)} / 英文 {sorted(en_goers)}",
        )
    )
    size_ok, size_detail = _party_size_equivalent(zh, en, joint)
    checks.append(Check("party_size_reading", size_ok, size_detail))
    checks.append(
        Check(
            "party_size_capacity",
            _capacity_ok(zh, en, joint),
            "两条短句都能覆盖此范围内的同行人数形式"
            if _capacity_ok(zh, en, joint)
            else "英文单复数或人数上界无法由同一次替换覆盖",
        )
    )
    checks.append(
        Check(
            "complement_universe",
            a.complement_universe == b.complement_universe,
            "第二句补集属于同一类人（来宾）"
            if a.complement_universe == b.complement_universe
            else f"第二句补集一侧是“{a.complement_universe}”、另一侧是“{b.complement_universe}”",
        )
    )
    reference_ok = a.complement_kind == b.complement_kind and a.complement_source == b.complement_source
    if (
        not reference_ok
        and joint.max_count == 0
        and a.complement_universe == b.complement_universe == "guests"
    ):
        # 单独来宾范围内，“其余来宾”与“其他来宾”都是除被称呼者之外的来宾。
        reference_ok = True
    checks.append(
        Check(
            "complement_reference",
            reference_ok,
            "第二句沿第一句的同行集合取剩余来宾"
            if reference_ok
            else "第二句以“另一批人”或受称呼者之外指代，未明确是同行集合的剩余来宾",
        )
    )
    checks.append(
        Check(
            "zero_companion_wording",
            not (
                joint.max_count == 0
                and "group_count" in template_fields(zh.variant.text)
            ),
            "单独来宾不需要显示同行人数"
            if not (
                joint.max_count == 0
                and "group_count" in template_fields(zh.variant.text)
            )
            else "中文短句在单独来宾时会显示“0 位同行者”，措辞需另写单人短句",
        )
    )
    checks.append(
        Check(
            "same_action",
            a.action == b.action,
            "两种语言的动作意图一致" if a.action == b.action else "两种语言的动作意图不同",
        )
    )
    checks.append(
        Check(
            "honorific",
            a.honorific_slot == b.honorific_slot,
            "身份称呼词位一致"
            if a.honorific_slot == b.honorific_slot
            else "一侧有称呼词位、另一侧没有；切换语言会丢失或凭空增加称呼",
        )
    )
    return tuple(checks)


def _capacity_ok(zh: CopyAnalysis, en: CopyAnalysis, joint: Scope) -> bool:
    """英文的 (s) 写法不能同时覆盖 0/1/多人。

    这是替换层的问题，不是集合归属问题，因此是软问题：交付前需另写按人数
    区分的短句，或由编辑确认该范围实际只会出现一种形式。
    """
    if not re.search(r"\(\w+\)", en.variant.text):
        return True
    low = joint.min_count
    high = joint.max_count
    if high is not None and low == high:
        return True
    return False


def _is_static(analysis: CopyAnalysis) -> bool:
    check = analysis.check("static_message")
    return bool(check and check.ok)


def analyze_pair(zh: CopyAnalysis, en: CopyAnalysis) -> BilingualPair:
    if _is_static(zh) and _is_static(en):
        checks = (
            Check("same_placeholders", True, "两侧都是不含变量的静态短句"),
            Check("static_both", True, "两种语言都是独立的退回/静态消息"),
        )
        return BilingualPair(
            message_id=zh.variant.message_id,
            zh=zh.variant,
            en=en.variant,
            status="match",
            joint_scope=Scope.all(),
            reasons=("两侧都是独立静态短句，不依赖任何来宾值",),
            checks=checks,
            zh_analysis=zh,
            en_analysis=en,
        )
    if _is_static(zh) or _is_static(en):
        return BilingualPair(
            message_id=zh.variant.message_id,
            zh=zh.variant,
            en=en.variant,
            status="mismatch",
            joint_scope=None,
            reasons=("一侧是静态消息、另一侧引用来宾变量，不能成对",),
            checks=(Check("static_both", False, "只有一侧是静态短句"),),
            zh_analysis=zh,
            en_analysis=en,
        )
    joint = zh.scope.intersect(en.scope)
    checks = _pair_checks(zh, en, joint) if joint is not None else (
        Check("same_placeholders", False, "范围互不相交，无法作为同一句对"),
    )
    reasons: list[str] = []
    status = "match"

    hard = [check for check in checks if check.id in HARD_CHECKS and not check.ok]
    if joint is None:
        reasons.append(
            f"中文范围“{zh.scope.label}”与英文范围“{en.scope.label}”没有交集"
        )
        status = "mismatch"
    elif zh.scope != en.scope:
        wider = "中文" if zh.scope.covers(en.scope) else ("英文" if en.scope.covers(zh.scope) else "两侧")
        reasons.append(
            f"覆盖范围不同：中文“{zh.scope.label}”、英文“{en.scope.label}”；"
            f"{wider}更宽，只能在共同范围“{joint.label}”内作为草稿核对，"
            "正式提交须换成同一覆盖的两种语言"
        )
        status = "conditional"
    if hard:
        reasons.extend(check.detail for check in hard)
        status = "mismatch"
    for analysis in (zh, en):
        if analysis.errors:
            reasons.extend(flag.message for flag in analysis.errors)
            status = "mismatch"
        if analysis.pending:
            reasons.extend(analysis.pending)
            status = "mismatch"

    soft = [check for check in checks if check.id in SOFT_CHECKS and not check.ok]
    if soft and status != "mismatch":
        status = "conditional"
        reasons.extend(check.detail for check in soft)
    if status == "match":
        if joint is not None and not joint.is_unrestricted:
            reasons.append(
                f"两种语言在同一覆盖范围“{joint.label}”内等价；只能按该范围提交，不得扩大"
            )
        else:
            reasons.append("两种语言声明同一集合、同一人数读法、同一补集")

    return BilingualPair(
        message_id=zh.variant.message_id,
        zh=zh.variant,
        en=en.variant,
        status=status,
        joint_scope=joint,
        reasons=tuple(dict.fromkeys(reasons)),
        checks=tuple(checks),
        zh_analysis=zh,
        en_analysis=en,
    )


def bilingual_matches(
    workbench: Workbench,
    message_id: str,
    semantics: Semantics = DEFAULT_SEMANTICS,
) -> list[BilingualPair]:
    """枚举同一消息的中英组合，逐项说明在什么范围里真的等价。"""
    analyses = {
        variant.id: analyze_copy(variant, semantics) for variant in workbench.copybook
    }
    zh_list = [v for v in workbench.copybook if v.message_id == message_id and v.locale == "zh"]
    en_list = [v for v in workbench.copybook if v.message_id == message_id and v.locale == "en"]
    return [
        analyze_pair(analyses[zh.id], analyses[en.id])
        for zh in zh_list
        for en in en_list
    ]


# ---------------------------------------------------------------------------
# 整本编校稿
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MessageReview:
    message_id: str
    analyses: tuple[CopyAnalysis, ...]
    pairs: tuple[BilingualPair, ...]

    @property
    def clean_pairs(self) -> tuple[BilingualPair, ...]:
        return tuple(pair for pair in self.pairs if pair.status == "match")

    @property
    def conditional_pairs(self) -> tuple[BilingualPair, ...]:
        return tuple(pair for pair in self.pairs if pair.status == "conditional")

    @property
    def usable_pairs(self) -> tuple[BilingualPair, ...]:
        return tuple(pair for pair in self.pairs if pair.usable)

    @property
    def pending_variants(self) -> tuple[CopyAnalysis, ...]:
        return tuple(item for item in self.analyses if item.pending or item.errors)


@dataclass(frozen=True)
class Review:
    workbench: Workbench
    analyses: Mapping[str, CopyAnalysis]
    messages: Mapping[str, MessageReview]

    @property
    def flags(self) -> tuple[Flag, ...]:
        seen: set[tuple[str, str, str]] = set()
        output: list[Flag] = []
        for analysis in self.analyses.values():
            for flag in analysis.flags:
                key = (flag.code, flag.severity, flag.message)
                if key not in seen:
                    seen.add(key)
                    output.append(flag)
        return tuple(output)

    @property
    def errors(self) -> tuple[Flag, ...]:
        return tuple(flag for flag in self.flags if flag.severity == ERROR)

    @property
    def warnings(self) -> tuple[Flag, ...]:
        return tuple(flag for flag in self.flags if flag.severity == WARN)

    @property
    def deliverable_variants(self) -> tuple[CopyAnalysis, ...]:
        return tuple(item for item in self.analyses.values() if item.deliverable)

    def analysis(self, copy_id: str) -> CopyAnalysis:
        return self.analyses[copy_id]

    def message(self, message_id: str) -> MessageReview:
        return self.messages[message_id]


def review_copybook(
    workbench: Workbench, semantics: Semantics = DEFAULT_SEMANTICS
) -> Review:
    analyses = {
        variant.id: analyze_copy(variant, semantics) for variant in workbench.copybook
    }
    message_ids = sorted({variant.message_id for variant in workbench.copybook})
    messages: dict[str, MessageReview] = {}
    for message_id in message_ids:
        messages[message_id] = MessageReview(
            message_id=message_id,
            analyses=tuple(
                analyses[variant.id]
                for variant in workbench.copybook
                if variant.message_id == message_id
            ),
            pairs=tuple(bilingual_matches(workbench, message_id, semantics)),
        )
    return Review(workbench=workbench, analyses=analyses, messages=messages)


