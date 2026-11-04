import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from bizcard_intake import cards, doctor, pipeline, telegram
from bizcard_intake.contacts import SaveResult
from bizcard_intake.draft import CardDraft

ALLOWED = {"TELEGRAM_ALLOWED_CHAT_IDS": "42"}


class FakeTelegram:
    def __init__(self):
        self.sent, self.edited, self.photos = [], [], []
        self.next_id = 100

    def send_message(self, token, chat_id, text, reply_markup=None):
        self.next_id += 1
        self.sent.append((text, reply_markup, self.next_id))
        return {"result": {"message_id": self.next_id}}

    def edit_message(self, token, chat_id, message_id, text, reply_markup=None):
        self.edited.append((text, reply_markup, message_id))
        return {}

    def send_photo(self, token, chat_id, path, caption=""):
        self.photos.append(caption)

    def last_markup(self):
        for text, markup, _ in reversed(self.edited + self.sent):
            if markup:
                return markup
        return None


def fake_scan(names):
    names = list(names)

    def scan(path):
        name = names.pop(0)
        return {"images": {"front": str(path), "profile": str(path)}, "rectifier": "test",
                "draft": CardDraft.from_dict({"name": name, "title": "과장", "company": "ACME", "mobile": [f"010-0000-{len(names)}"]})}
    return scan


def photo_update(update_id, file_id, media_group_id=None, reply_to=None):
    message = {"chat": {"id": 42}, "message_id": 1000 + update_id, "photo": [{"file_id": file_id}]}
    if media_group_id:
        message["media_group_id"] = media_group_id
    if reply_to:
        message["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": message}


def text_update(update_id, text, reply_to=None):
    message = {"chat": {"id": 42}, "message_id": 2000 + update_id, "text": text}
    if reply_to:
        message["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": message}


def callback(update_id, data):
    return {"update_id": update_id, "callback_query": {"id": str(update_id), "data": data, "message": {"chat": {"id": 42}}}}


