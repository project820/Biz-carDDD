"""Channel-independent core: card -> recognize -> review/edit -> approve -> Google Contacts.

The channel (Telegram, CLI) creates a card per photo and calls these functions;
every state change is persisted on the card before and after slow or remote
steps, so a crash, restart or network error never loses a card or makes an
unapproved draft save itself.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable, Optional

from . import cards
from .contacts import ContactSaveError, SaveResult, save_contact, upload_photo
from .draft import CardDraft, apply_edits, parse_edits, preserve_locked
from .image_processing import PreparedImages, prepare_card_images
from .parser import merge_drafts, parse_card, revise_draft
from .vision_ocr import extract_text

DATA_DIR = Path("data/telegram_photos")
LOG_DIR = Path("data/logs")
SESSION_DIR = Path("data/sessions")  # legacy, migrated into cards
SESSION_ARCHIVE_DIR = Path("data/session_archive")
PENDING_CONTACT_DIR = Path("data/pending_contacts")  # legacy, migrated into cards
IMAGE_KEYS = ("front", "profile", "ocr", "llm", "rectified", "back")
LEGACY_IMAGE_KEYS = {"front_image_path": "front", "profile_image_path": "profile", "ocr_image_path": "ocr", "llm_image_path": "llm", "rectified_image_path": "rectified"}
RETRY_BASE_SECONDS = 120
RETRY_MAX_SECONDS = 6 * 3600
AUTH_RETRY_SECONDS = 1800

log = logging.getLogger("bizcard")


def profile_image_path(source_path: Path) -> Path:
    return source_path.parent.parent / "profile" / f"{source_path.stem}_profile.jpg"


def current_draft(card: dict) -> CardDraft:
    return CardDraft.from_session_value(card.get("draft"))


def set_draft(card: dict, draft: CardDraft, event: str) -> None:
    card["draft"] = draft.to_dict()
    card["revision"] = int(card.get("revision") or 0) + 1
    cards.add_history(card, event, draft.display_name)


# ---------------------------------------------------------------- recognition

def scan_front(front_path: Path) -> dict:
    """Image prep + OCR + parse for one front photo. Returns images and draft."""
    started = time.monotonic()
    prepared = prepare_card_images(front_path, profile_image_path(front_path))
    images = {
        "front": str(front_path),
        "profile": str(prepared.profile_path),
        "ocr": str(prepared.ocr_path),
        "llm": str(prepared.llm_path),
        "rectified": str(prepared.rectified_path) if prepared.rectified_path else None,
    }
    ocr = extract_text(prepared.ocr_path)
    log.info("vision ocr image=%s lines=%d avg_conf=%.3f", prepared.ocr_path.name, len(ocr.lines), ocr.average_confidence)
    draft = parse_card(prepared.llm_path, ocr, "front")
    log.info("front draft done image=%s elapsed=%.1fs", front_path.name, time.monotonic() - started)
    return {"images": images, "rectifier": prepared.source_used, "draft": draft}


def recognize_front(card: dict, front_path: Optional[Path] = None) -> dict:
    """Fill the card from its front photo. A failure still leaves a reviewable card."""
    front = Path(front_path or card["images"]["front"])
    card["images"]["front"] = str(front)
    card["status"] = "processing"
    cards.save_card(card)
    previous = current_draft(card)
    try:
        result = scan_front(front)
    except Exception as exc:  # keep the card editable whatever broke
        log.exception("recognition failed card=%s", card["id"])
        draft = preserve_locked(CardDraft(warnings=["자동 인식 실패: '직접 수정'으로 입력하거나 '인식 재시도'를 눌러주세요"]), previous)
        card["recognition_error"] = str(exc)[:300]
    else:
        card["images"].update({k: v for k, v in result["images"].items() if v})
        card["rectifier"] = result["rectifier"]
        draft = preserve_locked(result["draft"], previous)
        card["front_draft"] = draft.to_dict()
        card["recognition_error"] = None
    set_draft(card, draft, "recognized")
    card["status"] = "review"
    cards.save_card(card)
    return card


def recognize_back(card: dict, back_path: Path) -> dict:
    card["images"]["back"] = str(back_path)
    card["awaiting_back"] = False
    previous = current_draft(card)
    try:
        prepared = prepare_card_images(back_path, back_path.parent.parent / "back_tmp" / f"{back_path.stem}_profile.jpg")
        back = parse_card(prepared.llm_path, extract_text(prepared.ocr_path), "back")
        cleanup_paths(prepared.profile_path, prepared.ocr_path, prepared.llm_path, prepared.rectified_path)
        merged = merge_drafts(previous, back)
        card["back_draft"] = back.to_dict()
    except Exception as exc:
        log.exception("back recognition failed card=%s", card["id"])
        merged = CardDraft.from_dict(previous.to_dict())
        merged.warnings.append(f"뒷면 인식 실패: {str(exc)[:80]}")
    set_draft(card, preserve_locked(merged, previous), "back_merged")
    card["status"] = "review"
    cards.save_card(card)
    return card


# ---------------------------------------------------------------- editing

def edit(card: dict, text: str) -> tuple[bool, str]:
    """Apply a reply. '필드: 값' lines are applied directly; free text goes to the AI reviser."""
    edits, unknown = parse_edits(text)
    previous = current_draft(card)
    if edits:
        set_draft(card, apply_edits(previous, edits), "manual_edit")
        _reopen_for_review(card)
        cards.save_card(card)
        note = f"\n반영하지 못한 줄: {' / '.join(unknown[:3])}" if unknown else ""
        return True, "직접 수정 반영" + note
    revised = revise_draft(previous, text)
    if revised is None:
        return False, "자동 수정 실패: '이름: 홍길동'처럼 '항목: 값' 형식으로 보내주시면 바로 반영됩니다."
    set_draft(card, preserve_locked(revised, previous), "ai_edit")
    _reopen_for_review(card)
    cards.save_card(card)
    return True, "초안 수정 반영"


def _reopen_for_review(card: dict) -> None:
    if card.get("status") == "save_failed":
        card["approved_revision"] = None
        card["next_retry_at"] = None
    if card.get("status") in ("save_failed", "processing"):
        card["status"] = "review"


# ---------------------------------------------------------------- saving

def approve(card: dict, revision: Optional[int] = None) -> dict:
    """Save the approved revision. Returns a result dict for the channel to render."""
    if card.get("status") == "saved":
        return {"ok": True, "already": True, "message": f"이미 저장된 명함입니다: {current_draft(card).display_name}"}
    if card.get("status") not in cards.REVIEW_STATUSES:
        return {"ok": False, "message": "지금은 저장할 수 없는 상태입니다. 잠시 후 다시 눌러주세요."}
    if revision is not None and int(revision) != int(card.get("revision") or 0):
        return {"ok": False, "stale": True, "message": "초안이 그 사이 수정되었습니다. 가장 최근 초안의 '저장' 버튼을 눌러주세요."}
    draft = current_draft(card)
    missing = draft.missing_for_save()
    if missing:
        return {"ok": False, "invalid": True, "message": f"저장하려면 {', '.join(missing)}이(가) 필요합니다. '직접 수정'으로 입력해주세요."}
    card["approved_revision"] = card.get("revision")
    cards.add_history(card, "approved", str(card["approved_revision"]))
    return _save(card)


def _save(card: dict) -> dict:
    draft = current_draft(card)
    card["status"] = "saving"
    card["save_attempts"] = int(card.get("save_attempts") or 0) + 1
    cards.save_card(card)

    def checkpoint(resource_name: str, action: str) -> None:
        card["resource_name"] = resource_name
        card["save_action"] = card.get("save_action") or action
        cards.add_history(card, "contact_written", f"{action} {resource_name}")
        cards.save_card(card)

    try:
        result: SaveResult = save_contact(draft, card["images"].get("profile"), resource_name=card.get("resource_name"), checkpoint=checkpoint)
    except ContactSaveError as exc:
        return _save_failed(card, str(exc), getattr(exc, "kind", "other"), getattr(exc, "retryable", False))
    except Exception as exc:  # unexpected adapter failure: keep the card, retry later
        log.exception("save failed card=%s", card["id"])
        return _save_failed(card, f"예상치 못한 오류: {str(exc)[:120]}", "other", True)

    card["status"] = "saved"
    card["resource_name"] = result.resource_name or card.get("resource_name")
    card["save_action"] = card.get("save_action") or result.action
    card["photo_status"] = result.photo
    card["last_error"] = None
    card["error_kind"] = None
    card["next_retry_at"] = None
    card["save_warnings"] = list(result.warnings)
    cards.add_history(card, "saved", result.summary)
    keep = {"profile"} if result.photo == "failed" else set()
    cleanup_card_images(card, keep=keep)
    cards.save_card(card)
    archive_saved(card, result.summary)
    log.info("saved card=%s %s photo=%s", card["id"], card["resource_name"], result.photo)
    return {"ok": True, "result": result, "message": result.summary, "warnings": list(result.warnings), "photo": result.photo}


def _save_failed(card: dict, message: str, kind: str, retryable: bool) -> dict:
    card["status"] = "save_failed"
    card["last_error"] = message
    card["error_kind"] = kind
    if kind == "invalid":
        card["next_retry_at"] = None
    elif kind == "auth":
        card["next_retry_at"] = time.time() + AUTH_RETRY_SECONDS
    else:
        delay = min(RETRY_BASE_SECONDS * (2 ** max(0, int(card.get("save_attempts") or 1) - 1)), RETRY_MAX_SECONDS)
        card["next_retry_at"] = time.time() + delay
    cards.add_history(card, "save_failed", f"{kind}: {message}")
    cards.save_card(card)
    log.warning("save failed card=%s kind=%s: %s", card["id"], kind, message)
    return {"ok": False, "kind": kind, "retryable": kind != "invalid", "message": message}


def retry_due(force: bool = False, on_result: Optional[Callable[[dict, dict], None]] = None) -> tuple[int, list[str]]:
    """Retry approved saves whose backoff elapsed, and photos that failed to upload."""
    saved, failures = 0, []
    for card in cards.list_cards(statuses=["save_failed"]):
        if card.get("approved_revision") is None or card.get("approved_revision") != card.get("revision"):
            continue  # never save a revision nobody approved
        if card.get("error_kind") == "invalid":
            continue
        if not force and (card.get("next_retry_at") or 0) > time.time():
            continue
        outcome = _save(card)
        if outcome["ok"]:
            saved += 1
        else:
            failures.append(f"{cards.label(card)} {current_draft(card).display_name}: {outcome['message']}")
        if on_result:
            on_result(card, outcome)
    for card in cards.list_cards(statuses=["saved"]):
        if card.get("photo_status") != "failed" or not card.get("resource_name"):
            continue
        path = card["images"].get("profile")
        if not path or not Path(path).exists():
            card["photo_status"] = "missing"
            cards.save_card(card)
            continue
        try:
            upload_photo(card["resource_name"], path)
        except ContactSaveError as exc:
            log.warning("photo retry failed card=%s: %s", card["id"], exc)
            continue
        card["photo_status"] = "uploaded"
        cleanup_card_images(card)
        cards.save_card(card)
    return saved, failures


def pending_retry_count() -> int:
    return sum(1 for c in cards.list_cards(statuses=["save_failed"]) if c.get("approved_revision") == c.get("revision") and c.get("error_kind") != "invalid")


def cancel(card: dict) -> None:
    card["status"] = "cancelled"
    card["awaiting_back"] = False
    cards.add_history(card, "cancelled")
    cleanup_card_images(card)
    cards.save_card(card)


def archive_saved(card: dict, summary: str) -> None:
    try:
        SESSION_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        archived = dict(card, save_output=summary, archived_at=cards.now())
        (SESSION_ARCHIVE_DIR / f"card-{card['id']}.json").write_text(json.dumps(archived, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        log.exception("archive failed card=%s", card["id"])


def cleanup_card_images(card: dict, keep: set = frozenset()) -> None:
    for key in IMAGE_KEYS:
        if key in keep:
            continue
        path = card["images"].get(key)
        if path:
            cleanup_paths(path)
            card["images"][key] = None


def cleanup_paths(*items) -> None:
    for item in items:
        if isinstance(item, PreparedImages):
            paths = [item.profile_path, item.ocr_path, item.llm_path, item.rectified_path]
        else:
            paths = [item]
        for path in paths:
            if not path:
                continue
            try:
                Path(path).unlink()
            except OSError:
                pass


def draft_text(card: dict) -> str:
    draft = current_draft(card)
    header = f"{cards.label(card)} 명함 초안"
    return draft.to_text(header=header)


# ---------------------------------------------------------------- legacy migration

def migrate_legacy() -> list[str]:
    """Turn old per-chat sessions and pending_contacts files into cards (idempotent).

    - pending entries already saved later (a success archive with the same front
      photo exists) are retired, which stops the daily 'image missing' failure;
    - entries archived only because a newer photo arrived become review cards and
      are never saved without approval;
    - approved-but-failed entries become save_failed cards that retry.
    """
    notes: list[str] = []
    saved_fronts = _archived_front_images()
    seen_fronts: set[str] = set()
    migrated_dir = PENDING_CONTACT_DIR / "migrated"
    for path in sorted(PENDING_CONTACT_DIR.glob("*.json")):
        try:
            legacy = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        chat_id = path.name.split("-", 1)[0]
        front = legacy.get("front_image_path") or ""
        if front and front in saved_fronts:
            notes.append(f"{path.name}: 이미 저장된 연락처라 재시도 목록에서 정리")
        elif front and front in seen_fronts:
            notes.append(f"{path.name}: 중복 기록 정리")
        else:
            approved = legacy.get("status") == "approved" and legacy.get("pending_reason") != "superseded_by_new_front_photo"
            card = _card_from_legacy(chat_id, legacy, "save_failed" if approved else "review")
            if approved:
                card["approved_revision"] = card["revision"]
                card["last_error"] = "이전 저장 실패분 (로그인/서버 오류)"
                card["error_kind"] = "auth" if "credential" in str(legacy.get("pending_reason", "")).lower() else "transient"
                card["next_retry_at"] = time.time()
            cards.save_card(card)
            notes.append(f"{path.name}: {cards.label(card)} {'자동 재시도 대상으로' if approved else '검토 대기로'} 이전")
        if front:
            seen_fronts.add(front)
        migrated_dir.mkdir(parents=True, exist_ok=True)
        path.rename(migrated_dir / path.name)
    for path in sorted(SESSION_DIR.glob("*.json")) if SESSION_DIR.exists() else []:
        try:
            legacy = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        front = legacy.get("front_image_path") or ""
        if legacy.get("draft") and not (front and (front in saved_fronts or front in seen_fronts)):
            card = _card_from_legacy(path.stem, legacy, "review")
            cards.save_card(card)
            notes.append(f"세션 {path.name}: {cards.label(card)} 검토 대기로 이전")
        migrated = SESSION_DIR / "migrated"
        migrated.mkdir(parents=True, exist_ok=True)
        path.rename(migrated / path.name)
    for note in notes:
        log.info("legacy migration: %s", note)
    return notes


def _card_from_legacy(chat_id: str, legacy: dict, status: str) -> dict:
    card = cards.new_card(chat_id, source="legacy")
    card["images"] = {short: legacy.get(long) for long, short in LEGACY_IMAGE_KEYS.items() if legacy.get(long)}
    card["front_draft"] = legacy.get("front_draft")
    card["back_draft"] = legacy.get("back_draft")
    set_draft(card, CardDraft.from_session_value(legacy.get("draft")), "migrated")
    card["status"] = status
    return card


def _archived_front_images() -> set[str]:
    fronts = set()
    for path in SESSION_ARCHIVE_DIR.glob("*.json") if SESSION_ARCHIVE_DIR.exists() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        front = data.get("front_image_path") or (data.get("images") or {}).get("front")
        if front and str(data.get("save_output", "")).strip():
            fronts.add(front)
    return fronts
