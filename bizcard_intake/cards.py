"""Durable per-card records: one JSON file per business card.

Every photo becomes a card with its own id, status and draft revision, so
several cards can be in review at once, buttons always target one card, and a
failed save keeps its draft and images until it is saved or cancelled.

Statuses:
  processing  -> image prep / OCR / parsing running
  review      -> draft ready for the approver (also after a failed recognition)
  saving      -> approved revision is being written to Google Contacts
  save_failed -> approved, but Google rejected or was unreachable; retried later
  saved       -> contact exists in Google (photo may still be pending)
  cancelled   -> discarded by the approver
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Iterable, Optional

CARD_DIR = Path("data/cards")
OPEN_STATUSES = ("processing", "review", "saving", "save_failed")
REVIEW_STATUSES = ("review", "save_failed")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def new_card(chat_id, *, media_group_id: Optional[str] = None, source: str = "telegram") -> dict:
    card_id = time.strftime("%y%m%d") + secrets.token_hex(3)
    card = {
        "id": card_id,
        "seq": _next_seq(),
        "chat_id": str(chat_id),
        "source": source,
        "status": "processing",
        "revision": 0,
        "created_at": now(),
        "updated_at": now(),
        "media_group_id": media_group_id,
        "images": {},
        "draft": {},
        "front_draft": None,
        "back_draft": None,
        "awaiting_back": False,
        "message_ids": [],
        "approved_revision": None,
        "resource_name": None,
        "save_action": None,
        "photo_status": None,
        "save_attempts": 0,
        "last_error": None,
        "error_kind": None,
        "next_retry_at": None,
        "history": [],
    }
    save_card(card)
    return card


def label(card: dict) -> str:
    return f"[명함 {card.get('seq') or card['id']}]"


def card_path(card_id: str) -> Path:
    return CARD_DIR / f"{card_id}.json"


def save_card(card: dict) -> None:
    card["updated_at"] = now()
    CARD_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(card_path(card["id"]), json.dumps(card, ensure_ascii=False, indent=2))


def load_card(card_id: str) -> Optional[dict]:
    if not card_id or "/" in card_id or ".." in card_id:
        return None
    path = card_path(card_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_cards(chat_id=None, statuses: Optional[Iterable[str]] = None) -> list[dict]:
    if not CARD_DIR.exists():
        return []
    wanted = set(statuses) if statuses else None
    found = []
    for path in sorted(CARD_DIR.glob("*.json")):
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # one corrupt file must not block the others
        if chat_id is not None and card.get("chat_id") != str(chat_id):
            continue
        if wanted is not None and card.get("status") not in wanted:
            continue
        found.append(card)
    found.sort(key=lambda c: (c.get("created_at") or "", c.get("seq") or 0))
    return found


def find_by_message(chat_id, message_id) -> Optional[dict]:
    if message_id is None:
        return None
    for card in reversed(list_cards(chat_id)):
        if message_id in card.get("message_ids", []):
            return card
    return None


def group_cards(chat_id, media_group_id: str) -> list[dict]:
    return [c for c in list_cards(chat_id) if c.get("media_group_id") == media_group_id]


def remember_message(card: dict, message_id: Optional[int]) -> None:
    if message_id is not None and message_id not in card["message_ids"]:
        card["message_ids"] = (card["message_ids"] + [message_id])[-20:]


def add_history(card: dict, event: str, detail: str = "") -> None:
    card.setdefault("history", []).append({"at": now(), "event": event, "detail": detail[:300]})
    card["history"] = card["history"][-30:]


def load_state(name: str) -> dict:
    path = CARD_DIR / f"_{name}.state"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(name: str, data: dict) -> None:
    CARD_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(CARD_DIR / f"_{name}.state", json.dumps(data, ensure_ascii=False, indent=2))


def _next_seq() -> int:
    state = load_state("seq")
    seq = int(state.get("seq", 0)) + 1
    save_state("seq", {"seq": seq})
    return seq


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