class TelegramTest(unittest.TestCase):
    def setUp(self):
        telegram._rejected_chats.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.tg = FakeTelegram()
        self.patches = [
            patch.dict(os.environ, ALLOWED),
            patch.object(cards, "CARD_DIR", self.tmp / "cards"),
            patch.object(pipeline, "SESSION_ARCHIVE_DIR", self.tmp / "archive"),
            patch.object(telegram, "send_message", self.tg.send_message),
            patch.object(telegram, "edit_message", self.tg.edit_message),
            patch.object(telegram, "send_photo", self.tg.send_photo),
            patch.object(telegram, "api_post", lambda token, method, payload: {"ok": True}),
            patch.object(telegram, "api_get", lambda token, method, params=None, timeout=10: {"result": {"file_path": f"photos/{params['file_id']}.jpg"}}),
            patch.object(telegram, "download_file", lambda token, file_path: b"jpg"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._tmp.cleanup()

    def run_updates(self, updates):
        telegram.handle_updates("token", updates, self.tmp / "photos")

    def test_chat_outside_allowlist_is_rejected_once(self):
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_CHAT_IDS": "7"}):
            for i in range(2):
                telegram.handle_update("token", photo_update(i, "x"))
        self.assertEqual(len(self.tg.sent), 1)
        self.assertEqual(cards.list_cards(), [])

    def test_run_refuses_to_start_without_allowlist(self):
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_CHAT_IDS": ""}):
            with self.assertRaises(SystemExit):
                telegram.run("token")

    def test_photo_creates_card_with_card_scoped_buttons(self):
        with patch.object(pipeline, "scan_front", fake_scan(["정호준"])):
            self.run_updates([photo_update(1, "a")])
        [card] = cards.list_cards()
        self.assertEqual(card["status"], "review")
        draft_text, markup, _ = self.tg.edited[-1]
        self.assertIn("표시 이름: 정호준 과장 ACME", draft_text)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], f"ap:{card['id']}:{card['revision']}")
        self.assertTrue(Path(card["images"]["front"]).name.startswith(card["id"]))

    def test_album_makes_one_card_per_photo_and_batch_save(self):
        with patch.object(pipeline, "scan_front", fake_scan(["가", "나", "다"])):
            self.run_updates([photo_update(1, "a", "G1"), photo_update(2, "b", "G1"), photo_update(3, "c", "G1")])
        self.assertEqual(len(cards.list_cards()), 3)
        summary, markup, _ = self.tg.sent[-1]
        self.assertIn("보낸 명함 3건", summary)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "ba:g:G1")
        saved = []
        with patch.object(pipeline, "save_contact", lambda d, i, resource_name=None, checkpoint=None: saved.append(d.name) or SaveResult("created", "people/x", "uploaded", [], d.display_name)):
            self.run_updates([callback(9, "ba:g:G1")])
        self.assertEqual(saved, ["가", "나", "다"])
        self.assertTrue(all(c["status"] == "saved" for c in cards.list_cards()))

    def test_reply_edits_only_the_card_it_replies_to(self):
        with patch.object(pipeline, "scan_front", fake_scan(["가", "나"])):
            self.run_updates([photo_update(1, "a"), photo_update(2, "b")])
        first, second = cards.list_cards()
        target_message = first["message_ids"][-1]
        with patch.object(pipeline, "revise_draft", side_effect=AssertionError("no AI for field edits")):
            self.run_updates([text_update(3, "직함: 영업팀장\n회사: 누리네트웍스", reply_to=target_message)])
        self.assertEqual(pipeline.current_draft(cards.load_card(first["id"])).display_name, "가 영업팀장 누리네트웍스")
        self.assertEqual(pipeline.current_draft(cards.load_card(second["id"])).company, "ACME")

    def test_unthreaded_text_with_several_open_cards_asks_for_a_reply(self):
        with patch.object(pipeline, "scan_front", fake_scan(["가", "나"])):
            self.run_updates([photo_update(1, "a"), photo_update(2, "b")])
        self.run_updates([text_update(3, "직함: 부장")])
        self.assertTrue(any("답장으로" in text for text, _, _ in self.tg.sent))
        self.assertTrue(all(pipeline.current_draft(c).short_title == "과장" for c in cards.list_cards()))

    def test_old_approve_button_is_not_applied_to_a_newer_card(self):
        with patch.object(pipeline, "scan_front", fake_scan(["가"])):
            self.run_updates([photo_update(1, "a")])
        with patch.object(pipeline, "save_contact", side_effect=AssertionError("must not save")):
            self.run_updates([callback(2, "approve")])
        self.assertIn("예전 버튼", self.tg.sent[-1][0])

    def test_stale_revision_button_after_edit(self):
        with patch.object(pipeline, "scan_front", fake_scan(["가"])):
            self.run_updates([photo_update(1, "a")])
        [card] = cards.list_cards()
        old = f"ap:{card['id']}:{card['revision']}"
        self.run_updates([text_update(2, "직함: 부장")])
        with patch.object(pipeline, "save_contact", side_effect=AssertionError("must not save")):
            self.run_updates([callback(3, old)])
        self.assertIn("수정되었습니다", self.tg.edited[-1][0])

    def test_edit_button_sends_force_reply_bound_to_card(self):
        with patch.object(pipeline, "scan_front", fake_scan(["가"])):
            self.run_updates([photo_update(1, "a")])
        [card] = cards.list_cards()
        self.run_updates([callback(2, f"ed:{card['id']}")])
        text, markup, message_id = self.tg.sent[-1]
        self.assertTrue(markup["force_reply"])
        self.assertIn("이름: 가", text)
        self.assertEqual(cards.find_by_message(42, message_id)["id"], card["id"])

    def test_back_photo_goes_to_the_requesting_card(self):
        with patch.object(pipeline, "scan_front", fake_scan(["가"])):
            self.run_updates([photo_update(1, "a")])
        [card] = cards.list_cards()
        self.run_updates([callback(2, f"bk:{card['id']}")])
        prompt_id = self.tg.sent[-1][2]
        merged = []
        with patch.object(pipeline, "recognize_back", lambda c, path: merged.append(c["id"]) or c):
            self.run_updates([photo_update(3, "back", reply_to=prompt_id)])
        self.assertEqual(merged, [card["id"]])
        self.assertEqual(len(cards.list_cards()), 1)

    def test_list_shows_open_cards(self):
        with patch.object(pipeline, "scan_front", fake_scan(["가", "나"])):
            self.run_updates([photo_update(1, "a"), photo_update(2, "b")])
        self.run_updates([text_update(3, "목록")])
        text, markup, _ = self.tg.sent[-1]
        self.assertIn("저장 안 된 명함 2건", text)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "ba:all")

    def test_save_failure_message_is_short_and_offers_retry(self):
        from bizcard_intake.contacts import ContactSaveError
        with patch.object(pipeline, "scan_front", fake_scan(["가"])):
            self.run_updates([photo_update(1, "a")])
        [card] = cards.list_cards()
        with patch.object(pipeline, "save_contact", side_effect=ContactSaveError("Google 서버 일시 오류(502). 잠시 후 다시 시도합니다.", kind="transient", retryable=True)):
            self.run_updates([callback(2, f"ap:{card['id']}:{card['revision']}")])
        text, markup, _ = self.tg.edited[-1]
        self.assertIn("저장 보류", text)
        self.assertNotIn("<", text)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], f"sv:{card['id']}")

    def test_restart_recovers_interrupted_cards(self):
        card = cards.new_card(42)
        card["status"] = "saving"
        cards.save_card(card)
        telegram.recover_interrupted()
        self.assertEqual(cards.load_card(card["id"])["status"], "save_failed")

    def test_album_is_processed_in_parallel_and_summarised_once(self):
        import threading
        import time as _time
        from concurrent.futures import ThreadPoolExecutor

        active, peak = [0], [0]
        lock = threading.Lock()

        def slow_scan(path):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            _time.sleep(0.4)
            with lock:
                active[0] -= 1
            return {"images": {"front": str(path)}, "rectifier": "t", "draft": CardDraft.from_dict({"name": Path(path).stem[-1], "mobile": ["010-1"]})}

        executor = ThreadPoolExecutor(max_workers=4)
        with patch.object(telegram, "_executor", executor), patch.object(pipeline, "scan_front", slow_scan):
            started = _time.monotonic()
            self.run_updates([photo_update(i, f"p{i}", "G2") for i in range(1, 5)])
            returned = _time.monotonic() - started
            executor.shutdown(wait=True)
            total = _time.monotonic() - started
        self.assertLess(returned, 0.3)  # polling loop is free while cards are processed
        self.assertLess(total, 1.2)     # 4 x 0.4 s ran concurrently
        self.assertGreaterEqual(peak[0], 3)
        self.assertEqual([c["seq"] for c in cards.list_cards()], sorted(c["seq"] for c in cards.list_cards()))
        summaries = [text for text, _, _ in self.tg.sent if "보낸 명함 4건" in text]
        self.assertEqual(len(summaries), 1)

    def test_double_tap_saves_once(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        with patch.object(pipeline, "scan_front", fake_scan(["가"])):
            self.run_updates([photo_update(1, "a")])
        [card] = cards.list_cards()
        calls, gate = [], threading.Event()

        def slow_save(draft, image, resource_name=None, checkpoint=None):
            calls.append(1)
            gate.wait(1)
            return SaveResult("created", "people/x", "uploaded", [], draft.display_name)

        executor = ThreadPoolExecutor(max_workers=4)
        data = f"ap:{card['id']}:{card['revision']}"
        with patch.object(telegram, "_executor", executor), patch.object(pipeline, "save_contact", slow_save):
            self.run_updates([callback(2, data), callback(3, data)])
            gate.set()
            executor.shutdown(wait=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(cards.load_card(card["id"])["status"], "saved")

    def test_health_check_alerts_admin_chat_on_failed_checks(self):
        failed = [doctor.Check("gws", False, "Google 인증 무효", "gws auth login"), doctor.Check("parser", True, "ok")]
        with patch.object(doctor, "run_checks", lambda names=None: failed), patch.dict(os.environ, {"BIZCARD_ALERT_CHAT_ID": "42"}), patch.object(
            pipeline, "retry_due", side_effect=AssertionError("no retry while Google auth is broken")
        ):
            problems = telegram.health_check("token")
        self.assertEqual([c.name for c in problems], ["gws"])
        self.assertIn("gws auth login", self.tg.sent[0][0])

    def test_api_calls_retry_transient_network_errors(self):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise URLError("Connection reset by peer")
            return {"ok": True}

        with patch.object(telegram.time, "sleep", lambda s: None):
            self.assertEqual(telegram.with_retry(flaky, "x"), {"ok": True})
            with self.assertRaises(URLError):
                telegram.with_retry(lambda: (_ for _ in ()).throw(URLError("down")), "y")

    def test_rate_limit_honours_retry_after_and_client_errors_fail_fast(self):
        import io
        import json as _json
        from urllib.error import HTTPError

        def http_error(code, body):
            return HTTPError("u", code, "x", {}, io.BytesIO(_json.dumps(body).encode()))

        sleeps, attempts = [], []

        def limited():
            attempts.append(1)
            if len(attempts) == 1:
                raise http_error(429, {"parameters": {"retry_after": 7}})
            return {"ok": True}

        with patch.object(telegram.time, "sleep", sleeps.append):
            telegram.with_retry(limited, "x")
            with self.assertRaises(RuntimeError):
                telegram.with_retry(lambda: (_ for _ in ()).throw(http_error(400, {"description": "Bad Request"})), "y")
        self.assertEqual(sleeps, [7.0])

    def test_get_updates_uses_long_polling(self):
        seen = {}

        def fake_api_get(token, method, params=None, timeout=10):
            seen.update(params or {})
            seen["timeout_arg"] = timeout
            return {"result": []}

        with patch.object(telegram, "api_get", fake_api_get):
            telegram.get_updates("token", 5)
        self.assertEqual(seen["timeout"], telegram.LONG_POLL_SECONDS)
        self.assertGreater(seen["timeout_arg"], telegram.LONG_POLL_SECONDS)


if __name__ == "__main__":
    unittest.main()
