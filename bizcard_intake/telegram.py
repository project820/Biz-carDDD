"""Telegram channel: long polling, per-card buttons, batches, chat allowlist, health alerts."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import cards, contacts, doctor, pipeline
from .pipeline import DATA_DIR, LOG_DIR

API_BASE = "https://api.telegram.org"
HEALTH_CHECK_INTERVAL = 24 * 3600
RETRY_SCAN_INTERVAL = 60
LONG_POLL_SECONDS = 25  # Telegram allows up to 50; fewer new connections, no added latency
RETRY_ATTEMPTS = 4
RETRY_DELAY = 1.5
LIST_WORDS = {"목록", "/list", "list", "대기", "대기목록", "/pending"}
HELP_WORDS = {"/start", "/help", "도움말", "help", "사용법"}
HELP_TEXT = (
    "명함 사진을 보내주세요. 여러 장을 한 번에 보내도 명함마다 따로 초안을 만듭니다.\n\n"
    "- 고치기: 초안 메시지에 답장으로 '항목: 값'을 보내세요. 예) 직함: 과장\n"
    "  (이름, 직함, 직함원문, 회사, 휴대전화, 회사전화, 팩스, 이메일, 웹사이트, 주소, 메모. 비우려면 '팩스: -')\n"
    "- 문장으로 써도 됩니다. 예) 이메일은 kim@acme.com 이야\n"
    "- '목록': 저장 안 된 명함과 일괄 저장 버튼"
)

log = logging.getLogger("bizcard")
_rejected_chats: set[int] = set()

# Photo work (image prep, OCR, model calls) runs on a small worker pool so an
# album of cards is processed in parallel and buttons stay responsive. Tests and
# the CLI leave _executor as None, which runs the work inline.
_executor: Optional[ThreadPoolExecutor] = None
_group_lock = threading.Lock()
_group_pending: dict[tuple[int, str], int] = {}
_group_summary_wanted: set[tuple[int, str]] = set()


def worker_count() -> int:
    try:
        return max(1, int(os.environ.get("BIZCARD_WORKERS", "30")))
    except ValueError:
        return 30


def submit(fn, *args, group: Optional[tuple[int, str]] = None) -> None:
    """Run photo work on the pool (or inline) and track album completion."""
    if group:
        with _group_lock:
            _group_pending[group] = _group_pending.get(group, 0) + 1

    def job() -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("background job failed: %s", getattr(fn, "__name__", fn))
        finally:
            if group:
                _group_job_done(group)

    if _executor is None:
        job()
    else:
        _executor.submit(job)


_progress_at: dict[tuple[int, str], float] = {}
PROGRESS_EVERY = 3.0


def _group_job_done(group: tuple[int, str]) -> None:
    with _group_lock:
        _group_pending[group] = _group_pending.get(group, 1) - 1
        finished = _group_pending[group] <= 0
        wanted = finished and group in _group_summary_wanted
        if finished:
            _group_pending.pop(group, None)
            _group_summary_wanted.discard(group)
            _progress_at.pop(group, None)
        show_progress = not finished and time.monotonic() - _progress_at.get(group, 0.0) >= PROGRESS_EVERY
        if show_progress:
            _progress_at[group] = time.monotonic()
    if wanted:
        _safe_group_summary(group)
    elif show_progress:
        _update_group_progress(group)


def _update_group_progress(group: tuple[int, str]) -> None:
    members = cards.group_cards(group[0], group[1])
    done = sum(1 for c in members if c.get("status") != "processing")
    with _summary_lock:
        message_id = cards.load_state("groups").get(f"{group[0]}:{group[1]}")
    if not message_id:
        return
    try:
        edit_message(_summary_token, group[0], message_id, f"보낸 명함 {len(members)}장 처리 중: 완료 {done}/{len(members)}")
    except Exception:
        log.warning("progress update failed group=%s", group[1])


def _request_group_summary(group: tuple[int, str]) -> None:
    with _group_lock:
        busy = _group_pending.get(group, 0) > 0
        if busy:
            _group_summary_wanted.add(group)
    if not busy:
        _safe_group_summary(group)


def _safe_group_summary(group: tuple[int, str]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    try:
        send_group_summary(_summary_token or token, group[0], group[1])
    except Exception:
        log.exception("group summary failed chat=%s group=%s", *group)


_summary_token = ""
_summary_lock = threading.RLock()


def allowed_chat_ids() -> set[int]:
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
    return {int(part) for part in raw.replace(";", ",").split(",") if part.strip()}


def run(token: str, data_dir: Path = DATA_DIR) -> None:
    if not allowed_chat_ids():
        raise SystemExit("TELEGRAM_ALLOWED_CHAT_IDS is required (comma-separated chat ids allowed to use the bot)")
    for note in pipeline.migrate_legacy():
        log.info("migrated: %s", note)
    recover_interrupted()
    delete_webhook(token)
    me = api_get(token, "getMe")["result"]
    username = me.get("username", "unknown")
    log.info("polling @%s", username)
    print(f"Polling @{username} - Ctrl-C to stop", flush=True)

    global _executor, _summary_token
    _summary_token = token
    _executor = ThreadPoolExecutor(max_workers=worker_count(), thread_name_prefix="card")
    log.info("photo workers=%d", worker_count())
    offset = cards.load_state("telegram").get("offset")
    next_health_check = 0.0
    next_retry_scan = 0.0
    while True:
        if time.monotonic() >= next_health_check:
            health_check(token)
            next_health_check = time.monotonic() + HEALTH_CHECK_INTERVAL
            next_retry_scan = time.monotonic() + RETRY_SCAN_INTERVAL
        elif time.monotonic() >= next_retry_scan:
            retry_due_and_report(token)
            next_retry_scan = time.monotonic() + RETRY_SCAN_INTERVAL
        try:
            updates = get_updates(token, offset)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log.warning("getUpdates failed (%s); backing off", exc)
            time.sleep(5)
            continue
        offset = handle_updates(token, updates, data_dir, offset)


def handle_updates(token: str, updates: list, data_dir: Path = DATA_DIR, offset: Optional[int] = None) -> Optional[int]:
    global _summary_token
    _summary_token = _summary_token or token
    groups: set[tuple[int, str]] = set()
    for update in updates:
        offset = update["update_id"] + 1
        cards.save_state("telegram", {"offset": offset})
        try:
            group = handle_update(token, update, data_dir)
            if group:
                groups.add(group)
        except KeyboardInterrupt:
            raise
        except Exception:
            log.exception("handle_update failed for update_id=%s", update.get("update_id"))
            notify_failure(token, update)
    for group in sorted(groups):
        _request_group_summary(group)
    return offset


def recover_interrupted() -> None:
    """Cards left mid-step by a crash or restart go back to a state the user can act on."""
    for card in cards.list_cards(statuses=["processing", "saving"]):
        if card["status"] == "saving":
            card["status"] = "save_failed"
            card["error_kind"] = "unknown_outcome" if not card.get("resource_name") else "transient"
            card["last_error"] = "저장 도중 봇이 재시작되었습니다. 저장 여부를 확인한 뒤 다시 시도합니다."
            card["next_retry_at"] = time.time()
        else:
            card["status"] = "review"
            draft = pipeline.current_draft(card)
            draft.warnings.append("처리 도중 봇이 재시작됨: '인식 재시도'를 눌러주세요")
            card["draft"] = draft.normalize().to_dict()
        cards.add_history(card, "recovered_after_restart")
        cards.save_card(card)


def health_check(token: str) -> list[doctor.Check]:
    """Verify the external dependencies and alert the admin chat on failure."""
    failed = [check for check in doctor.run_checks(["parser", "gws"]) if not check.ok]
    for check in failed:
        log.warning("health check: %s: %s", check.name, check.detail)
    alert_chat_id = os.environ.get("BIZCARD_ALERT_CHAT_ID")
    if failed and alert_chat_id:
        waiting = pipeline.pending_retry_count()
        text = "봇 점검 경고\n" + "\n".join(f"- {c.detail}\n  해결: {c.fix}" for c in failed)
        if waiting:
            text += f"\n\n저장 대기 중인 명함 {waiting}건은 문제가 해결되면 자동으로 저장합니다."
        try:
            send_message(token, int(alert_chat_id), text)
        except Exception:
            log.exception("failed to send health alert")
    if not any(c.name == "gws" for c in failed):
        log.info("health check ok" if not failed else "health check: gws ok")
        retry_due_and_report(token, force=True)
    return failed


def retry_due_and_report(token: str, force: bool = False) -> None:
    def report(card: dict, outcome: dict) -> None:
        chat_id = int(card["chat_id"]) if str(card.get("chat_id", "")).lstrip("-").isdigit() else None
        if chat_id is None or (not outcome["ok"] and not force):
            return  # quiet on background failures; the next health check summarises them
        try:
            send_message(token, chat_id, save_result_text(card, outcome, retried=True), reply_markup=None if outcome["ok"] else saved_failed_buttons(card))
        except Exception:
            log.exception("failed to report retry card=%s", card["id"])

    if not any(True for _ in cards.list_cards(statuses=["save_failed", "saved"])):
        return
    saved, failures = pipeline.retry_due(force=force, on_result=report)
    if saved or failures:
        log.info("retry: saved=%d failed=%d", saved, len(failures))


def notify_failure(token: str, update: dict) -> None:
    chat_id = chat_id_of(update)
    if chat_id is None:
        return
    try:
        send_message(token, chat_id, "처리 중 오류가 발생했습니다. 명함은 보관되어 있으니 '목록'에서 확인하거나 다시 시도해주세요.")
    except Exception:
        log.exception("failed to notify chat_id=%s of error", chat_id)


def chat_id_of(update: dict) -> Optional[int]:
    message = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
    return (message.get("chat") or {}).get("id")


def handle_update(token: str, update: dict, data_dir: Path = DATA_DIR) -> Optional[tuple[int, str]]:
    """Handle one update. Returns (chat_id, media_group_id) when an album photo was added."""
    chat_id = chat_id_of(update)
    if chat_id is None:
        return None
    if chat_id not in allowed_chat_ids():
        reject_chat(token, chat_id)
        return None

    callback = update.get("callback_query")
    if callback:
        handle_callback(token, callback, chat_id)
        return None

    message = update.get("message") or {}
    photos = message.get("photo") or []
    document = message.get("document") or {}
    if not photos and str(document.get("mime_type", "")).startswith("image/"):
        photos = [{"file_id": document["file_id"]}]
    if photos:
        return handle_photo(token, chat_id, message, photos, data_dir)
    handle_text(token, chat_id, message)
    return None


def reject_chat(token: str, chat_id: int) -> None:
    log.warning("rejected chat_id=%s (not in TELEGRAM_ALLOWED_CHAT_IDS)", chat_id)
    if chat_id in _rejected_chats:
        return
    _rejected_chats.add(chat_id)
    send_message(token, chat_id, f"비공개 봇입니다. 사용하려면 봇 운영자의 TELEGRAM_ALLOWED_CHAT_IDS에 이 chat id를 추가하세요: {chat_id}")


# ---------------------------------------------------------------- photos

def handle_photo(token: str, chat_id: int, message: dict, photos: list, data_dir: Path) -> Optional[tuple[int, str]]:
    media_group_id = message.get("media_group_id")
    reply_card = cards.find_by_message(chat_id, (message.get("reply_to_message") or {}).get("message_id"))
    back_target = None
    if not media_group_id:
        if reply_card and reply_card.get("awaiting_back"):
            back_target = reply_card
        else:
            waiting = [c for c in cards.list_cards(chat_id, cards.REVIEW_STATUSES) if c.get("awaiting_back")]
            back_target = waiting[-1] if len(waiting) == 1 else None
    if back_target:
        back_target["awaiting_back"] = False  # the next photo must not also go to this card
        cards.save_card(back_target)
        submit(process_back_photo, token, chat_id, photos, data_dir, back_target)
        return None
    # Card creation happens here, in update order, so card numbers follow the
    # album; image prep, OCR and the model calls run on the worker pool.
    group = (chat_id, str(media_group_id)) if media_group_id else None
    card = cards.new_card(chat_id, media_group_id=group[1] if group else None)
    if group:
        _ensure_group_progress(token, group)
        submit(process_front_photo, token, chat_id, photos, data_dir, card, None, True, group=group)
        return group
    status_id = send_status(token, chat_id, f"{cards.label(card)} 처리 중: 이미지 보정 · OCR")
    cards.remember_message(card, status_id)
    cards.save_card(card)
    submit(process_front_photo, token, chat_id, photos, data_dir, card, status_id)
    return None


def _ensure_group_progress(token: str, group: tuple[int, str]) -> None:
    """One message per album that shows progress and later becomes the summary."""
    key = f"{group[0]}:{group[1]}"
    with _summary_lock:
        state = cards.load_state("groups")
        if state.get(key):
            return
        result = send_message(token, group[0], "보낸 명함 처리 중… (명함마다 사진과 초안이 하나씩 옵니다)")
        state[key] = result["result"]["message_id"]
        cards.save_state("groups", state)


def process_front_photo(token: str, chat_id: int, photos: list, data_dir: Path, card: dict, status_id: Optional[int] = None, album: bool = False) -> None:
    log.info("front photo chat=%s card=%s", chat_id, card["id"])
    if status_id is None and not album:
        status_id = send_status(token, chat_id, f"{cards.label(card)} 처리 중: 이미지 보정 · OCR")
        cards.remember_message(card, status_id)
        cards.save_card(card)
    try:
        front_path = save_largest_photo(token, photos, chat_id, data_dir, "front", card["id"])
    except Exception:
        log.exception("photo download failed card=%s", card["id"])
        pipeline.cancel(card)
        text = f"{cards.label(card)} 사진을 받지 못했습니다. 다시 보내주세요."
        if status_id is not None:
            edit_message(token, chat_id, status_id, text)
        else:
            send_message(token, chat_id, text)
        return
    card["images"]["front"] = str(front_path)
    pipeline.recognize_front(card, front_path)
    profile = card["images"].get("profile")
    preview = Path(profile) if profile and Path(profile).exists() else None
    if album:
        show_draft(token, chat_id, card, photo=preview)
        return
    if preview:
        try:
            send_photo(token, chat_id, preview, f"{cards.label(card)} 연락처 사진 미리보기")
        except Exception:
            log.exception("preview send failed card=%s", card["id"])
    show_draft(token, chat_id, card, status_id)


def process_back_photo(token: str, chat_id: int, photos: list, data_dir: Path, card: dict) -> None:
    status_id = send_status(token, chat_id, f"{cards.label(card)} 처리 중: 뒷면 OCR")
    back_path = save_largest_photo(token, photos, chat_id, data_dir, "back", card["id"])
    pipeline.recognize_back(card, back_path)
    show_draft(token, chat_id, card, status_id)


CAPTION_LIMIT = 1024


def show_draft(token: str, chat_id: int, card: dict, replace_message_id: Optional[int] = None, prefix: str = "", photo: Optional[Path] = None) -> None:
    text = (prefix + "\n\n" if prefix else "") + pipeline.draft_text(card) + "\n\n고칠 내용은 이 메시지에 답장으로 보내주세요."
    message_id = None
    if photo is not None:
        try:
            if len(text) <= CAPTION_LIMIT:
                message_id = send_photo(token, chat_id, photo, text, reply_markup=draft_buttons(card))["result"]["message_id"]
            else:
                send_photo(token, chat_id, photo, f"{cards.label(card)} 연락처 사진 미리보기")
        except Exception:
            log.exception("preview send failed card=%s", card["id"])
    if message_id is None and replace_message_id is not None:
        try:
            edit_message(token, chat_id, replace_message_id, text, reply_markup=draft_buttons(card))
            message_id = replace_message_id
        except Exception:
            log.warning("could not edit status message; sending a new draft message")
    if message_id is None:
        message_id = send_message(token, chat_id, text, reply_markup=draft_buttons(card))["result"]["message_id"]
    fresh = cards.load_card(card["id"]) or card
    cards.remember_message(fresh, message_id)
    cards.save_card(fresh)
    card.update(fresh)


def send_group_summary(token: str, chat_id: int, media_group_id: str) -> None:
    with _summary_lock:
        _send_group_summary(token, chat_id, media_group_id)


def _send_group_summary(token: str, chat_id: int, media_group_id: str) -> None:
    members = cards.group_cards(chat_id, media_group_id)
    if len(members) < 2:
        return
    text, markup = batch_summary(members, f"g:{media_group_id}", "보낸 명함")
    state = cards.load_state("groups")
    key = f"{chat_id}:{media_group_id}"
    message_id = state.get(key)
    if message_id:
        try:
            edit_message(token, chat_id, message_id, text, reply_markup=markup)
            return
        except Exception:
            pass
    result = send_message(token, chat_id, text, reply_markup=markup)
    state[key] = result["result"]["message_id"]
    cards.save_state("groups", state)


def batch_summary(members: list[dict], scope: str, title: str) -> tuple[str, Optional[dict]]:
    ready = [c for c in members if c["status"] in cards.REVIEW_STATUSES and pipeline.current_draft(c).is_savable]
    needs_input = [c for c in members if c["status"] in cards.REVIEW_STATUSES and not pipeline.current_draft(c).is_savable]
    saved = [c for c in members if c["status"] == "saved"]
    failed = [c for c in members if c["status"] == "save_failed"]
    lines = [f"{title} {len(members)}건: 저장 가능 {len(ready)} · 입력 필요 {len(needs_input)} · 저장됨 {len(saved)}"]
    for card in members:
        draft = pipeline.current_draft(card)
        mark = {"saved": "저장됨", "save_failed": "저장 보류", "cancelled": "취소", "processing": "처리 중"}.get(card["status"])
        if card["status"] == "review":
            mark = "확인 필요" if (draft.warnings or not draft.is_savable) else "준비"
        lines.append(f"- {cards.label(card)} {draft.display_name or '(이름 없음)'} · {mark}")
    if failed:
        lines.append(f"\n저장 보류 {len(failed)}건은 자동으로 다시 시도합니다.")
    markup = None
    if ready:
        markup = {"inline_keyboard": [[{"text": f"저장 가능한 {len(ready)}건 모두 저장", "callback_data": f"ba:{scope}"[:64]}]]}
    return "\n".join(lines), markup


# ---------------------------------------------------------------- text replies

def handle_text(token: str, chat_id: int, message: dict) -> None:
    text = (message.get("text") or message.get("caption") or "").strip()
    if not text:
        send_message(token, chat_id, "명함 사진을 보내주세요.")
        return
    if text.lower() in HELP_WORDS:
        send_message(token, chat_id, HELP_TEXT)
        return
    if text.lower() in LIST_WORDS:
        send_open_list(token, chat_id)
        return

    card = cards.find_by_message(chat_id, (message.get("reply_to_message") or {}).get("message_id"))
    if card is None:
        open_cards = cards.list_cards(chat_id, cards.REVIEW_STATUSES)
        if len(open_cards) == 1:
            card = open_cards[0]
        elif not open_cards:
            send_message(token, chat_id, "명함 사진을 보내주세요. (사용법: 도움말)")
            return
        else:
            send_message(token, chat_id, f"검토 중인 명함이 {len(open_cards)}건입니다. 고칠 명함의 초안 메시지에 답장으로 보내주세요.")
            send_open_list(token, chat_id)
            return
    if card["status"] not in cards.REVIEW_STATUSES:
        send_message(token, chat_id, f"{cards.label(card)}은(는) 이미 {'저장' if card['status'] == 'saved' else '종료'}된 명함입니다.")
        return
    status_id = send_status(token, chat_id, f"{cards.label(card)} 처리 중: 초안 수정")
    ok, note = pipeline.edit(card, text)
    if not ok:
        edit_message(token, chat_id, status_id, f"{cards.label(card)} {note}")
        cards.remember_message(card, status_id)
        cards.save_card(card)
        return
    show_draft(token, chat_id, card, status_id, prefix=f"{cards.label(card)} {note}")


def send_open_list(token: str, chat_id: int) -> None:
    open_cards = cards.list_cards(chat_id, cards.REVIEW_STATUSES + ("processing",))
    if not open_cards:
        send_message(token, chat_id, "저장 안 된 명함이 없습니다.")
        return
    text, markup = batch_summary(open_cards, "all", "저장 안 된 명함")
    send_message(token, chat_id, text + "\n\n각 명함을 고치려면 해당 초안 메시지에 답장하세요.", reply_markup=markup)


# ---------------------------------------------------------------- buttons

def handle_callback(token: str, callback: dict, chat_id: int) -> None:
    callback_id = callback.get("id")
    if callback_id:
        try:
            api_post(token, "answerCallbackQuery", {"callback_query_id": callback_id})
        except Exception:
            log.warning("answerCallbackQuery failed; continuing")

    data = str(callback.get("data") or "")
    log.info("callback chat=%s data=%s", chat_id, data)
    action, _, rest = data.partition(":")
    if action == "ba":
        submit(approve_batch, token, chat_id, rest)
        return
    if action not in ("ap", "cx", "ed", "bk", "rs", "sv"):
        send_message(token, chat_id, "예전 버튼입니다. 최근 초안 메시지의 버튼을 눌러주세요. ('목록'으로 확인)")
        return
    card_id, _, revision = rest.partition(":")
    card = cards.load_card(card_id)
    if card is None or card.get("chat_id") != str(chat_id):
        send_message(token, chat_id, "해당 명함을 찾을 수 없습니다. '목록'으로 확인해주세요.")
        return

    if action in ("ap", "sv"):
        submit(approve_card, token, chat_id, card, int(revision) if revision.isdigit() and action == "ap" else None)
    elif action == "cx":
        if card["status"] == "saved":
            send_message(token, chat_id, f"{cards.label(card)}은(는) 이미 저장되어 취소할 수 없습니다.")
            return
        pipeline.cancel(card)
        send_message(token, chat_id, f"{cards.label(card)} 취소했습니다.")
    elif action == "ed":
        prompt = edit_prompt(card)
        result = send_message(token, chat_id, prompt, reply_markup={"force_reply": True, "input_field_placeholder": "직함: 과장"})
        cards.remember_message(card, result["result"]["message_id"])
        cards.save_card(card)
    elif action == "bk":
        card["awaiting_back"] = True
        result = send_message(token, chat_id, f"{cards.label(card)} 뒷면 사진을 이 메시지에 답장으로 보내주세요.", reply_markup={"force_reply": True})
        cards.remember_message(card, result["result"]["message_id"])
        cards.save_card(card)
    elif action == "rs":
        if not card["images"].get("front") or not Path(card["images"]["front"]).exists():
            send_message(token, chat_id, f"{cards.label(card)} 원본 사진이 없어 재인식할 수 없습니다. 사진을 다시 보내주세요.")
            return
        status_id = send_status(token, chat_id, f"{cards.label(card)} 다시 인식하는 중")
        card["status"] = "processing"
        cards.save_card(card)
        submit(_recognize_again, token, chat_id, card, status_id)


def _recognize_again(token: str, chat_id: int, card: dict, status_id: int) -> None:
    pipeline.recognize_front(card)
    show_draft(token, chat_id, card, status_id)


def approve_card(token: str, chat_id: int, card: dict, revision: Optional[int]) -> dict:
    display = pipeline.current_draft(card).display_name
    status_id = send_status(token, chat_id, f"{cards.label(card)} 저장 중: {display}")
    outcome = pipeline.approve(card, revision)
    markup = None if outcome["ok"] or outcome.get("stale") else (draft_buttons(card) if outcome.get("invalid") else saved_failed_buttons(card))
    edit_message(token, chat_id, status_id, save_result_text(card, outcome), reply_markup=markup)
    cards.remember_message(card, status_id)
    cards.save_card(card)
    return outcome


def approve_batch(token: str, chat_id: int, scope: str) -> None:
    if scope.startswith("g:"):
        members = cards.group_cards(chat_id, scope[2:])
    else:
        members = cards.list_cards(chat_id, cards.REVIEW_STATUSES)
    targets = [c for c in members if c["status"] == "review" and pipeline.current_draft(c).is_savable]
    if not targets:
        send_message(token, chat_id, "저장할 수 있는 명함이 없습니다.")
        return
    status_id = send_status(token, chat_id, f"{len(targets)}건 저장 중… (Google 연락처는 한 건씩 저장합니다)")
    lines = []
    ok = 0
    last_progress = time.monotonic()
    with contacts.contact_snapshot():
        for index, card in enumerate(targets, 1):
            outcome = pipeline.approve(card)
            ok += 1 if outcome["ok"] else 0
            lines.append(f"- {cards.label(card)} {pipeline.current_draft(card).display_name}: {'저장' if outcome['ok'] else '보류 · ' + outcome['message']}")
            if index < len(targets) and time.monotonic() - last_progress >= 5:
                last_progress = time.monotonic()
                try:
                    edit_message(token, chat_id, status_id, f"{len(targets)}건 저장 중… {index}/{len(targets)}")
                except Exception:
                    pass
    skipped = [c for c in members if c["status"] == "review" and c not in targets]
    text = f"일괄 저장 {ok}/{len(targets)}건 완료\n" + "\n".join(lines)
    if skipped:
        text += f"\n\n입력이 더 필요한 {len(skipped)}건은 그대로 남겨두었습니다."
    if ok < len(targets):
        text += "\n저장 보류된 명함은 자동으로 다시 시도합니다."
    edit_message(token, chat_id, status_id, text)
    if scope.startswith("g:"):
        try:
            send_group_summary(token, chat_id, scope[2:])
        except Exception:
            log.exception("group summary refresh failed")


def save_result_text(card: dict, outcome: dict, retried: bool = False) -> str:
    name = pipeline.current_draft(card).display_name
    prefix = "자동 재시도 " if retried else ""
    if outcome.get("already"):
        return f"{cards.label(card)} {outcome['message']}"
    if outcome["ok"]:
        lines = [f"{cards.label(card)} {prefix}저장 완료", outcome["message"]]
        if outcome.get("photo") == "failed":
            lines.append("연락처 사진은 올리지 못해 잠시 후 다시 올립니다.")
        elif outcome.get("photo") == "missing":
            lines.append("연락처 사진 파일이 없어 사진 없이 저장했습니다.")
        for warning in outcome.get("warnings") or []:
            if "사진" not in warning:
                lines.append(f"참고: {warning}")
        return "\n".join(lines)
    if outcome.get("stale") or outcome.get("invalid"):
        return f"{cards.label(card)} {outcome['message']}"
    hint = {
        "auth": "Mac mini에서 Google 로그인 후 자동으로 저장합니다.",
        "transient": "초안은 보관했고 잠시 후 자동으로 다시 저장합니다.",
        "unknown_outcome": "저장되었는지 확인한 뒤 다시 시도합니다. 같은 연락처가 중복되지 않도록 기존 연락처를 먼저 찾습니다.",
    }.get(outcome.get("kind"), "초안은 보관했습니다. '지금 다시 저장'을 누르거나 자동 재시도를 기다려주세요.")
    return f"{cards.label(card)} {prefix}저장 보류: {name}\n사유: {outcome['message']}\n{hint}"


def edit_prompt(card: dict) -> str:
    draft = pipeline.current_draft(card)
    current = [
        f"이름: {draft.name}",
        f"직함: {draft.short_title}",
        f"회사: {draft.company}",
        f"휴대전화: {', '.join(draft.mobile)}",
        f"회사전화: {', '.join(draft.work)}",
        f"이메일: {', '.join(draft.email)}",
    ]
    return (
        f"{cards.label(card)} 바꿀 줄만 이 메시지에 답장으로 보내주세요. (AI 없이 그대로 반영)\n"
        "아래를 복사해 고쳐 보내도 됩니다. 비우려면 '팩스: -'\n\n" + "\n".join(current)
    )


def draft_buttons(card: dict) -> dict:
    cid, rev = card["id"], card.get("revision") or 0
    rows = [
        [{"text": "저장", "callback_data": f"ap:{cid}:{rev}"}, {"text": "직접 수정", "callback_data": f"ed:{cid}"}],
        [{"text": "뒷면 추가", "callback_data": f"bk:{cid}"}, {"text": "인식 재시도", "callback_data": f"rs:{cid}"}, {"text": "취소", "callback_data": f"cx:{cid}"}],
    ]
    return {"inline_keyboard": rows}


def saved_failed_buttons(card: dict) -> dict:
    cid = card["id"]
    return {"inline_keyboard": [[{"text": "지금 다시 저장", "callback_data": f"sv:{cid}"}, {"text": "직접 수정", "callback_data": f"ed:{cid}"}, {"text": "취소", "callback_data": f"cx:{cid}"}]]}


def save_largest_photo(token: str, photos: list, chat_id: int, data_dir: Path, side: str, card_id: str = "") -> Path:
    file_id = photos[-1]["file_id"]
    file_info = api_get(token, "getFile", {"file_id": file_id})["result"]
    file_path = file_info["file_path"]
    name = Path(file_path).name
    target = data_dir / str(chat_id) / side / (f"{card_id}_{name}" if card_id else name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(download_file(token, file_path))
    log.info("saved %s photo: %s", side, target)
    return target


def get_updates(token: str, offset: Optional[int] = None) -> list:
    params = {"timeout": LONG_POLL_SECONDS, "allowed_updates": json.dumps(["message", "callback_query"])}
    if offset is not None:
        params["offset"] = offset
    return api_get(token, "getUpdates", params, timeout=LONG_POLL_SECONDS + 10)["result"]


def send_status(token: str, chat_id: int, text: str) -> int:
    return send_message(token, chat_id, text)["result"]["message_id"]


_pace_lock = threading.Lock()
_last_send: dict[int, float] = {}


def _pace(chat_id: int) -> None:
    """Keep messages to one chat spaced out; Telegram throttles bursts per chat."""
    interval = float(os.environ.get("BIZCARD_TELEGRAM_INTERVAL", "0.35"))
    with _pace_lock:
        now = time.monotonic()
        wait = _last_send.get(chat_id, 0.0) + interval - now
        _last_send[chat_id] = max(now, _last_send.get(chat_id, 0.0) + interval)
    if wait > 0:
        time.sleep(wait)


def send_message(token: str, chat_id: int, text: str, reply_markup: Optional[dict] = None) -> dict:
    _pace(chat_id)
    payload = {"chat_id": chat_id, "text": text[:4000]}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return api_post(token, "sendMessage", payload)


def edit_message(token: str, chat_id: int, message_id: int, text: str, reply_markup: Optional[dict] = None) -> dict:
    _pace(chat_id)
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000]}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        return api_post(token, "editMessageText", payload)
    except RuntimeError as exc:
        if "not modified" in str(exc):
            return {"ok": True}
        raise


def send_photo(token: str, chat_id: int, image_path: Path, caption: str = "", reply_markup: Optional[dict] = None) -> dict:
    _pace(chat_id)
    boundary = f"----bizcard-{uuid.uuid4().hex}"
    parts = [
        _multipart_field(boundary, "chat_id", str(chat_id)),
        _multipart_field(boundary, "caption", caption[:CAPTION_LIMIT]),
        *([_multipart_field(boundary, "reply_markup", json.dumps(reply_markup))] if reply_markup else []),
        _multipart_file(boundary, "photo", image_path.name, "image/jpeg", image_path.read_bytes()),
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    request = Request(
        method_url(token, "sendPhoto"),
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )

    def call() -> dict:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    result = with_retry(call, "Telegram sendPhoto")
    if result.get("ok") is not True:
        raise RuntimeError(result.get("description") or "Telegram sendPhoto failed")
    return result


def _multipart_field(boundary: str, name: str, value: str) -> bytes:
    return f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")


def _multipart_file(boundary: str, name: str, filename: str, content_type: str, content: bytes) -> bytes:
    header = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    return header + content + b"\r\n"


def delete_webhook(token: str) -> None:
    api_post(token, "deleteWebhook", {"drop_pending_updates": False})


RATE_LIMIT_WAITS = 8


def with_retry(call, label: str, attempts: int = RETRY_ATTEMPTS):
    """Retry transient network failures; wait out Telegram's 429 retry_after
    (not counted as a failed attempt); fail fast on other 4xx."""
    attempt, rate_waits = 1, 0
    while True:
        try:
            return call()
        except HTTPError as exc:
            body = _http_error_body(exc)
            if exc.code == 429 and rate_waits < RATE_LIMIT_WAITS:
                rate_waits += 1
                delay = float(((body or {}).get("parameters") or {}).get("retry_after") or RETRY_DELAY * rate_waits)
                log.warning("%s rate limited; waiting %.0fs (%d/%d)", label, delay, rate_waits, RATE_LIMIT_WAITS)
                time.sleep(min(delay, 60))
                continue
            if 400 <= exc.code < 500:
                description = (body or {}).get("description") or f"HTTP {exc.code}"
                raise RuntimeError(f"{label}: {description}") from exc
            if attempt >= attempts:
                raise
            log.warning("%s failed (HTTP %s); retry %d/%d", label, exc.code, attempt, attempts - 1)
            time.sleep(RETRY_DELAY * attempt)
            attempt += 1
        except (URLError, ConnectionError, TimeoutError, OSError) as exc:
            if attempt >= attempts:
                raise
            log.warning("%s failed (%s); retry %d/%d", label, exc, attempt, attempts - 1)
            time.sleep(RETRY_DELAY * attempt)
            attempt += 1


def _http_error_body(exc: HTTPError) -> Optional[dict]:
    try:
        return json.loads(exc.read().decode("utf-8"))
    except Exception:
        return None


def api_get(token: str, method: str, params: Optional[dict] = None, timeout: int = 10) -> dict:
    def call() -> dict:
        with urlopen(method_url(token, method, params), timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    payload = with_retry(call, f"Telegram {method}")
    if payload.get("ok") is not True:
        raise RuntimeError(payload.get("description") or f"Telegram {method} failed")
    return payload


def api_post(token: str, method: str, payload: dict) -> dict:
    request = Request(
        method_url(token, method),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    def call() -> dict:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    result = with_retry(call, f"Telegram {method}")
    if result.get("ok") is not True:
        raise RuntimeError(result.get("description") or f"Telegram {method} failed")
    return result


def download_file(token: str, file_path: str) -> bytes:
    url = f"{API_BASE}/file/bot{quote(token, safe=':')}/{quote(file_path, safe='/')}"

    def call() -> bytes:
        with urlopen(url, timeout=20) as response:
            return response.read()

    return with_retry(call, "Telegram download")


def method_url(token: str, method: str, params: Optional[dict] = None) -> str:
    url = f"{API_BASE}/bot{quote(token, safe=':')}/{method}"
    return f"{url}?{urlencode(params)}" if params else url


def setup_logging() -> None:
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    log.addHandler(stream)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(LOG_DIR / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)
        stream.setLevel(logging.WARNING)  # bot.log rotates; launchd's stderr file does not
    except OSError:
        log.warning("could not open log file; continuing with stderr only")


def main() -> None:
    setup_logging()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    run(token)
