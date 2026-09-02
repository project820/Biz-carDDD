import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bizcard_intake import doctor, pipeline, telegram


def fake_send_message(sent):
    def _send(token, chat_id, text, reply_markup=None):
        sent.append((text, reply_markup))
        return {"result": {"message_id": len(sent)}}

    return _send


ALLOWED = {"TELEGRAM_ALLOWED_CHAT_IDS": "42"}


class TelegramTest(unittest.TestCase):
    def setUp(self):
        telegram._rejected_chats.clear()

    def test_chat_outside_allowlist_is_rejected_once(self):
        sent = []
        with patch.dict(os.environ, ALLOWED), patch.object(telegram, "send_message", fake_send_message(sent)), patch.object(
            pipeline, "scan_front", side_effect=AssertionError("must not process")
        ):
            for _ in range(2):
                telegram.handle_update("token", {"update_id": 1, "message": {"chat": {"id": 7}, "photo": [{"file_id": "x"}]}})
        self.assertEqual(len(sent), 1)
        self.assertIn("7", sent[0][0])

    def test_run_refuses_to_start_without_allowlist(self):
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_CHAT_IDS": ""}):
            with self.assertRaises(SystemExit):
                telegram.run("token")

    def test_text_message_without_draft_gets_guidance(self):
        sent = []
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ALLOWED), patch.object(pipeline, "SESSION_DIR", Path(tmp)), patch.object(
            telegram, "send_message", fake_send_message(sent)
        ):
            telegram.handle_update("token", {"update_id": 1, "message": {"chat": {"id": 42}, "text": "hello"}})
        self.assertEqual(sent[0][0], "명함 사진을 보내주세요.")

    def test_front_photo_flow_sends_preview_draft_and_buttons(self):
        sent, edited, photos = [], [], []

        def fake_scan_front(path):
            return {"status": "drafted", "profile_image_path": str(path), "front_image_path": str(path), "draft": {"name": "홍길동", "mobile": ["010-1"]}, "rectifier": "test"}

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ALLOWED), patch.object(pipeline, "SESSION_DIR", Path(tmp) / "s"), patch.object(
            telegram, "api_get", lambda token, method, params=None: {"result": {"file_path": "photos/card.jpg"}}
        ), patch.object(telegram, "download_file", lambda token, file_path: b"photo"), patch.object(pipeline, "scan_front", fake_scan_front), patch.object(
            telegram, "send_message", fake_send_message(sent)
        ), patch.object(telegram, "edit_message", lambda token, chat_id, message_id, text: edited.append(text) or {}), patch.object(
            telegram, "send_photo", lambda token, chat_id, image_path, caption="": photos.append(caption)
        ):
            telegram.handle_update("token", {"update_id": 1, "message": {"chat": {"id": 42}, "photo": [{"file_id": "small"}, {"file_id": "large"}]}}, Path(tmp))
            session = pipeline.load_session("42")
            self.assertTrue((Path(tmp) / "42" / "front" / "card.jpg").exists())

        self.assertEqual(session["draft"]["name"], "홍길동")
        self.assertEqual(photos, ["연락처 이미지 미리보기"])
        self.assertIn("초안 준비 완료", edited[-1])
        self.assertTrue(sent[-1][0].startswith("명함 입력 초안\n- 이름 필드: 홍길동 대표"))
        self.assertEqual(sent[-1][1]["inline_keyboard"][1][1]["text"], "뒷면스캔")

    def test_text_reply_while_draft_pending_revises_draft(self):
        sent, edited = [], []
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ALLOWED), patch.object(pipeline, "SESSION_DIR", Path(tmp)):
            pipeline.save_session("42", {"status": "drafted", "draft": {"name": "홍길동", "work": ["052-297-8976~7"]}})
            with patch.object(pipeline, "revise_draft", lambda draft, reply: pipeline.CardDraft.from_dict({"name": "홍길동", "work": ["052-297-8976", "052-297-8977"]})), patch.object(
                telegram, "send_message", fake_send_message(sent)
            ), patch.object(telegram, "edit_message", lambda token, chat_id, message_id, text: edited.append(text) or {}):
                telegram.handle_update("token", {"update_id": 3, "message": {"chat": {"id": 42}, "text": "두 회선 맞아"}})
            session = pipeline.load_session("42")
        self.assertEqual(session["draft"]["work"], ["052-297-8976", "052-297-8977"])
        self.assertEqual(edited[-1], "초안 수정 완료")
        self.assertEqual(sent[-1][1]["inline_keyboard"][0][0]["text"], "승인")

    def test_back_photo_merges_and_only_shows_approve_cancel(self):
        sent, edited = [], []
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ALLOWED), patch.object(pipeline, "SESSION_DIR", Path(tmp) / "s"):
            pipeline.save_session("42", {"awaiting": "back", "front_draft": {"name": "홍길동"}, "draft": {"name": "홍길동"}})
            with patch.object(telegram, "api_get", lambda token, method, params=None: {"result": {"file_path": "photos/back.jpg"}}), patch.object(
                telegram, "download_file", lambda token, file_path: b"back"
            ), patch.object(pipeline, "scan_back", lambda session, path: {**session, "status": "merged_drafted", "back_draft": {"name": "홍길동"}, "draft": {"name": "홍길동", "mobile": ["010-1"]}}), patch.object(
                telegram, "send_message", fake_send_message(sent)
            ), patch.object(telegram, "edit_message", lambda token, chat_id, message_id, text: edited.append(text) or {}):
                telegram.handle_update("token", {"update_id": 2, "message": {"chat": {"id": 42}, "photo": [{"file_id": "back"}]}}, Path(tmp))
        self.assertIn("- 휴대전화: 010-1", sent[-1][0])
        self.assertEqual(len(sent[-1][1]["inline_keyboard"]), 1)

    def test_callbacks_approve_cancel(self):
        sent, edited = [], []
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ALLOWED), patch.object(pipeline, "SESSION_DIR", Path(tmp)), patch.object(
            telegram, "api_post", lambda token, method, payload: {"ok": True}
        ), patch.object(telegram, "send_message", fake_send_message(sent)), patch.object(
            telegram, "edit_message", lambda token, chat_id, message_id, text: edited.append(text) or {}
        ), patch.object(pipeline, "approve", lambda key, session: "저장 완료\ncreated"):
            callback = {"id": "1", "data": "approve", "message": {"chat": {"id": 42}}}
            telegram.handle_update("token", {"update_id": 4, "callback_query": callback})
            self.assertEqual(sent[-1][0], "저장 실패: 승인할 초안이 없습니다.")

            pipeline.save_session("42", {"draft": {"name": "홍길동", "mobile": ["010-1"]}})
            telegram.handle_update("token", {"update_id": 5, "callback_query": callback})
            self.assertEqual(edited[-1], "저장 완료\ncreated")

            telegram.handle_update("token", {"update_id": 6, "callback_query": {**callback, "data": "cancel"}})
            self.assertEqual(sent[-1][0], "취소했습니다.")
            self.assertIsNone(pipeline.load_session("42"))

    def test_health_check_alerts_admin_chat_on_failed_checks(self):
        sent = []
        failed = [doctor.Check("gws", False, "Google 인증 무효", "gws auth login"), doctor.Check("parser", True, "ok")]
        with patch.object(doctor, "run_checks", lambda names=None: failed), patch.dict(os.environ, {"BIZCARD_ALERT_CHAT_ID": "42"}), patch.object(
            telegram, "send_message", fake_send_message(sent)
        ):
            problems = telegram.health_check("token")
        self.assertEqual([c.name for c in problems], ["gws"])
        self.assertIn("Google 인증 무효", sent[0][0])
        self.assertIn("gws auth login", sent[0][0])


if __name__ == "__main__":
    unittest.main()
