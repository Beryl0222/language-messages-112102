"""读取数据源，并把设备约束、业务值域与来宾请求整理成可检查的对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping
import json
import unicodedata


DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
VALID_LOCALES = ("zh", "en")


class DataError(ValueError):
    """交换资料不满足工作台可以安全解释的最小条件。"""


def load_table(path: Path) -> list[dict[str, Any]]:
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataError(f"{path}: 无法读取 UTF-8 JSON 数组: {exc}") from exc
    if not isinstance(rows, list):
        raise DataError(f"{path}: 顶层应为数组")
    return rows


def _text_width(text: str) -> int:
    """按设备约定计数显示列宽。

    汉字与东亚宽度 W/F 计两列；组合附标与零宽连接符计零列。
    """
    columns = 0
    for char in text:
        if unicodedata.category(char).startswith("M") or char in "‌‍﻿":
            continue
        if unicodedata.east_asian_width(char) in ("W", "F"):
            columns += 2
        else:
            columns += 1
    return columns


@dataclass(frozen=True)
class CopyVariant:
    """一条编校交换记录。原始文字保持收到时的样子。"""

    id: str
    message_id: str
    locale: str
    revision: str
    text: str
    role_hint: str | None = None
    applies_to: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "CopyVariant":
        variant = cls(
            id=row["id"],
            message_id=row["message_id"],
            locale=row["locale"],
            revision=row["revision"],
            text=row["text"],
            role_hint=row.get("role_hint"),
            applies_to=row.get("applies_to"),
        )
        if variant.locale not in VALID_LOCALES:
            raise DataError(f"{variant.id}: 未知语言 {variant.locale!r}")
        return variant

    @property
    def label(self) -> str:
        return f"{self.message_id}/{self.locale}/{self.revision} ({self.id})"

    def width(self) -> int:
        return _text_width(self.text)


@dataclass(frozen=True)
class PreviousDelivery:
    message_id: str
    locales: Mapping[str, str]

    def revision(self, locale: str) -> str | None:
        return self.locales.get(locale)


@dataclass(frozen=True)
class Screen:
    id: str
    device: str
    max_columns: Mapping[str, int]
    required_keys: tuple[str, ...]
    waiting_ref: str
    previous_delivery: Mapping[str, PreviousDelivery] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Screen":
        if row.get("kind") == "field_rules":
            raise DataError("场地字段规则不是屏幕，请用 FieldRules 读取")
        previous: dict[str, PreviousDelivery] = {}
        for message_id, locales in row.get("previous_delivery", {}).items():
            previous[message_id] = PreviousDelivery(message_id, dict(locales))
        return cls(
            id=row["id"],
            device=row["device"],
            max_columns={locale: int(value) for locale, value in row["max_columns"].items()},
            required_keys=tuple(row["required_keys"]),
            waiting_ref=row["waiting_ref"],
            previous_delivery=previous,
        )


@dataclass(frozen=True)
class FieldRules:
    """设备字段约定，描述值域而不是某一条短句的读法。"""

    group_count: str
    primary: str
    guide: str
    room: str

    def note_for(self, key: str) -> str | None:
        return getattr(self, key, None)


@dataclass(frozen=True)
class VisitorRequest:
    """活动方登记的来访请求。data 是设备实际会传入的值。"""

    id: str
    visit_class: str
    device: str
    data: Mapping[str, Any]
    available_from: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "VisitorRequest":
        data = row["data"]
        if not isinstance(data, Mapping):
            raise DataError(f"{row['id']}: data 应为对象")
        return cls(
            id=row["id"],
            visit_class=row["visit_class"],
            device=row["device"],
            data=dict(data),
            available_from=row.get("from"),
        )

    def identity_is_settled(self, shift: str | None = None) -> bool:
        """按登记班次判断这条请求现在是否可以使用。

        只记录“晚班”的请求，在未提供班次时按仍待处理处理，不能悄悄当成已生效。
        """
        if self.available_from is None:
            return True
        if shift is None:
            return False
        return shift.casefold() == self.available_from.casefold()

    def resolve_primary(self, allowed_name: str | None = None) -> str | None:
        """返回可以向屏幕写入的姓名；不获准时返回 None。

        只显示自选名时，设备实际传入的 primary 不是可公开值。
        """
        public_name = self.data.get("permitted_public_name")
        if public_name:
            return str(public_name)
        if allowed_name is not None:
            if self.data.get("primary") == allowed_name:
                return allowed_name
            return None
        return str(self.data.get("primary")) if self.data.get("primary") is not None else None

    def values(self, allowed_name: str | None = None) -> dict[str, Any]:
        values = dict(self.data)
        if "permitted_public_name" in values:
            values.setdefault("name", values["permitted_public_name"])
        resolved = self.resolve_primary(allowed_name)
        if resolved is not None:
            values["primary"] = resolved
        values.setdefault("honorific", None)
        return values

    def shown_identity(self, allowed_name: str | None = None) -> str:
        """组合读者最终看到的姓名与可选称呼，仅用于检查。"""
        values = self.values(allowed_name)
        parts = [values.get("honorific"), values.get("primary")]
        return " ".join(str(part) for part in parts if part)


@dataclass(frozen=True)
class Workbench:
    copybook: tuple[CopyVariant, ...]
    screens: Mapping[str, Screen]
    requests: tuple[VisitorRequest, ...]
    fields: FieldRules | None

    def screen(self, screen_id: str) -> Screen:
        try:
            return self.screens[screen_id]
        except KeyError as exc:
            raise DataError(f"没有编号为 {screen_id!r} 的屏幕") from exc

    def copy_by_id(self) -> dict[str, CopyVariant]:
        return {variant.id: variant for variant in self.copybook}

    def variants_for(self, message_id: str, locale: str | None = None) -> list[CopyVariant]:
        return [
            variant
            for variant in self.copybook
            if variant.message_id == message_id and (locale is None or variant.locale == locale)
        ]

    def waiting_variants(self, screen: Screen, locale: str) -> list[CopyVariant]:
        return self.variants_for(screen.waiting_ref, locale)

    def request(self, request_id: str) -> VisitorRequest:
        for request in self.requests:
            if request.id == request_id:
                return request
        raise DataError(f"没有编号为 {request_id!r} 的来宾请求")


def load_workbench(data_dir: str | Path | None = None) -> Workbench:
    directory = Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR
    copy_rows = load_table(directory / "copybook.json")
    screen_rows = load_table(directory / "screens.json")
    class_rows = load_table(directory / "classes.json")

    copybook = tuple(CopyVariant.from_row(row) for row in copy_rows)
    screens: dict[str, Screen] = {}
    fields: FieldRules | None = None
    for row in screen_rows:
        if row.get("kind") == "field_rules":
            fields = FieldRules(
                group_count=row["group_count"],
                primary=row["primary"],
                guide=row["guide"],
                room=row["room"],
            )
            continue
        screen = Screen.from_row(row)
        if screen.id in screens:
            raise DataError(f"屏幕编号重复: {screen.id}")
        screens[screen.id] = screen

    requests = tuple(VisitorRequest.from_row(row) for row in class_rows)

    validate_copybook(copybook)

    message_ids = {variant.message_id for variant in copybook}
    for screen in screens.values():
        if screen.waiting_ref not in message_ids:
            raise DataError(f"{screen.id}: waiting_ref {screen.waiting_ref!r} 不在文案表中")
    request_ids: set[str] = set()
    for request in requests:
        if request.id in request_ids:
            raise DataError(f"请求 id 重复: {request.id}")
        request_ids.add(request.id)
        if request.device not in screens:
            raise DataError(f"{request.id}: 设备 {request.device!r} 不在屏幕表中")

    return Workbench(copybook=copybook, screens=screens, requests=requests, fields=fields)


def validate_copybook(copybook: Iterable[CopyVariant]) -> None:
    """检查文案表的 id 与修订号唯一性。"""
    ids: set[str] = set()
    message_locale_revision: set[tuple[str, str, str]] = set()
    for variant in copybook:
        if variant.id in ids:
            raise DataError(f"文案 id 重复: {variant.id}")
        ids.add(variant.id)
        key = (variant.message_id, variant.locale, variant.revision)
        if key in message_locale_revision:
            raise DataError(f"同一消息出现重复修订号: {key}")
        message_locale_revision.add(key)


def iter_placeholders(text: str) -> Iterable[str]:
    """按出现顺序产生 {name} 形式的变量名。"""
    from .render import parse_template

    for part in parse_template(text):
        if isinstance(part, tuple) and part[0] == "field":
            yield part[1]
