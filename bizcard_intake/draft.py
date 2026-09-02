"""Structured business-card draft: fields, post-processing rules, chat rendering.

Design choices (intentional, Korean contacts):
- Google "given name" = full name + title (홍길동 이사); "family name" = company.
  With thousands of contacts, name + company on the caller screen is what makes
  a person recognisable.
- A missing title defaults to 대표.
- Korean multi-line notation 052-297-8976~7 is expanded to two numbers.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

DEFAULT_TITLE = "대표"
PHONE_FIELDS = ("mobile", "work", "fax")
LIST_FIELDS = PHONE_FIELDS + ("email", "website", "address")
PLACEHOLDER_VALUES = {"-", "없음", "N/A", "n/a", "null", "none", "None"}
PHONE_RANGE = re.compile(r"^(?P<prefix>.*?)(?P<last>\d{2,4})\s*[~∼～]\s*(?P<suffix>\d{1,4})$")
VALUE_SPLIT = re.compile(r"\s*(?:,|;|\n)\s*")
ADDRESS_SPLIT = re.compile(r"\s*(?:;|\n)\s*")


@dataclass
class CardDraft:
    name: str = ""
    title: str = ""
    company: str = ""
    mobile: list[str] = field(default_factory=list)
    work: list[str] = field(default_factory=list)
    fax: list[str] = field(default_factory=list)
    email: list[str] = field(default_factory=list)
    website: list[str] = field(default_factory=list)
    address: list[str] = field(default_factory=list)
    notes: str = ""
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "CardDraft":
        draft = cls()
        for key in ("name", "title", "company", "notes"):
            setattr(draft, key, clean_value(_as_text(data.get(key))))
        for key in LIST_FIELDS:
            splitter = ADDRESS_SPLIT if key == "address" else VALUE_SPLIT
            setattr(draft, key, _as_list(data.get(key), splitter))
        draft.warnings = [w for w in (_as_text(w).strip() for w in _iter(data.get("warnings"))) if w]
        return draft.normalize()

    @classmethod
    def from_json(cls, text: str) -> "CardDraft":
        return cls.from_dict(json.loads(text))

    @classmethod
    def from_text(cls, text: str) -> "CardDraft":
        """Parse the legacy '명함 입력 초안' text format (pre-JSON sessions)."""
        fields: dict[str, str] = {}
        for line in text.splitlines():
            match = re.match(r"^\s*-\s*([^:：]+)[:：]\s*(.*)$", line)
            if match:
                fields[match.group(1).strip()] = match.group(2).strip()
        name_parts = clean_value(fields.get("이름 필드", "")).split(maxsplit=1)
        return cls.from_dict(
            {
                "name": name_parts[0] if name_parts else "",
                "title": name_parts[1] if len(name_parts) > 1 else "",
                "company": fields.get("성씨 필드", ""),
                "mobile": fields.get("휴대전화", ""),
                "work": fields.get("회사전화", ""),
                "fax": fields.get("팩스", ""),
                "email": fields.get("이메일", ""),
                "website": fields.get("웹사이트", ""),
                "address": fields.get("주소", ""),
                "notes": fields.get("메모", ""),
                "warnings": [w for w in re.split(r"\s*;\s*", clean_value(fields.get("검증 경고", ""))) if w],
            }
        )

    @classmethod
    def from_session_value(cls, value) -> "CardDraft":
        if isinstance(value, str):
            return cls.from_text(value)
        return cls.from_dict(value or {})

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def normalize(self) -> "CardDraft":
        if self.name and not self.title:
            self.title = DEFAULT_TITLE
        for key in PHONE_FIELDS:
            expanded: list[str] = []
            for value in getattr(self, key):
                expanded.extend(expand_phone_range(value))
            setattr(self, key, _dedupe(expanded))
        for key in ("email", "website", "address"):
            setattr(self, key, _dedupe(getattr(self, key)))
        return self

    @property
    def name_field(self) -> str:
        return f"{self.name} {self.title}".strip()

    @property
    def has_phone(self) -> bool:
        return any(getattr(self, key) for key in PHONE_FIELDS)

    @property
    def is_savable(self) -> bool:
        return bool(self.name) and self.has_phone

    def to_text(self) -> str:
        return "\n".join(
            [
                "명함 입력 초안",
                f"- 이름 필드: {self.name_field}",
                f"- 성씨 필드: {self.company}",
                f"- 휴대전화: {', '.join(self.mobile)}",
                f"- 회사전화: {', '.join(self.work)}",
                f"- 팩스: {', '.join(self.fax)}",
                f"- 이메일: {', '.join(self.email)}",
                f"- 웹사이트: {', '.join(self.website)}",
                f"- 주소: {'; '.join(self.address)}",
                f"- 메모: {self.notes}",
                f"- 검증 경고: {'; '.join(self.warnings)}",
            ]
        )


def clean_value(value: str) -> str:
    value = value.strip()
    if not value or value in PLACEHOLDER_VALUES:
        return ""
    if re.fullmatch(r"[\(\[（【].*[\)\]）】]", value):
        return ""  # placeholder only, e.g. (확인 필요), (미기재)
    return re.sub(r"\s*[\(（][^\)）]*확인 필요[^\)）]*[\)）]\s*$", "", value).strip()


def expand_phone_range(value: str) -> list[str]:
    """'052-297-8976~7' -> ['052-297-8976', '052-297-8977']."""
    match = PHONE_RANGE.match(value)
    if not match:
        return [value]
    prefix, last, suffix = match.group("prefix"), match.group("last"), match.group("suffix")
    if len(suffix) > len(last):
        return [value]
    second = last[: len(last) - len(suffix)] + suffix
    return [f"{prefix}{last}", f"{prefix}{second}"]


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(_as_text(item) for item in value)
    return str(value)


def _iter(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _as_list(value, splitter: re.Pattern) -> list[str]:
    items: list[str] = []
    for raw in _iter(value):
        for part in splitter.split(_as_text(raw)):
            cleaned = clean_value(part)
            if cleaned:
                items.append(cleaned)
    return items


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        key = re.sub(r"\s+", "", item).lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique
