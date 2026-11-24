"""从两种语言的短句里识别每个变量实际指的谁、被谁修饰。

这里做的是可复核的浅层语义标注，不调用外部服务。每条模板得到：

* 变量在句中的角色（被领者、领队、目的地、同行人数、可选称呼）；
* 受话人、动作、以及“其余来宾”的范围与去向；
* 数值/量化线索，用来发现 ``group_count`` 被算进了本人，
  或 ``primary`` 被换成了领队；
* 每个词位只是显示给读者看，还是同时决定谁跟谁走；
* 无法确认的读法：只交付编辑，并列出它仍可服务的来宾类型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable

from .model import CANONICAL_SLOT_ROLES, Placeholder, Template, VisitRequest


# ---------------------------------------------------------------------------
# 词位表。规则只声明它看到什么，不替编辑决定。
# ---------------------------------------------------------------------------

SLOT_CUES: dict[str, dict[str, tuple[str, ...]]] = {
    "primary": {
        "zh": ("来宾", "您", "请", "随", "跟", "和", "与", "一行", "位", "先生", "女士"),
        "en": ("please", "follow", "join", "you", "your", "guest", "with"),
    },
    "guide": {
        "zh": ("老师", "领队", "导游", "向", "往", "到", "带", "引导"),
        "en": ("guide", "please", "follow", "lead", "escort", "join", "at"),
    },
    "room": {
        "zh": ("往", "到", "在", "厅", "室", "入口", "馆", "台", "处"),
        "en": ("to", "at", "in", "room", "hall", "desk", "entrance", "garden"),
    },
    "group_count": {
        "zh": ("位", "名", "人", "一行", "同行", "随行", "同伴"),
        "en": ("guest", "guests", "companion", "companions", "party", "other", "person", "people"),
    },
}

# 只有显示作用、不改变动作的变量。
DISPLAY_ONLY_SLOTS = frozenset({"honorific"})

# 领队/被领者角色互换的典型句式。
_INVERSION_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "en_guide_as_addressee",
        re.compile(
            r"^\s*\{guide\}\s*,\s*please\s+(?:join|follow|come\s+with|accompany)\b",
            re.IGNORECASE,
        ),
        "英文把领队放在受话人位置，请领队去加入来宾；这与中文“来宾随领队走”相反。",
    ),
    (
        "zh_guide_as_addressee",
        re.compile(r"\{guide\}[^。！？]{0,8}(?:请|麻烦)(?:您)?(?:跟|随)"),
        "中文把领队放在受话人位置，请领队跟着别人走。",
    ),
    (
        "zh_primary_leads",
        re.compile(r"请?\s*\{primary\}[^。！？]{0,6}(?:带|领|引)"),
        "中文让被领者去带领队，角色对调。",
    ),
)

_REST_PATTERNS: dict[str, re.Pattern[str]] = {
    "zh": re.compile(r"(其余|其他|剩下|余下|别的)"),
    "en": re.compile(
        r"\b(everyone\s+else|everybody\s+else|all\s+(?:the\s+)?(?:other|remaining)"
        r"|other\s+guests|remaining\s+guests|the\s+rest)\b",
        re.IGNORECASE,
    ),
}

_REST_STAY: dict[str, re.Pattern[str]] = {
    "zh": re.compile(r"(留|等候|等待|原地)"),
    "en": re.compile(r"\b(stay|stays|wait|waits|remain|remains)\b", re.IGNORECASE),
}

_REST_LEAVE: dict[str, re.Pattern[str]] = {
    "zh": re.compile(r"(一起|随行|同往|前往)"),
    "en": re.compile(r"\b(come\s+along|join\s+us|follow)\b", re.IGNORECASE),
}

_SOLO_PATTERNS: dict[str, re.Pattern[str]] = {
    "zh": re.compile(r"(独自|单独|一人|一个人|自行|自己)"),
    "en": re.compile(r"\b(on\s+your\s+own|by\s+yourself|alone|single|solo)\b", re.IGNORECASE),
}

# 句式本身存在二读、必须交给编辑选定的问题代码。
AMBIGUITY_CODES = frozenset({"zh_line_party", "en_with_your_party"})

_PARTY_HEAD_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "zh_line_party",
        re.compile(r"\{primary\}\s*一行"),
        "“一行”是否包含本人未写明；若按全体理解，同行人数会多算一人。",
    ),
    (
        "en_with_your_party",
        re.compile(r"\bwith\s+your\s+party\s+of\s+\{group_count\}", re.IGNORECASE),
        "“your party of N”通常含本人，和字段约定的“不含本人”冲突。",
    ),
)

_ARTICLE_BEFORE_SLOT = re.compile(r"(?i)(^|[\s(])(a|an)\s+\{([A-Za-z_]+)\}")
_VOWEL_LETTERS = set("aeiouAEIOU")
_PLURAL_SUFFIX = re.compile(r"(guest|companion)\(s\)")


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str  # "error" | "warning" | "note"
    message: str
    slot: str | None = None

    @property
    def blocking(self) -> bool:
        return self.severity == "error"


@dataclass
class SlotReading:
    name: str
    theme: str
    function: str
    modified_by: tuple[str, ...] = ()
    governs: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()

    @property
    def role_is_canonical(self) -> bool:
        expected = CANONICAL_SLOT_ROLES.get(self.name)
        return expected is None or self.theme == expected

    @property
    def affects_action(self) -> bool:
        """不只是显示：这个词位决定谁跟谁走到哪里。"""

        return self.name not in DISPLAY_ONLY_SLOTS


@dataclass
class Analysis:
    template: Template
    slots: dict[str, SlotReading] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    addressee: str | None = None
    rest_scope: str | None = None
    rest_action: str | None = None
    action: str = "guide_led_guest_to_room"
    conditional_slots: dict[str, object] = field(default_factory=dict)

    @property
    def locale(self) -> str:
        return self.template.locale

    @property
    def is_ambiguous(self) -> bool:
        """句式在谁算进群体上仍有二读，编辑选定前不能当作已定译法。"""

        return any(
            issue.code.startswith("ambiguous_") or issue.code in AMBIGUITY_CODES
            for issue in self.issues
        )

    @property
    def blocking_issues(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.blocking]

    @property
    def companion_count_includes_primary(self) -> bool:
        return any(issue.code == "en_with_your_party" for issue in self.issues)

    @property
    def fallback_only(self) -> bool:
        return self.template.is_fallback_copy()

    @property
    def scope_key(self) -> tuple[object, ...]:
        """语言无关的人群范围：条件、称呼要求、其余来宾去向、动作。"""

        return (
            tuple(sorted(self.conditional_slots.items())),
            "honorific" in self.template.placeholders,
            self.rest_action,
            self.action,
        )

    @property
    def coverage_key(self) -> tuple[object, ...]:
        """成对提交所需的完整语义覆盖，比 scope_key 更严。"""

        return self.scope_key + (
            tuple(sorted(self.template.placeholders)),
            self.addressee,
            self.rest_scope is not None,
        )


def _slot_theme(name: str) -> str:
    return CANONICAL_SLOT_ROLES.get(name, "unknown")


def _surrounding(template: Template, name: str, radius: int = 18) -> str:
    for segment in template.segments:
        if isinstance(segment, Placeholder) and segment.name == name:
            start = max(0, segment.start - radius)
            end = min(len(template.text), segment.end + radius)
            return template.text[start:end]
    return template.text


def _clues(template: Template, name: str) -> tuple[str, ...]:
    cues = SLOT_CUES.get(name, {}).get(template.locale, ())
    window = _surrounding(template, name).lower()
    return tuple(cue for cue in cues if cue.lower() in window)


def _find_head(template: Template) -> str | None:
    """主句主语：称呼和地点只是修饰/旁语，不能抢受话人。"""

    text = template.text
    if template.locale == "en":
        head = re.split(r"[,.;]", text, 1)[0]
        named = [m.group(1) for m in re.finditer(r"\{([A-Za-z_]+)\}", head)]
    else:
        head = re.split(r"[。！？，,]", text, 1)[0]
        named = [m.group(1) for m in re.finditer(r"\{([A-Za-z_]+)\}", head)]
    for name in named:
        if name not in ("honorific", "room", "group_count"):
            return name
    return named[0] if named else None


def _rest_details(template: Template, analysis: Analysis) -> None:
    pattern = _REST_PATTERNS.get(template.locale)
    if not pattern:
        return
    match = pattern.search(template.text)
    if not match:
        return
    analysis.rest_scope = match.group(0)
    tail = template.text[match.end() :]
    if template.locale in _REST_STAY and _REST_STAY[template.locale].search(tail):
        analysis.rest_action = "stay"
    elif template.locale in _REST_LEAVE and _REST_LEAVE[template.locale].search(tail):
        analysis.rest_action = "leave_with_guide"


def analyze_copy(template: Template) -> Analysis:
    """给一条模板做角色、量化、条件和可交付性标注。"""

    analysis = Analysis(template=template)
    analysis.addressee = _find_head(template)

    for name in dict.fromkeys(template.placeholders):
        theme = _slot_theme(name)
        function = "quantity" if name == "group_count" else "argument"
        if analysis.addressee == name:
            function = "addressee"
        if name == "room":
            function = "destination"
        if name == "honorific":
            function = "modifier_of_primary"
        if name == "guide":
            function = "leader"
        if name == "primary":
            function = "led_guest"

        governs: tuple[str, ...] = ()
        if name == "honorific":
            governs = ("primary",)
        if name == "group_count":
            window = _surrounding(template, name, 14).lower()
            owns = "primary" if any(
                token in window for token in ("your", "一行", "同组", "同行")
            ) else "guide"
            governs = (owns,)
        modified_by = ("honorific",) if name == "primary" and "honorific" in template.placeholders else ()
        analysis.slots[name] = SlotReading(
            name=name,
            theme=theme,
            function=function,
            modified_by=modified_by,
            governs=governs,
            evidence=_clues(template, name),
        )

    for code, pattern, message in _INVERSION_PATTERNS:
        if pattern.search(template.text):
            analysis.issues.append(Issue(code, "error", message))
    if template.locale not in ("zh", "en"):
        analysis.issues.append(
            Issue("unknown_language", "warning", "不支持的语言，无法核对变量角色。")
        )

    # 导览短句的受话人应是被领者；换成领队就是角色对调。
    if not template.is_fallback_copy() and analysis.addressee == "guide":
        if not any(issue.code == "en_guide_as_addressee" for issue in analysis.issues):
            analysis.issues.append(
                Issue(
                    "addressee_not_primary",
                    "error",
                    "句子的受话人是领队而不是被领者，变量角色被对调。",
                )
            )

    for code, pattern, message in _PARTY_HEAD_PATTERNS:
        if pattern.search(template.text):
            analysis.issues.append(Issue(code, "warning", message, slot="group_count"))
    if template.locale == "en" and _PLURAL_SUFFIX.search(template.text):
        analysis.issues.append(
            Issue(
                "plural_suffix",
                "warning",
                "用 guest(s)/companion(s) 兜住单复数，所有数量都显示同一种写法；"
                "单个替换不能覆盖单数、复数、零和冠词等情形。",
                slot="group_count",
            )
        )
    for match in _ARTICLE_BEFORE_SLOT.finditer(template.text):
        analysis.issues.append(
            Issue(
                "article_depends_on_value",
                "warning",
                f"不定冠词 {match.group(2)} 与 {{{match.group(3)}}} 拼在一起，"
                "值以元音或辅音开头时会选错冠词，需要按值分别成句。",
                slot=match.group(3),
            )
        )

    solo = _SOLO_PATTERNS.get(template.locale)
    if solo and solo.search(template.text):
        analysis.conditional_slots["solo_copy"] = True
        if template.applies_to is None:
            analysis.issues.append(
                Issue(
                    "unconditional_solo",
                    "error",
                    "文案把“独自/on your own”写成通用句，但没有数量条件；成组同行者会被支开。",
                    slot="group_count",
                )
            )

    if template.applies_to:
        if template.applies_to == "group_count==0":
            analysis.conditional_slots["group_count"] = 0
        elif "身份称呼" in template.applies_to or "honorific" in template.applies_to.lower():
            analysis.conditional_slots["honorific_required"] = True
        else:
            analysis.conditional_slots["applies_to_text"] = template.applies_to
            analysis.issues.append(
                Issue(
                    "unknown_condition",
                    "error",
                    f"无法解析适用条件 {template.applies_to!r}，不能确定它覆盖谁。",
                )
            )

    if template.applies_to == "group_count==0" and "group_count" in template.placeholders:
        analysis.issues.append(
            Issue(
                "conditional_uses_variable",
                "warning",
                "条件为 group_count==0 的文案仍引用 group_count，等于把零反复写出来。",
                slot="group_count",
            )
        )

    _rest_details(template, analysis)
    if not template.is_fallback_copy():
        if analysis.rest_scope is None:
            analysis.issues.append(
                Issue(
                    "missing_rest_scope",
                    "warning",
                    "短句没有交代其余来宾怎么办，同一句话里两个群体的范围不完整。",
                )
            )
        elif analysis.rest_action is None:
            analysis.issues.append(
                Issue(
                    "missing_rest_action",
                    "warning",
                    "提到了其余来宾，但没写他们是留下还是同行。",
                )
            )
    return analysis


def analyze_all(templates: Iterable[Template]) -> list[Analysis]:
    return [analyze_copy(template) for template in templates]


# ---------------------------------------------------------------------------
# 把模板放到一条实际请求上评估。
# ---------------------------------------------------------------------------


def assess_request(
    template: Template,
    analysis: Analysis,
    request: VisitRequest,
    required_keys: Iterable[str] = (),
) -> list[Issue]:
    """这条模板用在某条请求上才暴露的问题。

    返回的 issue 里 ``error`` 表示这条请求不能自动交付这条文案。
    """

    issues: list[Issue] = []

    if request.group_count == 0 and "group_count" in template.placeholders:
        issues.append(
            Issue(
                "zero_companions_stated",
                "error",
                "同行者数为零仍写出同行人数，屏幕上会出现“0 位同行者”一类与事实不符的说法。",
                slot="group_count",
            )
        )

    line_party = next(
        (issue for issue in analysis.issues if issue.code == "zh_line_party"), None
    )
    if analysis.companion_count_includes_primary:
        issues.append(
            Issue(
                "party_count_includes_primary",
                "error",
                "“your party of N”把本人算进 N；按字段约定 N 不含本人，"
                "读者会少算一位同行者。",
                slot="group_count",
            )
        )

    if line_party is not None and request.group_count == 0:
        issues.append(
            Issue(
                "party_wording_for_solo",
                "warning",
                "“一行”在单人身上有第二种读法，交付前请编辑选定；"
                "此句仍可服务有成组同行者的来宾。",
                slot="group_count",
            )
        )

    for match in _ARTICLE_BEFORE_SLOT.finditer(template.text):
        name = match.group(3)
        value = str(request.values.get(name) or "")
        article = match.group(2).lower()
        if not value:
            continue
        starts_vowel = value[0] in _VOWEL_LETTERS
        if (article == "an") != starts_vowel:
            issues.append(
                Issue(
                    "article_mismatch",
                    "error",
                    f"“{match.group(2)} {{{name}}}”填入 {value!r} 后冠词不合法，"
                    "需要按读音另写这一句。",
                    slot=name,
                )
            )

    # group_count==0 的专用句用“独自/on your own”表达了人数，不算漏项。
    expressed = set(template.placeholders)
    if analysis.conditional_slots.get("solo_copy"):
        expressed.add("group_count")
    missing_keys = set(required_keys) - expressed
    if missing_keys:
        issues.append(
            Issue(
                "required_key_not_stated",
                "note",
                "这条译文没有呈现设备要求的变量: "
                + ", ".join(sorted(missing_keys))
                + "；另一种语言会多给出这些信息。",
            )
        )

    if request.honorific is None and "honorific" in template.placeholders:
        issues.append(
            Issue(
                "honorific_required_missing",
                "error",
                "来宾没有同意显示身份称呼，这条写法却必须带称呼。",
                slot="honorific",
            )
        )

    if request.public_name_is_chosen and request.display_name != request.data["primary"]:
        issues.append(
            Issue(
                "preferred_name_used",
                "note",
                "使用来宾同意的自选名，未获同意的全名不出现在屏幕上。",
                slot="primary",
            )
        )
    if request.has_unconsented_honorific:
        issues.append(
            Issue(
                "honorific_suppressed",
                "note",
                "来宾提供了称呼但未表示同意，采用不带称呼的中性写法。",
                slot="honorific",
            )
        )
    if request.group_count == 1 and _PLURAL_SUFFIX.search(template.text):
        issues.append(
            Issue(
                "singular_needs_review",
                "note",
                "同行者只有一人，英文的 (s) 写法要由编辑确认读起来是否自然。",
                slot="group_count",
            )
        )
    return issues


# 与具体来宾无关、在屏幕上对任何读者都成立的问题。
TEMPLATE_LEVEL_CODES = frozenset(
    {
        "en_guide_as_addressee",
        "zh_guide_as_addressee",
        "zh_primary_leads",
        "addressee_not_primary",
        "plural_suffix",
        "article_depends_on_value",
        "unconditional_solo",
        "unknown_condition",
        "missing_rest_scope",
        "missing_rest_action",
        "conditional_uses_variable",
        "unknown_language",
    }
)


def issues_for_request(
    template: Template,
    analysis: Analysis,
    request: VisitRequest,
    required_keys: Iterable[str] = (),
) -> list[Issue]:
    """模板自身的问题 + 这条请求才暴露的问题，去重后返回。"""

    merged: list[Issue] = []
    seen: set[tuple[str, str | None]] = set()

    def add(issue: Issue) -> None:
        key = (issue.code, issue.slot)
        if key in seen:
            return
        seen.add(key)
        merged.append(issue)

    for issue in analysis.issues:
        if issue.code in TEMPLATE_LEVEL_CODES:
            add(issue)
    for issue in assess_request(template, analysis, request, required_keys):
        add(issue)
    return merged


def can_auto_deliver(issues: Iterable[Issue]) -> bool:
    return not any(issue.blocking for issue in issues)


def served_classes(
    template: Template,
    analysis: Analysis,
    requests: Iterable[VisitRequest],
    required_keys: Iterable[str] = (),
) -> tuple[list[str], list[tuple[str, list[Issue]]]]:
    """返回（可自动服务、交编辑并附原因）两组来宾类型。"""

    served: list[str] = []
    editorial: list[tuple[str, list[Issue]]] = []
    for request in sorted(requests, key=lambda item: item.id):
        issues = assess_request(template, analysis, request, required_keys)
        if can_auto_deliver(issues):
            served.append(request.id)
        else:
            editorial.append((request.id, issues))
    return served, editorial
