"""Structured business-card draft: fields, post-processing rules, chat rendering.

Design choices (Korean contacts):
- The phone/messages screen should read "name  title  company", e.g.
  "최민준 차장 (주)다솔이엔지". Google's display name follows the user's
  first/last order setting, so the whole display string goes into givenName and
  familyName stays empty. The full title and company are kept in the
  organization fields.
- display_title is the short title for that string (영업팀장); title keeps the
  card's full wording (영업팀장/컨설턴트 (Sales Executive)).
- No title is invented. A missing title becomes a warning the approver can fix.
- Korean multi-line notation 052-555-0126~7 is expanded to two numbers.
- Fields the approver typed by hand are listed in locked and survive re-OCR,
  back-side merges and AI revisions.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

PHONE_FIELDS = ("mobile", "work", "fax")
LIST_FIELDS = PHONE_FIELDS + ("email", "website", "address")
TEXT_FIELDS = ("name", "title", "display_title", "company", "notes")
EDITABLE_FIELDS = TEXT_FIELDS + LIST_FIELDS
PLACEHOLDER_VALUES = {"-", "없음", "N/A", "n/a", "null", "none", "None"}
PHONE_RANGE = re.compile(r"^(?P<prefix>.*?)(?P<last>\d{2,4})\s*[~∼～]\s*(?P<suffix>\d{1,4})$")
VALUE_SPLIT = re.compile(r"\s*(?:,|;|\n)\s*")
ADDRESS_SPLIT = re.compile(r"\s*(?:;|\n)\s*")
TITLE_SPLIT = re.compile(r"\s*[/,|·(（\[]\s*")
MISSING_TITLE_WARNING = "직함이 명함에 없음: 필요하면 '직함: ...'으로 입력"

# Labels the approver can type ("이름: 홍길동"). The first label of each field is
# what the draft preview shows.
FIELD_LABELS = {
    "name": ("이름", "성명", "name"),
    "display_title": ("직함", "표시직함", "표시 직함", "직급", "직책", "display_title"),
    "title": ("직함원문", "직함 원문", "전체직함", "전체 직함", "title"),
    "company": ("회사", "회사명", "상호", "company"),
    "mobile": ("휴대전화", "휴대폰", "핸드폰", "모바일", "mobile", "hp"),
    "work": ("회사전화", "직장전화", "사무실", "전화", "tel", "work"),
    "fax": ("팩스", "fax"),
    "email": ("이메일", "메일", "email", "e-mail"),
    "website": ("웹사이트", "홈페이지", "website", "url"),
    "address": ("주소", "address"),
    "notes": ("메모", "notes"),
}
LABEL_TO_FIELD = {re.sub(r"\s+", "", label).lower(): key for key, labels in FIELD_LABELS.items() for label in labels}
EDIT_LINE = re.compile(r"^\s*[-•*]?\s*([^:：=]{1,12}?)\s*[:：=]\s*(.*?)\s*$")


@dataclass
class CardDraft:
    name: str = ""
    title: str = ""
    display_title: str = ""
    company: str = ""
    mobile: list[str] = field(default_factory=list)
    work: list[str] = field(default_factory=list)
    fax: list[str] = field(default_factory=list)
    email: list[str] = field(default_factory=list)
    website: list[str] = field(default_factory=list)
    address: list[str] = field(default_factory=list)
    notes: str = ""
    warnings: list[str] = field(default_factory=list)
    locked: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "CardDraft":
        draft = cls()
        for key in TEXT_FIELDS:
            setattr(draft, key, clean_value(_as_text(data.get(key))))
        for key in LIST_FIELDS:
            splitter = ADDRESS_SPLIT if key == "address" else VALUE_SPLIT
            setattr(draft, key, _as_list(data.get(key), splitter))
        draft.warnings = [w for w in (_as_text(w).strip() for w in _iter(data.get("warnings"))) if w]
        draft.locked = [k for k in (_as_text(k).strip() for k in _iter(data.get("locked"))) if k in EDITABLE_FIELDS]
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
        for key in PHONE_FIELDS:
            expanded: list[str] = []
            for value in getattr(self, key):
                expanded.extend(expand_phone_range(value))
            setattr(self, key, _dedupe(expanded))
        for key in ("email", "website", "address"):
            setattr(self, key, _dedupe(getattr(self, key)))
        if self.display_title and not self.title:
            self.title = self.display_title
        has_title = bool(self.title or self.display_title)
        self.warnings = [w for w in _dedupe(self.warnings) if not (has_title and w == MISSING_TITLE_WARNING)]
        if self.name and not has_title and MISSING_TITLE_WARNING not in self.warnings:
            self.warnings.append(MISSING_TITLE_WARNING)
        self.locked = _dedupe([k for k in self.locked if k in EDITABLE_FIELDS])
        return self

    @property
    def short_title(self) -> str:
        if self.display_title:
            return self.display_title
        return short_title_of(self.title)

    @property
    def company_display(self) -> str:
        return re.sub(r"(\(주\)|\(유\)|\(사\)|\(재\))\s+", r"\1", self.company).strip()

    @property
    def display_name(self) -> str:
        """What the phone shows: 최민준 차장 (주)다솔이엔지."""
        return " ".join(part for part in (self.name, self.short_title, self.company_display) if part)

    @property
    def name_field(self) -> str:
        return self.display_name

    @property
    def has_phone(self) -> bool:
        return any(getattr(self, key) for key in PHONE_FIELDS)

    @property
    def is_savable(self) -> bool:
        return bool(self.name) and self.has_phone

    def missing_for_save(self) -> list[str]:
        missing = []
        if not self.name:
            missing.append("이름")
        if not self.has_phone:
            missing.append("전화번호")
        return missing

    def to_text(self, header: str = "명함 입력 초안") -> str:
        lines = [
            header,
            f"표시 이름: {self.display_name or '(이름 없음)'}",
            "",
            f"- 이름: {self.name}",
            f"- 직함: {self.short_title}",
        ]
        if self.title and self.title != self.short_title:
            lines.append(f"- 직함원문: {self.title}")
        lines += [
            f"- 회사: {self.company}",
            f"- 휴대전화: {', '.join(self.mobile)}",
            f"- 회사전화: {', '.join(self.work)}",
            f"- 팩스: {', '.join(self.fax)}",
            f"- 이메일: {', '.join(self.email)}",
            f"- 웹사이트: {', '.join(self.website)}",
            f"- 주소: {'; '.join(self.address)}",
            f"- 메모: {self.notes}",
        ]
        if self.warnings:
            lines.append(f"- 확인 필요: {'; '.join(self.warnings)}")
        if self.locked:
            labels = [FIELD_LABELS[k][0] for k in self.locked if k in FIELD_LABELS]
            lines.append(f"- 직접 수정함: {', '.join(labels)}")
        return "\n".join(lines)


def short_title_of(title: str) -> str:
    """'영업팀장/컨설턴트 (Sales Executive)' -> '영업팀장'."""
    for part in TITLE_SPLIT.split(title or ""):
        part = part.strip(" )）]")
        if part:
            return part
    return ""


def parse_edits(text: str) -> tuple[dict[str, str], list[str]]:
    """'이름: 홍길동' lines -> ({'name': '홍길동'}, unrecognised_lines). No AI involved."""
    edits: dict[str, str] = {}
    unknown: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = EDIT_LINE.match(line)
        key = LABEL_TO_FIELD.get(re.sub(r"\s+", "", match.group(1)).lower()) if match else None
        if key is None:
            unknown.append(line.strip())
            continue
        edits[key] = match.group(2)
    return edits, unknown


def apply_edits(draft: CardDraft, edits: dict[str, str]) -> CardDraft:
    """Return a copy with the typed values; an empty value or '-'/'없음' clears the field."""
    data = draft.to_dict()
    for key, value in edits.items():
        data[key] = value
        if key == "display_title" and value and not edits.get("title") and not data.get("title"):
            data["title"] = value
    data["locked"] = list(draft.locked) + list(edits)
    edited = CardDraft.from_dict(data)
    resolved = {FIELD_LABELS[k][0] for k in edits}
    edited.warnings = [w for w in edited.warnings if not any(label in w for label in resolved)]
    return edited.normalize()


def preserve_locked(new: CardDraft, old: CardDraft) -> CardDraft:
    """Keep the approver's hand-typed fields when a machine result replaces the draft."""
    if not old.locked:
        return new
    data = new.to_dict()
    old_data = old.to_dict()
    for key in old.locked:
        data[key] = old_data[key]
    data["locked"] = list(old.locked) + [k for k in new.locked if k not in old.locked]
    return CardDraft.from_dict(data)


def clean_value(value: str) -> str:
    value = value.strip()
    if not value or value in PLACEHOLDER_VALUES:
        return ""
    if re.fullmatch(r"[\(\[（【].*[\)\]）】]", value):
        return ""  # placeholder only, e.g. (확인 필요), (미기재)
    return re.sub(r"\s*[\(（][^\)）]*확인 필요[^\)）]*[\)）]\s*$", "", value).strip()


def expand_phone_range(value: str) -> list[str]:
    """'052-555-0126~7' -> ['052-555-0126', '052-555-0127']."""
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
