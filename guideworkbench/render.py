"""短句模板的替换、宽度检查与退回消息选择。

这里只处理“把已知值放进已批准的短句”这一层，不判断短句语义是否正确；
语义核对在 review.py，成组交付在 bundle.py。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
import re
import unicodedata


TOKEN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
SPACE_RUN = re.compile(r"[ \t]{2,}")
SPACE_BEFORE_PUNCT = re.compile(r" +([,.;:!?，。；：！？])")

# 安全退回消息不应依赖任何来宾值：名字或地点一旦缺失，不能把空位当成
# 已经批准的目的地，也不能用截短的名字顶替。
FALLBACK_FORBIDDEN_FIELDS = ("primary", "room", "guide", "group_count", "honorific")


class RenderError(ValueError):
    """模板无法安全替换。"""


@dataclass(frozen=True)
class RenderPolicy:
    """替换时允许的写法。

    neutral_honorific 是经批准的中性称呼；空字符串表示只写姓名。
    没有明确批准时，不允许用空称呼或仅姓名冒充获准的写法。
    """

    neutral_honorific: str = ""
    neutral_honorific_approved: bool = False
    require_honorific_consent: bool = True

    def honorific_allowed(self, values: Mapping[str, Any]) -> bool:
        if not values.get("honorific"):
            return False
        if not self.require_honorific_consent:
            return True
        return bool(values.get("honorific_consent") or values.get("honorific_approved"))


DEFAULT_POLICY = RenderPolicy()


@dataclass(frozen=True)
class TemplateParts:
    parts: tuple[tuple[str, str], ...]

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(value for kind, value in self.parts if kind == "field")

    @property
    def unique_fields(self) -> tuple[str, ...]:
        seen: list[str] = []
        for name in self.fields:
            if name not in seen:
                seen.append(name)
        return tuple(seen)


def parse_template(text: str) -> TemplateParts:
    if not isinstance(text, str):
        raise RenderError("短句必须是字符串")
    parts: list[tuple[str, str]] = []
    position = 0
    for match in TOKEN.finditer(text):
        if match.start() > position:
            parts.append(("text", text[position:match.start()]))
        parts.append(("field", match.group(1)))
        position = match.end()
    if position < len(text):
        parts.append(("text", text[position:]))
    residue = TOKEN.sub("", text)
    if "{" in residue or "}" in residue:
        raise RenderError(f"模板中有未闭合或多余的花括号: {text!r}")
    return TemplateParts(tuple(parts))


def template_fields(text: str) -> tuple[str, ...]:
    return parse_template(text).unique_fields


def normalise_space(text: str) -> str:
    text = SPACE_RUN.sub(" ", text)
    text = SPACE_BEFORE_PUNCT.sub(r"\1", text)
    return text.strip()


def display_width(text: str) -> int:
    """按设备约定计数显示列宽。

    组合附标与零宽连接符计零列；东亚宽度 W/F 计两列，其余一列。
    """
    columns = 0
    for char in text:
        if unicodedata.category(char).startswith("M") or char in "‌‍﻿":
            continue
        columns += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return columns


@dataclass(frozen=True)
class Rendered:
    """一次替换的结果。即使被拦下，也保留供编辑检查的草稿文本。"""

    text: str
    blocked: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    used_neutral_honorific: bool = False
    used_honorific: str | None = None

    @property
    def deliverable(self) -> bool:
        return not self.blocked

    @property
    def columns(self) -> int:
        return display_width(self.text)


def _string_value(name: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    return str(value)


def render_text(
    text: str,
    values: Mapping[str, Any],
    policy: RenderPolicy = DEFAULT_POLICY,
) -> Rendered:
    """把值放进模板。

    - 缺少必需的值：拦下，text 为空。
    - 有称呼词位但值为空：只有获准的中性写法才可通过。
    - 有称呼词位且有人称：没有同意标记时拦下。
    - 不做任何截断。
    """
    parts = parse_template(text)
    notes: list[str] = []
    blocked: list[str] = []
    missing: list[str] = []

    for name in parts.unique_fields:
        if name == "honorific":
            continue
        if name not in values or values[name] is None or values[name] == "":
            missing.append(name)
    if missing:
        blocked.append("缺少必需变量: " + ", ".join(missing))
        return Rendered(
            text="",
            blocked=tuple(blocked),
            notes=tuple(notes),
            missing=tuple(missing),
        )

    honorific_text = ""
    used_neutral = False
    used_honorific: str | None = None
    if "honorific" in parts.unique_fields:
        raw_honorific = values.get("honorific")
        if raw_honorific not in (None, ""):
            if policy.honorific_allowed(values):
                honorific_text = _string_value("honorific", raw_honorific)
                used_honorific = honorific_text
            else:
                blocked.append(
                    "称呼词位有值，但没有记录本人同意；不能仅凭值域就称呼先生或女士"
                )
                # 仍生成仅姓名草稿供编辑查看。
                notes.append("草稿省略了未获同意的称呼")
        else:
            if policy.neutral_honorific_approved:
                honorific_text = policy.neutral_honorific
                used_neutral = True
                notes.append("使用了获准的中性称呼写法")
            else:
                blocked.append("没有获准的中性称呼写法，不能用空称呼顶替")
                notes.append("草稿把称呼词位留空")

    pieces: list[str] = []
    for kind, value in parts.parts:
        if kind == "text":
            pieces.append(value)
            continue
        if value == "honorific":
            pieces.append(honorific_text)
        else:
            pieces.append(_string_value(value, values[value]))
    rendered = normalise_space("".join(pieces))
    return Rendered(
        text=rendered,
        blocked=tuple(blocked),
        notes=tuple(notes),
        missing=(),
        used_neutral_honorific=used_neutral,
        used_honorific=used_honorific,
    )


def fill_template(
    text: str,
    values: Mapping[str, Any],
    policy: RenderPolicy = DEFAULT_POLICY,
) -> str:
    """严格替换；任何拦住的情形都抛出异常。"""
    rendered = render_text(text, values, policy)
    if rendered.blocked:
        raise RenderError("; ".join(rendered.blocked))
    return rendered.text


def is_static_fallback(text: str) -> bool:
    """退回消息必须是独立的静态短句。"""
    return not parse_template(text).fields


def check_fallback_text(
    text: str,
    limit: int,
    forbidden_values: Sequence[str] = (),
) -> tuple[int, tuple[str, ...]]:
    """检查退回短句是否可以安全显示。

    返回 (列数, 问题列表)。不通过时不能把短句当作通用指引交付。
    """
    problems: list[str] = []
    if not is_static_fallback(text):
        problems.append("退回消息仍依赖变量，不能作为独立文案")
    columns = display_width(text)
    if columns > limit:
        problems.append(f"退回消息宽 {columns} 列，超过 {limit} 列")
    for value in forbidden_values:
        if value and value in text:
            problems.append(f"退回消息含有具体来宾值 {value!r}，可能把缺失值当成已授权内容")
    return columns, tuple(problems)


@dataclass(frozen=True)
class DeliveredMessage:
    """一条本地预览结果。status 为 ready / fallback / blocked。"""

    request_id: str
    screen_id: str
    message_id: str
    locale: str
    copy_id: str | None
    revision: str | None
    status: str
    text: str
    columns: int | None
    limit: int | None
    conditions: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    fallback_id: str | None = None
    fallback_text: str | None = None

    @property
    def uses_fallback(self) -> bool:
        return self.status == "fallback"

    @property
    def ok(self) -> bool:
        return self.status in ("ready", "fallback")


@dataclass(frozen=True)
class BranchSelection:
    """对某个 group_count 值，某语言的短句选择结果。"""

    locale: str
    group_count: int
    chosen: Any | None
    candidates: tuple[Any, ...] = ()
    status: str = "none"  # unique / ambiguous / none
    reason: str = ""
