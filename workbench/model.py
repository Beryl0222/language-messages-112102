"""交换资料的只读模型与显示宽度计算。"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import unicodedata
from typing import Any, Iterable, Mapping


DATA_DIR = Path(__file__).resolve().parents[1] / "data"
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# 设备接口里的固定变量含义，来自 screens.json 的 field_rules。
# 这里只把业务侧写明的含义固化成可比较的角色，不改变资料。
CANONICAL_SLOT_ROLES: Mapping[str, str] = {
    "primary": "led_guest",
    "guide": "leader",
    "room": "destination",
    "group_count": "companion_count_excluding_led_guest",
    "honorific": "optional_display_title_for_primary",
}
OPTIONAL_SLOTS = frozenset({"honorific"})
DATA_ONLY_FIELDS = frozenset({"permitted_public_name", "honorific_consent", "from"})


class DataError(ValueError):
    """交换资料不合约定。"""


def display_width(text: str) -> int:
    """按设备约定计算列宽。

    组合附标（Mn/Me）和零宽连接符计零；东亚宽度 W/F 计两列；其余一列。
    """

    width = 0
    for char in text:
        if char == "‍":
            continue
        if unicodedata.category(char) in ("Mn", "Me"):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


@dataclass(frozen=True)
class Placeholder:
    name: str
    start: int
    end: int


def parse_segments(text: str) -> tuple[Any, ...]:
    """把模板拆成字面量和占位符，保留原始顺序。"""

    segments: list[Any] = []
    cursor = 0
    for match in PLACEHOLDER_RE.finditer(text):
        if match.start() > cursor:
            segments.append(text[cursor : match.start()])
        segments.append(Placeholder(match.group(1), match.start(), match.end()))
        cursor = match.end()
    if cursor < len(text):
        segments.append(text[cursor:])
    return tuple(segments)


@dataclass(frozen=True)
class Template:
    id: str
    message_id: str
    locale: str
    revision: str
    text: str
    applies_to: str | None = None
    role_hint: str | None = None
    segments: tuple[Any, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "segments", parse_segments(self.text))

    @property
    def placeholders(self) -> tuple[str, ...]:
        return tuple(
            segment.name for segment in self.segments if isinstance(segment, Placeholder)
        )

    @property
    def revision_number(self) -> int:
        match = re.fullmatch(r"r(\d+)", self.revision)
        return int(match.group(1)) if match else 0

    def is_fallback_copy(self) -> bool:
        """兜底文案只指向通用服务台，不点名、不指具体地点。"""

        return not any(
            name in ("primary", "guide", "room", "group_count")
            for name in self.placeholders
        )


@dataclass(frozen=True)
class Screen:
    id: str
    device: str
    max_columns: Mapping[str, int]
    required_keys: tuple[str, ...]
    waiting_ref: str
    previous_delivery: Mapping[str, Mapping[str, str]]

    def max_width(self, locale: str) -> int:
        try:
            return int(self.max_columns[locale])
        except KeyError as exc:
            raise DataError(f"{self.id} 没有 {locale} 的显示宽度") from exc

    @property
    def supported_locales(self) -> tuple[str, ...]:
        return tuple(sorted(self.max_columns))

    @property
    def guide_message_ids(self) -> tuple[str, ...]:
        """设备正在投放、且不是兜底消息的编号。"""

        return tuple(
            message_id
            for message_id in self.previous_delivery
            if message_id != self.waiting_ref
        )


@dataclass(frozen=True)
class VisitRequest:
    id: str
    visit_class: str
    device_id: str
    data: Mapping[str, Any]
    from_shift: str | None = None

    @property
    def active_guard(self) -> str | None:
        return self.from_shift

    def available(self, active_shift: str | None) -> bool:
        """带 from 的类别只在该班次开始后才允许发送。"""

        if self.from_shift is None:
            return True
        return active_shift == self.from_shift

    @property
    def group_count(self) -> int:
        return int(self.data["group_count"])

    @property
    def display_name(self) -> str:
        """本人只同意公开自选名时，屏幕上不得出现未获同意的全名。"""

        chosen = self.data.get("permitted_public_name")
        if isinstance(chosen, str) and chosen.strip():
            return chosen
        return str(self.data["primary"])

    @property
    def public_name_is_chosen(self) -> bool:
        chosen = self.data.get("permitted_public_name")
        return isinstance(chosen, str) and bool(chosen.strip())

    @property
    def honorific(self) -> str | None:
        """有称呼且未被本人拒绝时才可用；未录称呼就走中性写法。"""

        value = self.data.get("honorific")
        if not isinstance(value, str) or not value.strip():
            return None
        if self.data.get("honorific_consent") is False:
            return None
        return value.strip()

    @property
    def has_unconsented_honorific(self) -> bool:
        """本人明确表示不要身份称呼时，记录以便屏幕改用中性写法。"""

        value = self.data.get("honorific")
        return (
            isinstance(value, str)
            and bool(value.strip())
            and self.data.get("honorific_consent") is False
        )

    @property
    def values(self) -> dict[str, Any]:
        return {
            "primary": self.display_name,
            "guide": self.data["guide"],
            "room": self.data["room"],
            "group_count": self.group_count,
            "honorific": self.honorific,
        }

    def validate(self) -> None:
        missing = [
            key
            for key in ("primary", "guide", "room", "group_count")
            if key not in self.data
        ]
        if missing:
            raise DataError(f"{self.id} 缺少变量 {', '.join(missing)}")
        for key in ("primary", "guide", "room"):
            if not isinstance(self.data[key], str) or not self.data[key].strip():
                raise DataError(f"{self.id} 的 {key} 必须是非空文本")
        group_count = self.data["group_count"]
        if isinstance(group_count, bool) or not isinstance(group_count, int):
            raise DataError(f"{self.id} 的 group_count 必须是整数")
        if group_count < 0:
            raise DataError(f"{self.id} 的 group_count 不能是负数")
        chosen = self.data.get("permitted_public_name")
        if chosen is not None and (
            not isinstance(chosen, str) or not chosen.strip()
        ):
            raise DataError(f"{self.id} 的 permitted_public_name 必须是非空文本")
        consent = self.data.get("honorific_consent")
        if consent is not None and not isinstance(consent, bool):
            raise DataError(f"{self.id} 的 honorific_consent 必须是布尔值")


@dataclass
class Tables:
    screens: dict[str, Screen]
    field_rules: dict[str, str]
    templates: dict[str, Template]
    classes: dict[str, VisitRequest]

    def templates_for(
        self,
        message_id: str,
        locale: str | None = None,
        revision: str | None = None,
    ) -> list[Template]:
        rows = [
            template
            for template in self.templates.values()
            if template.message_id == message_id
            and (locale is None or template.locale == locale)
            and (revision is None or template.revision == revision)
        ]
        return sorted(rows, key=lambda item: (item.locale, item.revision_number, item.id))

    def locales_for(self, message_id: str) -> set[str]:
        return {template.locale for template in self.templates_for(message_id)}

    def message_ids(self) -> set[str]:
        return {template.message_id for template in self.templates.values()}

    def devices_for_message(self, message_id: str) -> list[str]:
        """哪些屏幕正在投放或退回这条消息。"""

        result: list[str] = []
        for screen in self.screens.values():
            if message_id in screen.previous_delivery or message_id == screen.waiting_ref:
                result.append(screen.device)
        return sorted(set(result))

    def screen_for_request(self, request: VisitRequest) -> Screen:
        try:
            return self.screens[request.device_id]
        except KeyError as exc:
            raise DataError(f"{request.id} 指向不存在的屏幕 {request.device_id}") from exc


def _read_rows(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataError(f"无法读取 {path}: {exc}") from exc
    if not isinstance(raw, list):
        raise DataError(f"{path.name} 顶层必须是数组")
    return raw


def _unique_by_id(rows: Iterable[dict[str, Any]], name: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        identifier = row.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise DataError(f"{name} 中有记录缺少字符串 id")
        if identifier in result:
            raise DataError(f"{name} 中 id 重复: {identifier}")
        result[identifier] = row
    return result


def load_tables(data_dir: str | Path | None = None) -> Tables:
    directory = Path(data_dir) if data_dir is not None else DATA_DIR
    screens_rows = _unique_by_id(_read_rows(directory / "screens.json"), "screens")
    copy_rows = _unique_by_id(_read_rows(directory / "copybook.json"), "copybook")
    class_rows = _unique_by_id(_read_rows(directory / "classes.json"), "classes")

    screens: dict[str, Screen] = {}
    field_rules: dict[str, str] = {}
    for identifier, row in screens_rows.items():
        if row.get("kind") == "field_rules":
            field_rules = {
                key: value
                for key, value in row.items()
                if key not in ("id", "kind") and isinstance(value, str)
            }
            continue
        device = row.get("device")
        max_columns = row.get("max_columns")
        required_keys = row.get("required_keys")
        waiting_ref = row.get("waiting_ref")
        previous_delivery = row.get("previous_delivery", {})
        if not isinstance(device, str) or not device:
            raise DataError(f"{identifier} 缺少 device")
        if not isinstance(max_columns, dict) or not all(
            isinstance(key, str) and isinstance(value, int) and value > 0
            for key, value in max_columns.items()
        ):
            raise DataError(f"{identifier} 的 max_columns 不合法")
        if not isinstance(required_keys, list) or not all(
            isinstance(key, str) for key in required_keys
        ):
            raise DataError(f"{identifier} 的 required_keys 不合法")
        if not isinstance(waiting_ref, str) or not waiting_ref:
            raise DataError(f"{identifier} 缺少 waiting_ref")
        if not isinstance(previous_delivery, dict):
            raise DataError(f"{identifier} 的 previous_delivery 不合法")
        screens[identifier] = Screen(
            id=identifier,
            device=device,
            max_columns=dict(max_columns),
            required_keys=tuple(required_keys),
            waiting_ref=waiting_ref,
            previous_delivery={
                str(message_id): {str(locale): str(revision) for locale, revision in revs.items()}
                for message_id, revs in previous_delivery.items()
                if isinstance(revs, dict)
            },
        )

    templates: dict[str, Template] = {}
    seen_keys: set[tuple[str, str, str]] = set()
    for identifier, row in copy_rows.items():
        message_id = row.get("message_id")
        locale = row.get("locale")
        revision = row.get("revision")
        text = row.get("text")
        if not isinstance(message_id, str) or not message_id:
            raise DataError(f"{identifier} 缺少 message_id")
        if not isinstance(locale, str) or not locale:
            raise DataError(f"{identifier} 缺少 locale")
        if not isinstance(revision, str) or not revision:
            raise DataError(f"{identifier} 缺少 revision")
        if not isinstance(text, str):
            raise DataError(f"{identifier} 缺少 text")
        key = (message_id, locale, revision)
        if key in seen_keys:
            raise DataError(f"同一消息、语言和修订重复: {key}")
        seen_keys.add(key)
        applies_to = row.get("applies_to")
        role_hint = row.get("role_hint")
        if applies_to is not None and not isinstance(applies_to, str):
            raise DataError(f"{identifier} 的 applies_to 必须是文本")
        if role_hint is not None and not isinstance(role_hint, str):
            raise DataError(f"{identifier} 的 role_hint 必须是文本")
        templates[identifier] = Template(
            id=identifier,
            message_id=message_id,
            locale=locale,
            revision=revision,
            text=text,
            applies_to=applies_to,
            role_hint=role_hint,
        )

    classes: dict[str, VisitRequest] = {}
    for identifier, row in class_rows.items():
        visit_class = row.get("visit_class")
        device_id = row.get("device")
        data = row.get("data")
        if not isinstance(visit_class, str) or not visit_class:
            raise DataError(f"{identifier} 缺少 visit_class")
        if not isinstance(device_id, str) or not device_id:
            raise DataError(f"{identifier} 缺少 device")
        if not isinstance(data, dict):
            raise DataError(f"{identifier} 的 data 必须是对象")
        from_shift = row.get("from")
        request = VisitRequest(
            id=identifier,
            visit_class=visit_class,
            device_id=device_id,
            data=dict(data),
            from_shift=from_shift if isinstance(from_shift, str) else None,
        )
        request.validate()
        classes[identifier] = request

    tables = Tables(
        screens=screens,
        field_rules=field_rules,
        templates=templates,
        classes=classes,
    )
    _validate_links(tables)
    return tables


def load_requests(path: str | Path) -> list[VisitRequest]:
    """读取编校员准备的合成请求，用于在交付前试预览。

    每行与 classes.json 的字段相同；id 不能与既有类别重复。
    """

    rows = _unique_by_id(_read_rows(Path(path)), "requests")
    requests: list[VisitRequest] = []
    for identifier, row in rows.items():
        visit_class = row.get("visit_class")
        device_id = row.get("device")
        data = row.get("data")
        if not isinstance(visit_class, str) or not visit_class:
            raise DataError(f"{identifier} 缺少 visit_class")
        if not isinstance(device_id, str) or not device_id:
            raise DataError(f"{identifier} 缺少 device")
        if not isinstance(data, dict):
            raise DataError(f"{identifier} 的 data 必须是对象")
        from_shift = row.get("from")
        request = VisitRequest(
            id=identifier,
            visit_class=visit_class,
            device_id=device_id,
            data=dict(data),
            from_shift=from_shift if isinstance(from_shift, str) else None,
        )
        request.validate()
        requests.append(request)
    return requests


def _validate_links(tables: Tables) -> None:
    message_ids = tables.message_ids()
    allowed_slots = set(CANONICAL_SLOT_ROLES) | OPTIONAL_SLOTS
    for screen in tables.screens.values():
        if screen.waiting_ref not in message_ids:
            raise DataError(f"{screen.id} 的 waiting_ref {screen.waiting_ref} 不存在")
        waiting_templates = tables.templates_for(screen.waiting_ref)
        if not waiting_templates:
            raise DataError(f"{screen.id} 的 waiting_ref 没有文案")
        if not all(template.is_fallback_copy() for template in waiting_templates):
            raise DataError(
                f"{screen.id} 的 waiting_ref 含有具名或地点变量，不能当独立退回文案"
            )
        for message_id, revisions in screen.previous_delivery.items():
            if message_id not in message_ids:
                raise DataError(
                    f"{screen.id} 的 previous_delivery 引用未知消息 {message_id}"
                )
            for locale, revision in revisions.items():
                if not tables.templates_for(message_id, locale, revision):
                    raise DataError(
                        f"{screen.id} 的 previous_delivery 引用不存在的修订 "
                        f"{message_id}/{locale}/{revision}"
                    )
    waiting_refs = {screen.waiting_ref for screen in tables.screens.values()}
    for template in tables.templates.values():
        unknown = set(template.placeholders) - allowed_slots
        if unknown:
            raise DataError(
                f"{template.id} 使用未知变量: {', '.join(sorted(unknown))}"
            )
        if not template.placeholders and template.message_id not in waiting_refs:
            raise DataError(
                f"{template.id} 没有变量，也不是屏幕批准的独立退回文案"
            )
    for request in tables.classes.values():
        screen = tables.screen_for_request(request)
        missing = set(screen.required_keys) - set(request.data)
        if missing:
            raise DataError(
                f"{request.id} 缺少屏幕 {screen.id} 要求的变量: "
                f"{', '.join(sorted(missing))}"
            )
