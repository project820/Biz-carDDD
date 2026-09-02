"""Google Contacts adapter through the `gws` CLI (People API)."""

from __future__ import annotations

import base64
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from .draft import CardDraft

PERSON_FIELDS = "names,phoneNumbers,emailAddresses,addresses,organizations,urls,biographies,photos,metadata"
UPDATE_FIELDS = "names,phoneNumbers,emailAddresses,addresses,organizations,urls,biographies"
CONTACTS_SCOPE = "https://www.googleapis.com/auth/contacts"
LOGIN_COMMAND = f"gws auth login --scopes {CONTACTS_SCOPE}"


class ContactSaveError(RuntimeError):
    pass


def save_contact(draft: CardDraft, profile_image_path: Optional[str]) -> str:
    """Create or update the contact; returns a one-line summary. Raises ContactSaveError."""
    if not draft.is_savable:
        raise ContactSaveError("이름 또는 전화번호가 없어 Google Contacts에 저장하지 않았습니다.")

    person = build_person(draft)
    existing = find_existing_contact(draft)
    if existing:
        saved = update_contact(existing, person)
        action = "updated"
    else:
        saved = create_contact(person)
        action = "created"

    resource_name = saved.get("resourceName") or (existing or {}).get("resourceName")
    if resource_name:
        update_photo(resource_name, profile_image_path)
        verify_contact(resource_name)
    return f"{action}: {draft.name_field} / {draft.company} / {resource_name}"


def build_person(draft: CardDraft) -> dict[str, Any]:
    person: dict[str, Any] = {"names": [{"givenName": draft.name_field, "familyName": draft.company}]}

    organization: dict[str, str] = {}
    if draft.company:
        organization["name"] = draft.company
    if draft.title:
        organization["title"] = draft.title
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

    biography = "\n".join(
        part for part in [draft.notes, f"검증 경고: {'; '.join(draft.warnings)}" if draft.warnings else ""] if part
    )
    if biography:
        person["biographies"] = [{"value": biography, "contentType": "TEXT_PLAIN"}]
    return person


def find_existing_contact(draft: CardDraft) -> Optional[dict[str, Any]]:
    """Scan all contacts instead of searchContacts: the search index lags a few
    minutes behind createContact, so two quick approvals of the same card
    would otherwise create duplicates."""
    mobile = draft.mobile[0] if draft.mobile else ""
    name_key = draft.name.split()[0] if draft.name.split() else ""  # tolerate "홍길동 이사" in name
    if not mobile or not name_key:
        return None

    page_token = None
    while True:
        params: dict[str, Any] = {
            "resourceName": "people/me",
            "personFields": "names,phoneNumbers,emailAddresses,organizations,metadata",
            "pageSize": 1000,
        }
        if page_token:
            params["pageToken"] = page_token
        data = run_gws(["people", "people", "connections", "list", "--params", json.dumps(params, ensure_ascii=False)])
        for person in data.get("connections", []):
            if has_matching_mobile(person, mobile) and has_matching_name(person, name_key):
                return person
        page_token = data.get("nextPageToken")
        if not page_token:
            return None


def has_matching_mobile(person: dict[str, Any], mobile: str) -> bool:
    target = normalize_phone(mobile)
    return any(normalize_phone(phone.get("value", "")) == target for phone in person.get("phoneNumbers", []))


def has_matching_name(person: dict[str, Any], name_key: str) -> bool:
    for name in person.get("names", []):
        candidates = [
            name.get("displayName", ""),
            name.get("givenName", ""),
            " ".join(part for part in [name.get("givenName", ""), name.get("familyName", "")] if part),
        ]
        for candidate in candidates:
            normalized = re.sub(r"\s+", "", candidate)
            if normalized == name_key or normalized.startswith(name_key):
                return True
    return False


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D+", "", value)
    if digits.startswith("82") and len(digits) >= 11:
        return "0" + digits[2:]
    return digits


def create_contact(person: dict[str, Any]) -> dict[str, Any]:
    params = {"personFields": PERSON_FIELDS}
    return run_gws(["people", "people", "createContact", "--params", json.dumps(params), "--json", json.dumps(person, ensure_ascii=False)])


def update_contact(existing: dict[str, Any], person: dict[str, Any]) -> dict[str, Any]:
    resource_name = existing["resourceName"]
    person["resourceName"] = resource_name
    for key in ("etag", "metadata"):
        if existing.get(key):
            person[key] = existing[key]
    params = {"resourceName": resource_name, "updatePersonFields": UPDATE_FIELDS, "personFields": PERSON_FIELDS}
    return run_gws(["people", "people", "updateContact", "--params", json.dumps(params), "--json", json.dumps(person, ensure_ascii=False)])


def update_photo(resource_name: str, image_path: Optional[str]) -> None:
    if not image_path:
        return
    path = Path(image_path)
    if not path.exists():
        raise ContactSaveError(f"연락처 이미지 파일이 없습니다. {image_path}")
    body = {"photoBytes": base64.b64encode(path.read_bytes()).decode("ascii"), "personFields": "photos"}
    run_gws(["people", "people", "updateContactPhoto", "--params", json.dumps({"resourceName": resource_name}), "--json", json.dumps(body)])


def verify_contact(resource_name: str) -> None:
    params = {"resourceName": resource_name, "personFields": "names,phoneNumbers,photos"}
    run_gws(["people", "people", "get", "--params", json.dumps(params)])


def run_gws(args: list[str], allow_failure: bool = False) -> dict[str, Any]:
    try:
        completed = subprocess.run(["gws", *args], check=False, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise ContactSaveError("gws CLI를 찾을 수 없습니다. brew install googleworkspace-cli")
    if completed.returncode != 0:
        if allow_failure:
            return {}
        raise ContactSaveError(gws_error_message(completed.stdout, completed.stderr))
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}


def gws_error_message(stdout: str, stderr: str) -> str:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        payload = {}
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


def gws_auth_status() -> dict[str, Any]:
    """Result of `gws auth status` (empty dict when gws is missing or broken)."""
    try:
        completed = subprocess.run(["gws", "auth", "status"], capture_output=True, text=True, timeout=30)
        return json.loads(completed.stdout) if completed.stdout.strip() else {}
    except Exception as exc:  # FileNotFoundError, timeout, JSON errors
        return {"token_error": str(exc)}
