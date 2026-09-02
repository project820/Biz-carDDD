"""Channel-independent core: scan -> draft -> (revise) -> approve -> Google Contacts."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from .contacts import ContactSaveError, save_contact
from .draft import CardDraft
from .image_processing import PreparedImages, prepare_card_images
from .parser import merge_drafts, parse_card, revise_draft
from .vision_ocr import extract_text

DATA_DIR = Path("data/telegram_photos")
SESSION_DIR = Path("data/sessions")
LOG_DIR = Path("data/logs")
SESSION_ARCHIVE_DIR = Path("data/session_archive")
PENDING_CONTACT_DIR = Path("data/pending_contacts")
IMAGE_PATH_KEYS = ("front_image_path", "profile_image_path", "ocr_image_path", "llm_image_path", "rectified_image_path")

log = logging.getLogger("bizcard")


def profile_image_path(source_path: Path) -> Path:
    return source_path.parent.parent / "profile" / f"{source_path.stem}_profile.jpg"


def scan_front(front_path: Path) -> dict:
    started = time.monotonic()
    prepared = prepare_card_images(front_path, profile_image_path(front_path))
    ocr = extract_text(prepared.ocr_path)
    log.info("vision ocr image=%s lines=%d avg_conf=%.3f", prepared.ocr_path.name, len(ocr.lines), ocr.average_confidence)
    draft = parse_card(prepared.llm_path, ocr, "front")
    log.info("front draft done image=%s elapsed=%.1fs", front_path.name, time.monotonic() - started)
    return {
        "status": "drafted",
        "front_image_path": str(front_path),
        "profile_image_path": str(prepared.profile_path),
        "ocr_image_path": str(prepared.ocr_path),
        "llm_image_path": str(prepared.llm_path),
        "rectified_image_path": str(prepared.rectified_path) if prepared.rectified_path else None,
        "rectifier": prepared.source_used,
        "front_draft": draft.to_dict(),
        "draft": draft.to_dict(),
    }


def scan_back(session: dict, back_path: Path) -> dict:
    prepared = prepare_card_images(back_path, back_path.parent.parent / "back_tmp" / f"{back_path.stem}_profile.jpg")
    back = parse_card(prepared.llm_path, extract_text(prepared.ocr_path), "back")
    merged = merge_drafts(CardDraft.from_session_value(session.get("front_draft") or session.get("draft")), back)
    cleanup_paths(back_path, prepared)
    session.update({"status": "merged_drafted", "awaiting": None, "back_draft": back.to_dict(), "draft": merged.to_dict()})
    return session


def revise(session: dict, reply: str) -> bool:
    revised = revise_draft(current_draft(session), reply)
    if revised is None:
        return False
    session["draft"] = revised.to_dict()
    if not session.get("back_draft"):
        session["front_draft"] = revised.to_dict()
    return True


def current_draft(session: dict) -> CardDraft:
    return CardDraft.from_session_value(session.get("draft"))


def draft_text(session: dict) -> str:
    return current_draft(session).to_text()


def approve(key: str, session: dict) -> str:
    session["status"] = "approved"
    save_session(key, session)
    try:
        output = save_contact(current_draft(session), session.get("profile_image_path"))
    except ContactSaveError as exc:
        archive_pending_contact(key, session, str(exc))
        return f"저장 실패: {exc}"
    except Exception as exc:  # timeouts and other unexpected adapter failures
        archive_pending_contact(key, session, str(exc))
        return f"저장 실패: {exc}"

    archive_session(key, session, output)
    delete_session(key)
    cleanup_paths(*(session.get(k) for k in IMAGE_PATH_KEYS))
    return f"저장 완료\n{output}"


def retry_pending() -> tuple[int, list[str]]:
    """Retry queued saves; returns (saved_count, failure messages)."""
    saved_dir = PENDING_CONTACT_DIR / "saved"
    failures: list[str] = []
    saved = 0
    for path in sorted(PENDING_CONTACT_DIR.glob("*.json")):
        session = json.loads(path.read_text(encoding="utf-8"))
        try:
            output = save_contact(current_draft(session), session.get("profile_image_path"))
        except Exception as exc:
            failures.append(f"{path.name}: {exc}")
            continue
        saved_dir.mkdir(parents=True, exist_ok=True)
        path.rename(saved_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{path.name}")
        cleanup_paths(*(session.get(k) for k in IMAGE_PATH_KEYS))
        saved += 1
        log.info("retry saved %s: %s", path.name, output)
    return saved, failures


def save_session(key: str, session: dict) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    (SESSION_DIR / f"{key}.json").write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")


def load_session(key: str) -> Optional[dict]:
    path = SESSION_DIR / f"{key}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def delete_session(key: str) -> None:
    try:
        (SESSION_DIR / f"{key}.json").unlink()
    except OSError:
        pass


def archive_session(key: str, session: dict, save_output: str) -> None:
    SESSION_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archived = dict(session)
    archived["save_output"] = save_output
    archived["archived_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    (SESSION_ARCHIVE_DIR / f"{key}-{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(
        json.dumps(archived, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def preserve_pending_session(key: str, reason: str) -> None:
    session = load_session(key)
    if session:
        archive_pending_contact(key, session, reason)


def archive_pending_contact(key: str, session: dict, reason: str) -> Path:
    PENDING_CONTACT_DIR.mkdir(parents=True, exist_ok=True)
    pending = dict(session)
    pending["pending_reason"] = reason
    pending["pending_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    image_path = Path(str(session.get("front_image_path") or "unknown"))
    suffix = image_path.stem if image_path.name != "unknown" else time.strftime("%H%M%S")
    path = PENDING_CONTACT_DIR / f"{key}-{time.strftime('%Y%m%d-%H%M%S')}-{suffix}.json"
    path.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


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
