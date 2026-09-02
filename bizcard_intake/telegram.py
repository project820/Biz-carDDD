"""Telegram channel: long polling, buttons, chat allowlist, health alerts."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import time
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlencode
from urllib.error import URLError
from urllib.request import Request, urlopen

from . import doctor, pipeline
from .pipeline import DATA_DIR, LOG_DIR, delete_session, load_session, preserve_pending_session, save_session

API_BASE = "https://api.telegram.org"
HEALTH_CHECK_INTERVAL = 24 * 3600
RETRY_ATTEMPTS = 4
RETRY_DELAY = 1.5

log = logging.getLogger("bizcard")
_rejected_chats: set[int] = set()


def allowed_chat_ids() -> set[int]:
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
    return {int(part) for part in raw.replace(";", ",").split(",") if part.strip()}


def run(token: str, data_dir: Path = DATA_DIR) -> None:
    if not allowed_chat_ids():
        raise SystemExit("TELEGRAM_ALLOWED_CHAT_IDS is required (comma-separated chat ids allowed to use the bot)")
    delete_webhook(token)
    me = api_get(token, "getMe")["result"]
    username = me.get("username", "unknown")
    log.info("polling @%s", username)
    print(f"Polling @{username} - Ctrl-C to stop", flush=True)

    offset = None
    next_health_check = 0.0
    while True:
        if time.monotonic() >= next_health_check:
            health_check(token)
            next_health_check = time.monotonic() + HEALTH_CHECK_INTERVAL
        try:
            updates = get_updates(token, offset)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log.warning("getUpdates failed (%s); backing off", exc)
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            try:
                handle_update(token, update, data_dir)
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("handle_update failed for update_id=%s", update.get("update_id"))
                notify_failure(token, update)
        time.sleep(0.2)


def health_check(token: str) -> list[doctor.Check]:
    """Verify the external dependencies and alert the admin chat on failure."""
    failed = [check for check in doctor.run_checks(["parser", "gws"]) if not check.ok]
    for check in failed:
        log.warning("health check: %s: %s", check.name, check.detail)
    alert_chat_id = os.environ.get("BIZCARD_ALERT_CHAT_ID")
    if failed and alert_chat_id:
        text = "봇 점검 경고\n" + "\n".join(f"- {c.detail}\n  해결: {c.fix}" for c in failed)
        try:
            send_message(token, int(alert_chat_id), text)
        except Exception:
            log.exception("failed to send health alert")
    if not failed:
        log.info("health check ok")
    return failed


def notify_failure(token: str, update: dict) -> None:
    chat_id = chat_id_of(update)
    if chat_id is None:
        return
    try:
        send_message(token, chat_id, "처리 중 오류가 발생했습니다. 다시 시도해주세요.")
    except Exception:
        log.exception("failed to notify chat_id=%s of error", chat_id)


def chat_id_of(update: dict) -> Optional[int]:
    message = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
    return (message.get("chat") or {}).get("id")


def handle_update(token: str, update: dict, data_dir: Path = DATA_DIR) -> None:
    chat_id = chat_id_of(update)
    if chat_id is None:
        return
    if chat_id not in allowed_chat_ids():
        reject_chat(token, chat_id)
        return

    callback = update.get("callback_query")
    if callback:
        handle_callback(token, callback, chat_id)
        return

    message = update.get("message") or {}
    photos = message.get("photo") or []
    session = load_session(str(chat_id)) or {}
    if not photos:
        text = (message.get("text") or "").strip()
        if text and session.get("draft"):
            process_draft_reply(token, chat_id, text, session)
        else:
            send_message(token, chat_id, "명함 사진을 보내주세요.")
        return

    if session.get("awaiting") == "back":
        process_back_photo(token, chat_id, photos, data_dir, session)
    else:
        process_front_photo(token, chat_id, photos, data_dir)


def reject_chat(token: str, chat_id: int) -> None:
    log.warning("rejected chat_id=%s (not in TELEGRAM_ALLOWED_CHAT_IDS)", chat_id)
    if chat_id in _rejected_chats:
        return
    _rejected_chats.add(chat_id)
    send_message(token, chat_id, f"비공개 봇입니다. 사용하려면 봇 운영자의 TELEGRAM_ALLOWED_CHAT_IDS에 이 chat id를 추가하세요: {chat_id}")


def process_front_photo(token: str, chat_id: int, photos: list, data_dir: Path) -> None:
    log.info("front photo from chat=%s", chat_id)
    preserve_pending_session(str(chat_id), "superseded_by_new_front_photo")
    status_id = send_status(token, chat_id, "처리 중: 이미지 보정")
    front_path = save_largest_photo(token, photos, chat_id, data_dir, "front")
    edit_message(token, chat_id, status_id, "처리 중: OCR 및 파싱")
    session = pipeline.scan_front(front_path)
    save_session(str(chat_id), session)
    edit_message(token, chat_id, status_id, "초안 준비 완료 (수정할 내용은 답장으로 보내주세요)")
    send_photo(token, chat_id, Path(session["profile_image_path"]), "연락처 이미지 미리보기")
    send_message(token, chat_id, pipeline.draft_text(session), reply_markup=front_action_buttons())


def process_back_photo(token: str, chat_id: int, photos: list, data_dir: Path, session: dict) -> None:
    status_id = send_status(token, chat_id, "처리 중: 뒷면 OCR")
    back_path = save_largest_photo(token, photos, chat_id, data_dir, "back")
    session = pipeline.scan_back(session, back_path)
    save_session(str(chat_id), session)
    edit_message(token, chat_id, status_id, "초안 준비 완료")
    send_message(token, chat_id, pipeline.draft_text(session), reply_markup=approve_cancel_buttons())


def process_draft_reply(token: str, chat_id: int, text: str, session: dict) -> None:
    status_id = send_status(token, chat_id, "처리 중: 초안 수정")
    if not pipeline.revise(session, text):
        edit_message(token, chat_id, status_id, "초안 수정 실패: 다시 답장해주세요.")
        return
    save_session(str(chat_id), session)
    edit_message(token, chat_id, status_id, "초안 수정 완료")
    buttons = approve_cancel_buttons() if session.get("back_draft") else front_action_buttons()
    send_message(token, chat_id, pipeline.draft_text(session), reply_markup=buttons)


def handle_callback(token: str, callback: dict, chat_id: int) -> None:
    callback_id = callback.get("id")
    if callback_id:
        api_post(token, "answerCallbackQuery", {"callback_query_id": callback_id})

    data = callback.get("data")
    log.info("callback chat=%s data=%s", chat_id, data)
    if data == "approve":
        session = load_session(str(chat_id))
        if not session:
            send_message(token, chat_id, "저장 실패: 승인할 초안이 없습니다.")
            return
        status_id = send_status(token, chat_id, "저장 중: Google Contacts")
        result = pipeline.approve(str(chat_id), session)
        log.info("approve result chat=%s: %s", chat_id, result.splitlines()[0])
        edit_message(token, chat_id, status_id, result)
    elif data == "cancel":
        delete_session(str(chat_id))
        send_message(token, chat_id, "취소했습니다.")
    elif data == "retry":
        delete_session(str(chat_id))
        send_message(token, chat_id, "재시도할 명함 사진을 보내주세요.")
    elif data == "back_scan":
        session = load_session(str(chat_id)) or {}
        session["awaiting"] = "back"
        save_session(str(chat_id), session)
        send_message(token, chat_id, "뒷면 사진을 보내주세요.")
    else:
        send_message(token, chat_id, "알 수 없는 선택입니다.")


def save_largest_photo(token: str, photos: list, chat_id: int, data_dir: Path, side: str) -> Path:
    file_id = photos[-1]["file_id"]
    file_info = api_get(token, "getFile", {"file_id": file_id})["result"]
    file_path = file_info["file_path"]
    target = data_dir / str(chat_id) / side / Path(file_path).name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(download_file(token, file_path))
    log.info("saved %s photo: %s", side, target)
    return target


def get_updates(token: str, offset: Optional[int] = None) -> list:
    params = {"timeout": 3, "allowed_updates": json.dumps(["message", "callback_query"])}
    if offset is not None:
        params["offset"] = offset
    return api_get(token, "getUpdates", params)["result"]


def send_status(token: str, chat_id: int, text: str) -> int:
    return send_message(token, chat_id, text)["result"]["message_id"]


def send_message(token: str, chat_id: int, text: str, reply_markup: Optional[dict] = None) -> dict:
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return api_post(token, "sendMessage", payload)


def edit_message(token: str, chat_id: int, message_id: int, text: str) -> dict:
    return api_post(token, "editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text})


def send_photo(token: str, chat_id: int, image_path: Path, caption: str = "") -> None:
    boundary = f"----bizcard-{uuid.uuid4().hex}"
    parts = [
        _multipart_field(boundary, "chat_id", str(chat_id)),
        _multipart_field(boundary, "caption", caption),
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


def front_action_buttons() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "승인", "callback_data": "approve"}, {"text": "취소", "callback_data": "cancel"}],
            [{"text": "재시도", "callback_data": "retry"}, {"text": "뒷면스캔", "callback_data": "back_scan"}],
        ]
    }


def approve_cancel_buttons() -> dict:
    return {"inline_keyboard": [[{"text": "승인", "callback_data": "approve"}, {"text": "취소", "callback_data": "cancel"}]]}


def with_retry(call, label: str, attempts: int = RETRY_ATTEMPTS):
    """Retry transient network failures (connection resets are frequent on some links)."""
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except (URLError, ConnectionError, TimeoutError, OSError) as exc:
            if attempt == attempts:
                raise
            log.warning("%s failed (%s); retry %d/%d", label, exc, attempt, attempts - 1)
            time.sleep(RETRY_DELAY * attempt)


def api_get(token: str, method: str, params: Optional[dict] = None) -> dict:
    def call() -> dict:
        with urlopen(method_url(token, method, params), timeout=10) as response:
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
    except OSError:
        log.warning("could not open log file; continuing with stderr only")


def main() -> None:
    setup_logging()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    run(token)
