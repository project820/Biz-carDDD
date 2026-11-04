"""Google Contacts adapter through the `gws` CLI (People API).

Saving is split into steps so a late failure never hides an earlier success:

1. find or GET the target contact, then create/update it (the only step that can
   fail the save);
2. report the resourceName through `checkpoint` so the caller can persist it
   before anything else happens;
3. upload the photo and read the contact back; problems there become warnings
   on the returned SaveResult.

Every error is a ContactSaveError with a short Korean message, a `kind` and a
`retryable` flag. Raw CLI output (which can be an HTML error page) is kept in
`detail` for logs and never shown as the message.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .draft import CardDraft

PERSON_FIELDS = "names,phoneNumbers,emailAddresses,addresses,organizations,urls,biographies,photos,metadata"
SCAN_FIELDS = "names,phoneNumbers,emailAddresses,addresses,organizations,urls,biographies,metadata"
VERIFY_FIELDS = "names,phoneNumbers"
UPDATE_FIELDS = "names,phoneNumbers,emailAddresses,addresses,organizations,urls,biographies"
CONTACTS_SCOPE = "https://www.googleapis.com/auth/contacts"
LOGIN_COMMAND = f"gws auth login --scopes {CONTACTS_SCOPE}"
GWS_TIMEOUT = 60

ERROR_KINDS = ("auth", "transient", "invalid", "not_found", "unknown_outcome", "other")
AUTH_MESSAGE = f"Google 로그인이 만료되었습니다. '{LOGIN_COMMAND}' 실행이 필요합니다."
UNKNOWN_OUTCOME_MESSAGE = "Google 응답을 확인하지 못했습니다. 저장 여부 확인 필요 (Google 주소록에서 확인하세요)."
AUTH_HINTS = ("no credentials", "invalid_grant", "invalid_rapt", "unauthenticated", "autherror", "authentication", "token has been expired", "token expired", "login required")
TRANSIENT_HINTS = ("timed out", "timeout", "connection reset", "connection refused", "temporarily unavailable", "backenderror", "service unavailable", "bad gateway", "dns", "network")

# Writable sub-fields copied from an existing contact when an update keeps its values.
KEEP_KEYS = {
    "phoneNumbers": ("value", "type"),
    "emailAddresses": ("value", "type", "displayName"),
    "urls": ("value", "type"),
    "addresses": ("formattedValue", "type", "poBox", "streetAddress", "extendedAddress", "city", "region", "postalCode", "country", "countryCode"),
}


class ContactSaveError(RuntimeError):
    """Short Korean, user-facing message; kind in ERROR_KINDS; detail = raw text for logs."""

    def __init__(self, message: str, kind: str = "other", retryable: bool = False, detail: str = ""):
        super().__init__(message)
        self.kind = kind if kind in ERROR_KINDS else "other"
        self.retryable = retryable
        self.detail = detail


@dataclass
class SaveResult:
    action: str  # "created" | "updated"
    resource_name: str
    photo: str = "none"  # "uploaded" | "missing" | "failed" | "none"
    warnings: list[str] = field(default_factory=list)
    display_name: str = ""

    @property
    def summary(self) -> str:
        label = "새로 저장" if self.action == "created" else "기존 연락처 갱신"
        return f"{label}: {self.display_name or self.resource_name}"


# --------------------------------------------------------------------------- save


def save_contact(
    draft: CardDraft,
    profile_image_path: Optional[str],
    resource_name: Optional[str] = None,
    checkpoint: Optional[Callable[[str, str], None]] = None,
) -> SaveResult:
    """Create or update the contact. Raises ContactSaveError only when the
    contact itself was not (or may not have been) written."""
    if not draft.is_savable:
        raise ContactSaveError("이름 또는 전화번호가 없어 저장하지 않았습니다.", kind="invalid")

    warnings: list[str] = []
    existing: Optional[dict[str, Any]] = None
    if resource_name:
        try:
            existing = get_contact(resource_name)
        except ContactSaveError as exc:
            if exc.kind != "not_found":
                raise
            existing = None
    if existing is None:
        existing, candidates = find_existing_candidates(draft)
        if candidates > 1:
            warnings.append(f"중복 후보 {candidates}건: 새 연락처로 저장함")

    if existing:
        update_contact(existing, draft)
        action, saved_name = "updated", existing["resourceName"]
    else:
        saved = create_contact(build_person(draft))
        saved_name = saved.get("resourceName") if isinstance(saved, dict) else None
        if not saved_name:
            raise ContactSaveError(UNKNOWN_OUTCOME_MESSAGE, kind="unknown_outcome", detail=json.dumps(saved, ensure_ascii=False)[:500])
        action = "created"

    if checkpoint is not None:
        try:
            checkpoint(saved_name, action)
        except Exception as exc:  # the contact exists; do not report the save as failed
            warnings.append(f"저장 기록 실패: {exc}")

    result = SaveResult(action=action, resource_name=saved_name, warnings=warnings, display_name=draft.display_name)
    _attach_photo(result, profile_image_path)
    result.warnings.extend(verify_contact(saved_name, draft))
    return result


def _attach_photo(result: SaveResult, image_path: Optional[str]) -> None:
    if not image_path:
        result.photo = "none"
        return
    if not Path(image_path).is_file():
        result.photo = "missing"
        result.warnings.append("명함 사진 파일이 없어 사진 없이 저장함")
        return
    try:
        upload_photo(result.resource_name, image_path)
    except ContactSaveError as exc:
        result.photo = "failed"
        result.warnings.append(f"사진 업로드 실패: {exc}")
        return
    result.photo = "uploaded"


def upload_photo(resource_name: str, image_path: str) -> None:
    """Upload (or replace) the contact photo. Safe to repeat."""
    path = Path(image_path)
    if not path.is_file():
        raise ContactSaveError("명함 사진 파일이 없습니다.", kind="invalid", detail=str(image_path))
    body = {"photoBytes": base64.b64encode(path.read_bytes()).decode("ascii"), "personFields": "photos"}
    run_gws(
        ["people", "people", "updateContactPhoto", "--params", json.dumps({"resourceName": resource_name}), "--json", json.dumps(body)],
        expect_json=False,
    )


def verify_contact(resource_name: str, draft: CardDraft) -> list[str]:
    """Read the contact back; return warnings instead of raising."""
    try:
        person = get_contact(resource_name, VERIFY_FIELDS)
    except ContactSaveError as exc:
        return [f"저장 확인 실패: {exc}"]
    warnings = []
    names = person.get("names") or [{}]
    given = (names[0] or {}).get("givenName", "")
    if given != draft.display_name:
        warnings.append(f"저장 확인: 이름이 '{given}'(으)로 저장됨")
    stored = {normalize_phone(p.get("value", "")) for p in person.get("phoneNumbers", [])}
    missing = [v for v in draft_phones(draft) if normalize_phone(v) not in stored]
    if missing:
        warnings.append(f"저장 확인: 전화번호 누락 {', '.join(missing)}")
    return warnings


# ---------------------------------------------------------------------- mapping


def build_person(draft: CardDraft) -> dict[str, Any]:
    person: dict[str, Any] = {"names": [{"givenName": draft.display_name}]}
    organization = _organization(draft)
    if organization:
        person["organizations"] = [organization]
    phone_numbers = (
        [{"value": v, "type": "mobile"} for v in draft.mobile]
        + [{"value": v, "type": "work"} for v in draft.work]
        + [{"value": v, "type": "workFax"} for v in draft.fax]
    )
    if phone_numbers:
        person["phoneNumbers"] = phone_numbers
    if draft.email:
        person["emailAddresses"] = [{"value": v, "type": "work"} for v in draft.email]
    if draft.website:
        person["urls"] = [{"value": v, "type": "work"} for v in draft.website]
    if draft.address:
        person["addresses"] = [{"formattedValue": v, "type": "work"} for v in draft.address]
    biography = _biography(draft)
    if biography:
        person["biographies"] = [{"value": biography, "contentType": "TEXT_PLAIN"}]
    return person


def _organization(draft: CardDraft) -> dict[str, str]:
    organization: dict[str, str] = {}
    if draft.company:
        organization["name"] = draft.company
    title = draft.title or draft.short_title
    if title:
        organization["title"] = title
    return organization


def _biography(draft: CardDraft) -> str:
    parts = [draft.notes, f"확인 필요: {'; '.join(draft.warnings)}" if draft.warnings else ""]
    return "\n".join(part for part in parts if part)


def build_update(existing: dict[str, Any], draft: CardDraft) -> dict[str, Any]:
    """Person body for updating `existing`: new name/organization, list fields as
    the union of existing and new values so nothing already stored is lost."""
    new = build_person(draft)
    person: dict[str, Any] = {"resourceName": existing["resourceName"], "names": new["names"]}
    if existing.get("etag"):
        person["etag"] = existing["etag"]
    if "organizations" in new:
        person["organizations"] = new["organizations"]
    for key in ("phoneNumbers", "emailAddresses", "urls", "addresses"):
        merged = _union(key, existing.get(key, []), new.get(key, []))
        if merged:
            person[key] = merged
    if "biographies" in new:
        person["biographies"] = new["biographies"]
    return person


def _union(key: str, old_items: list[dict], new_items: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[str] = set()
    for item in old_items:
        kept = {k: item[k] for k in KEEP_KEYS[key] if item.get(k)}
        dedupe = _dedupe_key(key, kept)
        if kept and dedupe not in seen:
            seen.add(dedupe)
            merged.append(kept)
    for item in new_items:
        dedupe = _dedupe_key(key, item)
        if dedupe not in seen:
            seen.add(dedupe)
            merged.append(item)
    return merged


def _dedupe_key(key: str, item: dict) -> str:
    if key == "phoneNumbers":
        return normalize_phone(item.get("value", ""))
    if key == "addresses":
        text = item.get("formattedValue") or " ".join(str(item.get(k, "")) for k in KEEP_KEYS["addresses"][2:])
        return re.sub(r"\s+", "", text).lower()
    if key == "urls":
        return re.sub(r"^https?://(www\.)?", "", item.get("value", "").strip().lower()).rstrip("/")
    return item.get("value", "").strip().lower()


# ------------------------------------------------------------------- matching


def find_existing_contact(draft: CardDraft) -> Optional[dict[str, Any]]:
    """The single existing contact for this card (person dict), or None when
    there is no match or the match is ambiguous."""
    return find_existing_candidates(draft)[0]


def find_existing_candidates(draft: CardDraft) -> tuple[Optional[dict[str, Any]], int]:
    """Scan every contact (the search index lags behind createContact) and
    return (match, candidate_count). match is set only when exactly one
    contact shares a phone number and the person name."""
    if draft.mobile:
        targets = {normalize_phone(v) for v in draft.mobile}
    else:
        targets = {normalize_phone(v) for v in draft.work}
    targets.discard("")
    if not targets or not draft.name.strip():
        return None, 0

    matches: dict[str, dict[str, Any]] = {}
    for person in iter_connections(SCAN_FIELDS):
        phones = {normalize_phone(p.get("value", "")) for p in person.get("phoneNumbers", [])}
        if targets & phones and has_matching_name(person, draft) and person.get("resourceName"):
            matches.setdefault(person["resourceName"], person)
    if len(matches) == 1:
        return next(iter(matches.values())), 1
    return None, len(matches)


def iter_connections(person_fields: str):
    page_token = None
    while True:
        params: dict[str, Any] = {"resourceName": "people/me", "personFields": person_fields, "pageSize": 1000}
        if page_token:
            params["pageToken"] = page_token
        data = run_gws(["people", "people", "connections", "list", "--params", json.dumps(params, ensure_ascii=False)])
        yield from data.get("connections", [])
        page_token = data.get("nextPageToken")
        if not page_token:
            return


def has_matching_name(person: dict[str, Any], draft: CardDraft) -> bool:
    """Exact person name: '최민준' matches '최민준 차장 (주)다솔이엔지' and the legacy
    layout givenName '최민준 차장' + familyName '(주) 다솔이엔지', never '최민준수'."""
    target = " ".join(draft.name.split())
    if not target:
        return False
    company = _squash(draft.company)
    for name in person.get("names", []):
        for key in ("givenName", "displayName", "unstructuredName"):
            if _starts_with_name(name.get(key, ""), target):
                return True
        family = _squash(name.get("familyName", ""))
        if family and company and family == company and _starts_with_name(name.get("givenName", ""), target):
            return True
    return False


def _starts_with_name(value: str, target: str) -> bool:
    value = " ".join((value or "").split())
    return value == target or value.startswith(target + " ")


def _squash(value: str) -> str:
    return re.sub(r"\s+", "", value or "").lower()


def draft_phones(draft: CardDraft) -> list[str]:
    return [*draft.mobile, *draft.work, *draft.fax]


def normalize_phone(value: str) -> str:
    """'+82 (0)10-1234-5678', '0082 10 1234 5678', '82-10-...' -> '01012345678'."""
    value = (value or "").strip()
    value = re.sub(r"\(\s*0\s*\)", "", value)
    international = value.startswith("+")
    digits = re.sub(r"\D+", "", value)
    if digits.startswith("00"):
        digits, international = digits[2:], True
    if digits.startswith("82") and (international or len(digits) >= 11):
        digits = digits[2:]
        if not digits.startswith("0"):
            digits = "0" + digits
    return digits


# ------------------------------------------------------------------ API calls


def get_contact(resource_name: str, person_fields: str = PERSON_FIELDS) -> dict[str, Any]:
    params = {"resourceName": resource_name, "personFields": person_fields}
    return run_gws(["people", "people", "get", "--params", json.dumps(params)])


def create_contact(person: dict[str, Any]) -> dict[str, Any]:
    params = {"personFields": "names,metadata"}
    return run_gws(
        ["people", "people", "createContact", "--params", json.dumps(params), "--json", json.dumps(person, ensure_ascii=False)],
        mutating=True,
    )


def update_contact(existing: dict[str, Any], draft: CardDraft) -> dict[str, Any]:
    person = build_update(existing, draft)
    mask = ",".join(key for key in UPDATE_FIELDS.split(",") if key in person)
    params = {"resourceName": person["resourceName"], "updatePersonFields": mask, "personFields": "names,metadata"}
    return run_gws(
        ["people", "people", "updateContact", "--params", json.dumps(params), "--json", json.dumps(person, ensure_ascii=False)],
        mutating=True,
    )


def run_gws(args: list[str], allow_failure: bool = False, expect_json: bool = True, mutating: bool = False) -> dict[str, Any]:
    try:
        completed = subprocess.run(["gws", *args], check=False, capture_output=True, text=True, timeout=GWS_TIMEOUT)
    except FileNotFoundError:
        raise ContactSaveError("gws CLI를 찾을 수 없습니다. brew install googleworkspace-cli", kind="other")
    except subprocess.TimeoutExpired:
        if allow_failure:
            return {}
        if mutating:
            raise ContactSaveError("Google 응답 시간 초과. 저장 여부 확인 필요 (Google 주소록에서 확인하세요).", kind="unknown_outcome", detail="timeout")
        raise ContactSaveError("Google 응답 시간 초과. 잠시 후 다시 시도합니다.", kind="transient", retryable=True, detail="timeout")
    if completed.returncode != 0:
        if allow_failure:
            return {}
        raise classify_gws_error(completed.stdout or "", completed.stderr or "")
    payload = _parse_json(completed.stdout or "")
    if isinstance(payload, dict):
        return payload
    if not expect_json:
        return {}
    raise ContactSaveError(UNKNOWN_OUTCOME_MESSAGE, kind="unknown_outcome", detail=_sanitize(completed.stdout or "")[:500])


def _parse_json(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")  # tolerate a notice line printed before the JSON body
    if start > 0:
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            return None
    return None


def classify_gws_error(stdout: str, stderr: str) -> ContactSaveError:
    """Turn a failed gws call into a ContactSaveError with a short Korean message."""
    code: Optional[int] = None
    message = ""
    reason = ""
    for text in (stdout, stderr):
        payload = _parse_json(text)
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            try:
                code = int(error.get("code")) if error.get("code") is not None else None
            except (TypeError, ValueError):
                code = None
            message = str(error.get("message") or "")
            reason = str(error.get("reason") or error.get("status") or "")
            break
    raw = f"{stdout}\n{stderr}"
    detail = _sanitize(raw)[:1000]
    if code is None:
        found = re.search(r"\b(?:Error|HTTP/\S+|status(?: code)?:?)\s*(\d{3})\b", raw, re.I) or re.search(r"\b(5\d\d|429|401|403|404)\b", raw)
        code = int(found.group(1)) if found else None
    lowered = f"{message} {reason} {detail}".lower()
    is_html = bool(re.search(r"<\s*(!doctype|html|body)\b", raw, re.I))

    if code in (401,) or (code == 403 and not re.search(r"rate|quota", lowered)) or (code is None and any(h in lowered for h in AUTH_HINTS)):
        return ContactSaveError(AUTH_MESSAGE, kind="auth", detail=detail)
    if code == 404:
        return ContactSaveError("연락처를 찾을 수 없습니다(404).", kind="not_found", detail=detail)
    if code is not None and (code >= 500 or code in (403, 408, 409, 429)):
        return ContactSaveError(f"Google 서버 일시 오류({code}). 잠시 후 다시 시도합니다.", kind="transient", retryable=True, detail=detail)
    if is_html or (code is None and any(h in lowered for h in TRANSIENT_HINTS)):
        label = f"({code})" if code else ""
        return ContactSaveError(f"Google 서버 일시 오류{label}. 잠시 후 다시 시도합니다.", kind="transient", retryable=True, detail=detail)
    if code == 400:
        return ContactSaveError(f"Google이 요청을 거부했습니다(400): {_short(message) or '입력값 확인 필요'}", kind="invalid", detail=detail)
    short = _short(message) or _short(gws_error_message(stdout, stderr))
    label = f"({code})" if code else ""
    return ContactSaveError(f"Google 연락처 작업 실패{label}: {short}", kind="other", detail=detail)


def gws_error_message(stdout: str, stderr: str) -> str:
    """Best raw error line from gws output (for logs)."""
    payload = _parse_json(stdout)
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = error.get("message") or error.get("reason")
        if message:
            return str(message)
    lines = []
    for text in (stderr, stdout):
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("Using keyring backend:"):
                lines.append(line)
    return "\n".join(lines) or "gws command failed"


def _sanitize(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("Using keyring backend:")]
    return " ".join(" ".join(lines).split())


def _short(text: str, limit: int = 120) -> str:
    text = _sanitize(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def gws_auth_status() -> dict[str, Any]:
    """Result of `gws auth status` (empty dict when gws is missing or broken)."""
    try:
        completed = subprocess.run(["gws", "auth", "status"], capture_output=True, text=True, timeout=30)
        return json.loads(completed.stdout) if completed.stdout.strip() else {}
    except Exception as exc:  # FileNotFoundError, timeout, JSON errors
        return {"token_error": str(exc)}
